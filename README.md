# flowverse

> The flows humanize offers but does not ship.

A flowverse is a git repository with a `flows/` directory of
[humanize](https://github.com/humanfia/humanize) flows in it: one `.py` file per flow, and
whatever they import beside them under names that start with an underscore. Nothing outside
that directory is read. This is the official one. It is offered from the start, under the name
`official`, and is fetched the first time somebody runs one of its flows.

## Table of Contents

- [Install](#install)
- [Usage](#usage)
- [Contributing](#contributing)
- [License](#license)

## Install

Nothing to install: humanize fetches this repository itself, into
`~/.humanize/flowverses/official/`.

```sh
hmz  # /flow, then left and right to `official`
```

To fetch the newest version of it, refresh it from that same menu.

## Usage

Every flow here is named `official/<flow>`, and one that holds several is
`official/<flow>:<name>`:

```sh
hmz exec -f official/rlar \
    -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max "$(cat TASK.md)"
```

| Flow | What it does |
| --- | --- |
| `official/continue_loop` | One session told to continue until it says it is finished. |
| `official/fixed_juice_ralph` | A ralph loop held to an answer size, the effort moved between turns. |
| `official/flame_chase` | Two agents chasing each other's work, turn about. |
| `official/goal` | One agent given a goal, asked again until the goal is met. |
| `official/humanize1:gen-idea` | Opens a loose idea into a repo-grounded draft. |
| `official/humanize1:gen-plan` | Turns that draft into a plan both sides have converged on. |
| `official/humanize1:rlcr` | Builds the plan under review until nothing is left to say. |
| `official/rlar` | An actor works and a reviewer reads it, until the reviewer has nothing left. |

Each flow's own file says what it drives, what it can be set up with, and the line that starts
it. `hmz exec -f official/<flow> --help` says the same thing at a command line.

## Contributing

```
flowverse/
├── flows/            what humanize reads, and the only thing it reads
│   ├── rlar.py       →  official/rlar
│   ├── humanize1.py  →  official/humanize1:gen-idea, :gen-plan, :rlcr
│   └── _humanize1/   not a flow; what humanize1.py imports
└── tests/            this repository's own, run against humanize itself
```

A flow is one file in `flows/` that imports nothing of humanize but `hmz.agents`, and holds a
`run(agents, task)` -- or several entry points marked with `@flow`, which is what makes one
file three flows. Tests live under `tests/`, and run against humanize itself:

```sh
uv run pytest
uv run ruff check
```

PRs accepted.

## License

Apache-2.0 © humanfia
