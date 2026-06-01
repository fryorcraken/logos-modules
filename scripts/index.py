#!/usr/bin/env python3
# Local index.json manager for a self-hosted Logos module catalog.
#
# The GitHub-Actions path (rebuild-index.yml → logos-modules-release-action's
# scripts/build_index.py) builds index.json by walking GitHub release tags
# and reading a sidecar.json baked into each release. This script does the
# same job from a plain URL list, so a catalog can host its .lgx files
# anywhere (S3, your own server, …) and still produce / mutate / inspect a
# byte-compatible index.json. Clients (lgpd, the package_downloader module,
# the package-manager UI) consume the output unchanged.
#
# Per-version entry format mirrors build_index.py:
#   { releasedAt, publisherRef, url, size, sha256, rootHash,
#     manifest, signature? }
#
# Usage:
#   ./scripts/index.py build    <urls-file> [-o index.json] [--name NAME]
#   ./scripts/index.py add      <index.json> [<url>...] [--from-file FILE]
#   ./scripts/index.py remove   <index.json> <package> [version]
#   ./scripts/index.py list     <index.json>
#   ./scripts/index.py show     <index.json> <package>
#   ./scripts/index.py validate <index.json> [--full]
#
# `build`, `add`, and `validate --full` require the `lgx` binary on PATH
# (every package is verified). `remove` / `list` / `show` / `validate`
# (light) are pure JSON ops — no `lgx`, no network.
#
# Install lgx:   nix build github:logos-co/logos-package#lgx
#
# Standard library only — `urllib`, `tarfile`-free (manifest + signature
# come from `lgx`), no third-party deps.

import argparse
import email.utils
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

SCHEMA_VERSION = 2
# Manifest fields the downloader cross-checks index ↔ file on
# (verifyDownloadAgainstIndex in package_downloader_lib.cpp). Keep this
# in sync with the C++ side — divergence would mean validate --full
# accepts indexes the client would later reject at install time.
MANIFEST_BINDING_FIELDS = ("name", "version", "main", "dependencies", "type")


# ── stdio helpers ────────────────────────────────────────────────────────

def info(msg: str) -> None:
    # `==>` prefix matches add-module.sh / catalog.sh conventions.
    print(f"==> {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> "_NoReturn":  # type: ignore[name-defined]
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


# ── lgx wrappers ─────────────────────────────────────────────────────────

def require_lgx() -> None:
    """Preflight: abort early when `lgx` isn't on PATH so we don't get a
    cryptic FileNotFoundError mid-download."""
    if shutil.which("lgx") is None:
        die(
            "the `lgx` binary is required but not on PATH.\n"
            "       Install with:  nix build github:logos-co/logos-package#lgx\n"
            "       Or skip the install path: this script's `remove` / `list` /\n"
            "       `show` / `validate` (light) subcommands work without lgx."
        )


def lgx_run(*args: str) -> bytes:
    """Run `lgx <args>` and return stdout bytes. Raises RuntimeError on
    non-zero exit, with the (decoded) stderr in the message."""
    r = subprocess.run(["lgx", *args], capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip() or "(no stderr)"
        raise RuntimeError(f"lgx {' '.join(args)} failed: {err}")
    return r.stdout


# ── time helpers ─────────────────────────────────────────────────────────

def iso_now() -> str:
    """ISO-8601 UTC second-precision with trailing Z — the exact shape
    build_index.py emits, so a manually-built index reads identically
    when diffed against the GitHub-Actions output."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_http_date(http_date: str | None) -> str | None:
    """Convert an HTTP `Last-Modified` header (RFC 7231 / RFC 2822 format)
    to the ISO `Z` shape used in index entries. Returns None when the
    header is absent or unparseable."""
    if not http_date:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # parsedate_to_datetime may return a naive datetime (assumed UTC) or
    # an aware one — normalise both to aware-UTC before formatting.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── index I/O ────────────────────────────────────────────────────────────

def load_index(path: pathlib.Path) -> dict:
    """Read and minimally type-check an index.json. Used by every
    subcommand that operates on an existing index — load failure is
    fatal, structural defects surface here so subcommands don't have to
    re-validate the top-level shape themselves."""
    if not path.exists():
        die(f"index file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"failed to read {path}: {exc}")
    if not isinstance(data, dict) or "packages" not in data:
        die(f"{path} doesn't look like an index.json (no top-level `packages`)")
    if not isinstance(data["packages"], list):
        die(f"{path}: `packages` must be a JSON array")
    return data


def save_index(path: pathlib.Path, index: dict) -> None:
    """Write back with the same shape build_index.py uses — 2-space
    indent, sort_keys=False so the natural top-level field order
    (schemaVersion, repositoryName, generatedAt, packages) stays
    readable, trailing newline."""
    path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def resolve_repo_name(cli_name: str | None) -> str:
    """`repositoryName` precedence: --name > `logos-repo.json` in cwd >
    "unknown". Mirrors what build_index.py reads from the catalog
    repo's logos-repo.json so the generated index header looks the
    same."""
    if cli_name:
        return cli_name
    repo_file = pathlib.Path("logos-repo.json")
    if repo_file.exists():
        try:
            name = json.loads(repo_file.read_text(encoding="utf-8")).get("name", "")
            if isinstance(name, str) and name:
                return name
        except (OSError, json.JSONDecodeError):
            pass
    return "unknown"


# ── URL list parsing ─────────────────────────────────────────────────────

def read_url_list(path: pathlib.Path) -> list[str]:
    """One URL per line, blank lines and `#` comments ignored. Inline
    `# ...` trailing comments stripped too — common shell-style file
    convention."""
    urls: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        # Strip an inline comment if present, but only when preceded by
        # whitespace — URLs never contain `#` followed by whitespace and
        # this keeps a `#` that's actually part of the URL intact.
        if " #" in raw:
            raw = raw.split(" #", 1)[0]
        elif "\t#" in raw:
            raw = raw.split("\t#", 1)[0]
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


# ── per-URL extraction (shared by build + add) ───────────────────────────

def download(url: str, dest: pathlib.Path) -> str | None:
    """Fetch a URL into `dest`, returning the response's Last-Modified
    header verbatim (or None when the server didn't send one). We don't
    parse it here — caller decides how to convert / fall back."""
    info(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "logos-index.py/1"})
    with urllib.request.urlopen(req) as resp:
        last_modified = resp.headers.get("Last-Modified")
        with dest.open("wb") as f:
            shutil.copyfileobj(resp, f)
    return last_modified


def version_entry_from_lgx(
    url: str, lgx_path: pathlib.Path, last_modified: str | None
) -> tuple[str, dict]:
    """Turn a downloaded .lgx into (package_name, version_entry).

    Pipeline:
      1. `lgx verify` — abort the whole run on failure (locked policy:
         a single bad package fails the index build).
      2. `lgx manifest --json` for the full manifest (matches what
         build_index.py records).
      3. `lgx signature` for the raw manifest.sig (empty = unsigned).
      4. sha256 + size from the downloaded bytes.

    The entry shape is byte-compatible with what build_index.py emits,
    so a diff against the GitHub-Actions index for the same packages
    differs only in `releasedAt` (Last-Modified vs the GH release time)
    and the top-level `generatedAt`."""
    info(f"verifying {lgx_path.name}")
    try:
        lgx_run("verify", str(lgx_path))
    except RuntimeError as exc:
        die(f"package failed `lgx verify` ({url}): {exc}")

    raw_manifest = lgx_run("manifest", str(lgx_path), "--json")
    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        die(f"{url}: package's manifest.json is unparseable: {exc}")

    raw_sig = lgx_run("signature", str(lgx_path))
    signature: dict | None = None
    if raw_sig.strip():
        try:
            signature = json.loads(raw_sig)
        except json.JSONDecodeError as exc:
            die(f"{url}: package's manifest.sig is unparseable: {exc}")

    data = lgx_path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    size = len(data)

    name = manifest.get("name", "")
    version = manifest.get("version", "")
    if not name or not version:
        die(f"{url}: manifest is missing `name` or `version`")
    root_hash = manifest.get("hashes", {}).get("root", "")
    if not root_hash:
        die(f"{url}: manifest has no `hashes.root`")

    entry: dict = {
        "releasedAt": iso_from_http_date(last_modified) or iso_now(),
        # `publisherRef` is informational (the client ignores it). In
        # the GH flow it's the release tag; for a self-hosted catalog
        # there isn't one, so we synthesise the same shape.
        "publisherRef": f"{name}-v{version}",
        "url": url,
        "size": size,
        "sha256": sha256,
        "rootHash": root_hash,
        "manifest": manifest,
    }
    if signature is not None:
        entry["signature"] = signature
    return name, entry


def fetch_entry(url: str, workdir: pathlib.Path) -> tuple[str, dict]:
    """Download + extract for a single URL, into a workdir we control
    (caller cleans it up)."""
    # Disambiguate filenames so two URLs ending in the same basename
    # don't collide in workdir. The lgx file itself is opaque; the name
    # we pick is just a working handle.
    lgx_path = workdir / f"pkg-{len(list(workdir.iterdir())):04d}.lgx"
    last_modified = download(url, lgx_path)
    return version_entry_from_lgx(url, lgx_path, last_modified)


# ── index mutation helpers ───────────────────────────────────────────────

def find_package(index: dict, name: str) -> dict | None:
    """Linear scan by name. Catalogs are small (tens of entries); a hash
    index isn't worth the complexity."""
    for pkg in index["packages"]:
        if pkg.get("name") == name:
            return pkg
    return None


def merge_version(index: dict, name: str, entry: dict) -> bool:
    """Insert `entry` under the package called `name`, creating the
    package entry if it didn't exist. Dedupe on (version, rootHash) —
    an incoming exact duplicate is skipped (returns False). Caller
    re-sorts.

    Returns True when the index was modified."""
    incoming_v = entry["manifest"]["version"]
    incoming_h = entry["rootHash"]

    pkg = find_package(index, name)
    if pkg is None:
        index["packages"].append({"name": name, "versions": [entry]})
        return True

    for v in pkg["versions"]:
        if v.get("manifest", {}).get("version") == incoming_v \
                and v.get("rootHash") == incoming_h:
            info(f"skipping {name} v{incoming_v} — already present "
                 f"(rootHash matches)")
            return False
    pkg["versions"].append(entry)
    return True


def sort_versions(index: dict) -> None:
    """Sort each package's `versions` descending by releasedAt so the
    client's "newest first" picker (`findBest` in the downloader)
    matches the order the catalog actually intends."""
    for pkg in index["packages"]:
        pkg["versions"].sort(
            key=lambda v: v.get("releasedAt", ""), reverse=True
        )


def bump_generated_at(index: dict) -> None:
    index["generatedAt"] = iso_now()


# ── subcommand: build ────────────────────────────────────────────────────

def cmd_build(args: argparse.Namespace) -> int:
    require_lgx()
    urls_file = pathlib.Path(args.urls_file)
    if not urls_file.exists():
        die(f"urls file not found: {urls_file}")
    urls = read_url_list(urls_file)
    if not urls:
        die(f"{urls_file}: no URLs found (blank lines and `#` comments are ignored)")

    index: dict = {
        "schemaVersion": SCHEMA_VERSION,
        "repositoryName": resolve_repo_name(args.name),
        "generatedAt": iso_now(),
        "packages": [],
    }

    with tempfile.TemporaryDirectory(prefix="logos-index-") as tmpdir:
        workdir = pathlib.Path(tmpdir)
        for url in urls:
            name, entry = fetch_entry(url, workdir)
            merge_version(index, name, entry)

    sort_versions(index)
    out = pathlib.Path(args.output)
    save_index(out, index)
    info(f"wrote {out} — {len(index['packages'])} package(s)")
    return 0


# ── subcommand: add ──────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace) -> int:
    require_lgx()
    index_path = pathlib.Path(args.index)
    index = load_index(index_path)

    urls: list[str] = list(args.url or [])
    if args.from_file:
        urls.extend(read_url_list(pathlib.Path(args.from_file)))
    if not urls:
        die("add: need at least one URL (positional or via --from-file)")

    added = 0
    with tempfile.TemporaryDirectory(prefix="logos-index-") as tmpdir:
        workdir = pathlib.Path(tmpdir)
        for url in urls:
            name, entry = fetch_entry(url, workdir)
            if merge_version(index, name, entry):
                added += 1

    sort_versions(index)
    bump_generated_at(index)
    save_index(index_path, index)
    info(f"updated {index_path} — added {added} version(s) "
         f"({len(urls) - added} skipped)")
    return 0


# ── subcommand: remove ───────────────────────────────────────────────────

def cmd_remove(args: argparse.Namespace) -> int:
    index_path = pathlib.Path(args.index)
    index = load_index(index_path)

    pkg = find_package(index, args.package)
    if pkg is None:
        die(f"package not found in index: {args.package}")

    if args.version is None:
        # Drop the whole package entry.
        n = len(pkg["versions"])
        index["packages"] = [
            p for p in index["packages"] if p.get("name") != args.package
        ]
        info(f"removed package `{args.package}` ({n} version(s))")
    else:
        # Drop every entry matching the version string (multiple are
        # technically possible if different rootHashes share a version
        # string — rare, but the index allows it; remove them all so the
        # command's contract is "this version is gone").
        before = len(pkg["versions"])
        pkg["versions"] = [
            v for v in pkg["versions"]
            if v.get("manifest", {}).get("version") != args.version
        ]
        removed = before - len(pkg["versions"])
        if removed == 0:
            die(f"version not found: {args.package} v{args.version}")
        # Empty package entry is useless to clients; drop it.
        if not pkg["versions"]:
            index["packages"] = [
                p for p in index["packages"] if p.get("name") != args.package
            ]
            info(f"removed `{args.package}` v{args.version} "
                 f"(was the last version — package dropped)")
        else:
            info(f"removed `{args.package}` v{args.version} "
                 f"({removed} entry(ies))")

    bump_generated_at(index)
    save_index(index_path, index)
    return 0


# ── subcommand: list ─────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> int:
    index = load_index(pathlib.Path(args.index))
    print(f"repositoryName: {index.get('repositoryName', '?')}")
    print(f"schemaVersion:  {index.get('schemaVersion', '?')}")
    print(f"generatedAt:    {index.get('generatedAt', '?')}")
    print(f"packages:       {len(index['packages'])}")
    if not index["packages"]:
        return 0
    print()
    # Column-align: name | versions | latest
    name_w = max(len(p.get("name", "")) for p in index["packages"])
    name_w = max(name_w, len("name"))
    print(f"{'name':<{name_w}}  versions  latest")
    print(f"{'-' * name_w}  --------  ------")
    for pkg in index["packages"]:
        name = pkg.get("name", "?")
        versions = pkg.get("versions", [])
        latest = versions[0].get("manifest", {}).get("version", "?") if versions else "(none)"
        print(f"{name:<{name_w}}  {len(versions):>8}  {latest}")
    return 0


# ── subcommand: show ─────────────────────────────────────────────────────

def cmd_show(args: argparse.Namespace) -> int:
    index = load_index(pathlib.Path(args.index))
    pkg = find_package(index, args.package)
    if pkg is None:
        die(f"package not found in index: {args.package}")

    print(f"package: {pkg.get('name')}")
    print(f"versions ({len(pkg.get('versions', []))}):")
    for v in pkg.get("versions", []):
        version = v.get("manifest", {}).get("version", "?")
        released = v.get("releasedAt", "?")
        root_hash = v.get("rootHash", "")
        short_hash = root_hash[:12] + "…" if len(root_hash) > 12 else root_hash
        sig = v.get("signature") or {}
        signer = sig.get("did", "") if isinstance(sig, dict) else ""
        signed_marker = signer if signer else "(unsigned)"
        url = v.get("url", "")
        print(f"  v{version}")
        print(f"    releasedAt: {released}")
        print(f"    rootHash:   {short_hash}")
        print(f"    signed:     {signed_marker}")
        print(f"    url:        {url}")
    return 0


# ── subcommand: validate ─────────────────────────────────────────────────

def _validate_light(index_path: pathlib.Path, index: dict) -> list[str]:
    """Structural + internal consistency. No network, no `lgx` — just
    walks the JSON tree and reports every inconsistency it finds.

    Returns a list of human-readable problems (empty = clean)."""
    issues: list[str] = []
    # Header
    if index.get("schemaVersion") != SCHEMA_VERSION:
        issues.append(
            f"schemaVersion: expected {SCHEMA_VERSION}, "
            f"got {index.get('schemaVersion')!r}"
        )
    if not isinstance(index.get("repositoryName"), str) or not index["repositoryName"]:
        issues.append("repositoryName: missing or not a non-empty string")
    if not isinstance(index.get("generatedAt"), str):
        issues.append("generatedAt: missing or not a string")

    # Packages
    pkg_names_seen: set[str] = set()
    for pi, pkg in enumerate(index["packages"]):
        ctx = f"packages[{pi}]"
        if not isinstance(pkg, dict):
            issues.append(f"{ctx}: not an object")
            continue
        name = pkg.get("name")
        if not isinstance(name, str) or not name:
            issues.append(f"{ctx}: `name` missing or not a non-empty string")
            continue
        if name in pkg_names_seen:
            issues.append(f"{ctx}: duplicate package name {name!r}")
        pkg_names_seen.add(name)

        versions = pkg.get("versions")
        if not isinstance(versions, list) or not versions:
            issues.append(f"{ctx} ({name}): `versions` must be a non-empty array")
            continue

        seen_keys: set[tuple[str, str]] = set()
        previous_released: str | None = None
        for vi, v in enumerate(versions):
            vctx = f"{ctx}.versions[{vi}] ({name})"
            if not isinstance(v, dict):
                issues.append(f"{vctx}: not an object")
                continue
            # Required fields the downloader (and findBest) lean on.
            for required in ("releasedAt", "url", "rootHash", "manifest"):
                if required not in v:
                    issues.append(f"{vctx}: missing `{required}`")
            manifest = v.get("manifest")
            if not isinstance(manifest, dict):
                issues.append(f"{vctx}: `manifest` must be an object")
                continue
            for required in MANIFEST_BINDING_FIELDS:
                if required not in manifest:
                    issues.append(f"{vctx}: manifest missing `{required}`")
            mname = manifest.get("name")
            if mname is not None and mname != name:
                issues.append(
                    f"{vctx}: manifest.name {mname!r} != package name {name!r}"
                )
            # Sort order
            released = v.get("releasedAt")
            if isinstance(released, str) and previous_released is not None:
                if released > previous_released:
                    issues.append(
                        f"{vctx}: out of order — versions must be sorted "
                        f"descending by releasedAt"
                    )
            previous_released = released if isinstance(released, str) else previous_released
            # Dedup key
            key = (manifest.get("version", ""), v.get("rootHash", ""))
            if key in seen_keys:
                issues.append(
                    f"{vctx}: duplicate (version, rootHash)={key!r} within package"
                )
            seen_keys.add(key)
    return issues


def _validate_entry_against_file(
    pkg_name: str, entry: dict, workdir: pathlib.Path
) -> list[str]:
    """Cross-check one index entry against its actual .lgx file. Used by
    `--full` only. Mirrors the bindings the client enforces at install
    time in verifyDownloadAgainstIndex (rootHash + manifest fields +
    signer DID) — running this offline catches the same mismatches the
    download path would, but at index-publish time."""
    issues: list[str] = []
    url = entry.get("url", "")
    if not url:
        issues.append(f"{pkg_name}: entry has no url; cannot full-validate")
        return issues
    try:
        # Reuse the same fetch+verify+extract pipeline `add`/`build` use.
        observed_name, observed = fetch_entry(url, workdir)
    except SystemExit:
        # fetch_entry calls die() on a fatal extraction error; convert
        # the would-be exit into a per-entry issue so validate reports
        # the whole batch rather than stopping at the first mismatch.
        raise  # but die() already called sys.exit; this branch is
        # unreachable. Documented for clarity.

    # Cross-checks — mirror verifyDownloadAgainstIndex's bindings.
    if observed_name != pkg_name:
        issues.append(
            f"{pkg_name}: file's manifest.name {observed_name!r} doesn't match "
            f"index package name"
        )
    if entry.get("rootHash") != observed.get("rootHash"):
        issues.append(
            f"{pkg_name} v{entry.get('manifest', {}).get('version', '?')}: "
            f"rootHash mismatch (index={entry.get('rootHash')}, "
            f"file={observed.get('rootHash')})"
        )
    idx_manifest = entry.get("manifest", {}) or {}
    obs_manifest = observed.get("manifest", {}) or {}
    for field in MANIFEST_BINDING_FIELDS:
        a = idx_manifest.get(field)
        b = obs_manifest.get(field)
        if a != b:
            issues.append(
                f"{pkg_name} v{idx_manifest.get('version', '?')}: "
                f"manifest.{field} mismatch (index vs file)"
            )
    idx_sig = entry.get("signature") or {}
    obs_sig = observed.get("signature") or {}
    if isinstance(idx_sig, dict) and idx_sig.get("did"):
        if not isinstance(obs_sig, dict) or obs_sig.get("did") != idx_sig.get("did"):
            issues.append(
                f"{pkg_name} v{idx_manifest.get('version', '?')}: "
                f"signature.did mismatch (index={idx_sig.get('did')}, "
                f"file={obs_sig.get('did') if isinstance(obs_sig, dict) else None})"
            )
    if "sha256" in entry and entry["sha256"] != observed.get("sha256"):
        issues.append(
            f"{pkg_name} v{idx_manifest.get('version', '?')}: sha256 mismatch"
        )
    if "size" in entry and entry["size"] != observed.get("size"):
        issues.append(
            f"{pkg_name} v{idx_manifest.get('version', '?')}: size mismatch "
            f"({entry['size']} vs {observed.get('size')})"
        )
    return issues


def cmd_validate(args: argparse.Namespace) -> int:
    index_path = pathlib.Path(args.index)
    index = load_index(index_path)

    issues = _validate_light(index_path, index)

    if args.full:
        require_lgx()
        info("light pass complete; running --full cross-check against URLs…")
        with tempfile.TemporaryDirectory(prefix="logos-index-validate-") as tmpdir:
            workdir = pathlib.Path(tmpdir)
            for pkg in index["packages"]:
                pkg_name = pkg.get("name", "?")
                for entry in pkg.get("versions", []):
                    issues.extend(
                        _validate_entry_against_file(pkg_name, entry, workdir)
                    )

    if not issues:
        print(f"{index_path}: OK ({len(index['packages'])} package(s)"
              + (", full check)" if args.full else ", light check)"))
        return 0
    for problem in issues:
        print(problem)
    print(f"\n{len(issues)} issue(s) — index failed validation",
          file=sys.stderr)
    return 1


# ── argparse wiring ──────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="index.py",
        description="Manage a self-hosted Logos catalog's index.json. "
                    "See module-level docstring for the full subcommand reference.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    pb = sub.add_parser("build", help="full rebuild from a URL-list file")
    pb.add_argument("urls_file", help="text file, one .lgx URL per line "
                                      "(blank lines + `#` comments ignored)")
    pb.add_argument("-o", "--output", default="index.json",
                    help="output path (default: index.json)")
    pb.add_argument("--name", default=None,
                    help="repositoryName for the header (default: read from "
                         "./logos-repo.json, fallback to 'unknown')")
    pb.set_defaults(func=cmd_build)

    pa = sub.add_parser("add", help="merge new .lgx URLs into an existing index")
    pa.add_argument("index", help="index.json path (modified in place)")
    pa.add_argument("url", nargs="*", help=".lgx URL(s) — positional")
    pa.add_argument("--from-file", default=None,
                    help="read additional URLs from this file")
    pa.set_defaults(func=cmd_add)

    pr = sub.add_parser("remove", help="drop a package, or one of its versions")
    pr.add_argument("index", help="index.json path (modified in place)")
    pr.add_argument("package", help="package name to remove")
    pr.add_argument("version", nargs="?", default=None,
                    help="optional version string; omit to drop the whole package")
    pr.set_defaults(func=cmd_remove)

    pl = sub.add_parser("list", help="header + one line per package")
    pl.add_argument("index", help="index.json path")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="versions + details for one package")
    ps.add_argument("index", help="index.json path")
    ps.add_argument("package", help="package name")
    ps.set_defaults(func=cmd_show)

    pv = sub.add_parser("validate",
                        help="structural check (light); add --full to also "
                             "download every package and cross-check index↔file")
    pv.add_argument("index", help="index.json path")
    pv.add_argument("--full", action="store_true",
                    help="download every package and verify the index entry "
                         "matches the actual .lgx (same bindings the client "
                         "enforces at install time)")
    pv.set_defaults(func=cmd_validate)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
