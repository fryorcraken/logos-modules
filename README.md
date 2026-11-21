# fryorcraken's Logos Modules

A personal catalog of [Logos](https://github.com/logos-co) modules for
[Basecamp](https://github.com/logos-co/logos-basecamp).

## Install this catalog

Add this URL as a module repository in Basecamp's package manager:

```
https://raw.githubusercontent.com/fryorcraken/logos-modules/main/logos-repo.json
```

Basecamp will then be able to discover and install every module listed
below.

## Modules

| Module | What it does |
|---|---|
| [Radicle](https://github.com/fryorcraken/logos-radicle-module) | Browse [Radicle](https://radicle.xyz) repositories — issues, patches, commits and file browsing over a public seed node, no local node required |
| [Blockchain node, portable x86-64](https://github.com/marclawclaw/logos-blockchain-module-x86-64-v2) | **Unofficial** rebuild of the Logos `blockchain_module` 0.3.4 for x86-64 CPUs **without ADX/BMI2** (Intel before Broadwell, Atom-class Celeron/Pentium, AMD before Zen), where the official module dies with "signal 4" ([logos-blockchain#3743](https://github.com/logos-blockchain/logos-blockchain/issues/3743)). Same name and version as the official module; proving is ~2.6× slower. **Experimental; only use it if the official module crashes.** Benchmarks from other CPUs welcome ([how](https://github.com/marclawclaw/logos-blockchain-module-x86-64-v2#share-your-benchmarks)). See below |

### Installing the portable `blockchain_module`

This build uses the same name and version as the official module, and logosctl (up to 0.3.2) ignores `--catalog` on
`install`. So download it from this catalogue explicitly, then install the file:

```sh
logosctl catalog add https://raw.githubusercontent.com/fryorcraken/logos-modules/main/logos-repo.json
logosctl catalog refresh
logosctl package download blockchain_module --catalog fryorcraken-modules --version 0.3.4 -o ./dl
logosctl install ./dl/blockchain_module*.lgx -y
logosctl module load blockchain_module
```

Do not run `logosctl package upgrade blockchain_module` afterwards: it can switch you back to the official build.
Drawbacks, benchmarks and the way forward are in the
[module's README](https://github.com/marclawclaw/logos-blockchain-module-x86-64-v2#readme).
