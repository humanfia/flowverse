# flowverse

> The flows humanize offers but does not ship.

A flowverse is a git repository with a `flows/` directory of
[humanize](https://github.com/humanfia/humanize) flows in it: one directory per flow, holding
the `__init__.py` that is the flow, whatever it imports beside it, and the `skills/` it brings.
Nothing outside that directory is read. This is the official one. It is offered from the start,
under the name `official`, and is fetched the first time somebody runs one of its flows.

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
├── flows/                   what humanize reads, and the only thing it reads
│   ├── rlar/                →  official/rlar
│   │   ├── __init__.py         the flow itself
│   │   └── skills/             what it brings: one directory per skill, each a SKILL.md
│   └── humanize1/           →  official/humanize1:gen-idea, :gen-plan, :rlcr
│       ├── __init__.py
│       └── _humanize1/         not a flow; what the flow beside it imports
└── tests/                   this repository's own, run against humanize itself
```

A flow is one directory in `flows/` whose `__init__.py` imports nothing of humanize but
`hmz.agents` and holds a `run(agents, task)` -- or several entry points marked with `@flow`,
which is what makes one flow three flows. Everything it needs lives inside that directory, so
a flow can be copied, forked and edited whole: `f` on it in humanize's `/flow` menu writes a
copy into `.humanize/flows/` for you to change.

The skills in a flow's `skills/` are mounted onto every session its agents open, in the layout
every one of these CLIs already reads a skill in -- a directory apiece, each holding a
`SKILL.md`. A flow may also name skills that live in another repository, by writing them where
it is declared:

```python
@flow(skills=("https://github.com/humanfia/flowverse#review-notes",))
def run(agents: Agents, task: str) -> None: ...
```

Tests live under `tests/`, and run against humanize itself:

```sh
uv run pytest
uv run ruff check
```

PRs accepted.

## License

Apache-2.0 © humanfia
