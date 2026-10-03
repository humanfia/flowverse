# flowverse

[![validate](https://github.com/humanfia/flowverse/actions/workflows/validate.yml/badge.svg)](https://github.com/humanfia/flowverse/actions/workflows/validate.yml)

The official index of [humanize](https://github.com/humanfia/humanize) flows: which flows
there are, which versions of each, and the exact commit each version is.

This repository holds no flow code. Each flow lives in a public GitHub repository of its own,
and this index pins every released version of it to a commit, as
[winget-pkgs](https://github.com/microsoft/winget-pkgs) does for winget.

## Table of Contents

- [Background](#background)
- [Install](#install)
- [Usage](#usage)
- [Contributing](#contributing)
- [License](#license)

## Background

One manifest per released version of a flow:

```text
flows/
└── recursive_lean_prover/          the flow's name
    └── 0.1.0/                      a SemVer 2.0.0 version
        └── flow.yaml               where that version is
```

```yaml
name: recursive_lean_prover
version: 0.1.0
description: Recursively plan, prove, compare, review and catalogue Lean theorems.
repo: humanfia/flow-recursive-lean-prover
ref: v0.1.0
commit: 6cfb688a5a38b51965b641fed8ad7aa063ae197f
subdir: recursive_lean_prover
license: Apache-2.0
dependencies:
  humanize1: ">=0.1.0,<0.2.0"
```

hmz installs exactly `commit`, so a version means the same code for everybody, for good. A
published version is never edited: a change is a new version. Every field is described in
[CONTRIBUTING.md](CONTRIBUTING.md#the-manifest), and checked against
[schema/flow.schema.json](schema/flow.schema.json).

The loops humanize is built around (`chat`, `ralph_loop`, `goal`, `flame_chase`,
`stateful_ralph`, `continue_loop`, `rlar`) ship inside humanize itself and are not listed here.

## Install

Nothing to install: this index is built into [hmz](https://github.com/humanfia/humanize) as its
official flowverse.

## Usage

In `hmz`, open `/flows` and go to **Flowverses**. Pick a flow, pick one of its versions, and
install it; installed flows are the ones you can run. hmz keeps the index itself up to date in
the background, and tells you when a flow you installed has a newer version.

**Your own flowverse.** Any GitHub repository laid out like this one is a flowverse: a
`flows/<name>/<version>/flow.yaml` per release. Add it from the same Flowverses page, next to
this one. Copy [schema/](schema/) and [.github/](.github/) to give it the same checks.

> [!WARNING]
> Installing a flow runs its code on your machine, and its agents work without asking for
> approval. Review in this index lowers the risk; it does not remove it. Read
> [Security](https://docs.humanfia.ai/humanize/user/security) first.

## Contributing

To list your flow, publish it in a repository of your own and open a pull request adding its
manifest: [CONTRIBUTING.md](CONTRIBUTING.md) is the whole guide. Report a malicious flow
privately, as [SECURITY.md](SECURITY.md) explains. Everyone taking part follows the
[code of conduct](CODE_OF_CONDUCT.md).

## License

The manifests, schema and scripts here are [Apache-2.0](LICENSE) &copy; Humanfia. Each flow is
under its own license, named in its manifest.
