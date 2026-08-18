# flowverse

> The flows humanize offers but does not ship.

A flowverse is a git repository with a `flows/` directory of
[humanize](https://github.com/humanfia/humanize2) flows in it. A flow is a module, in either of
its two shapes: a directory holding the `__init__.py` that is the flow, whatever it imports
beside it and the `skills/` it brings, or a single `.py` file for one that needs neither.
Nothing outside `flows/` is read. This is the official one. It is offered from the start,
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

To fetch the newest version of it, press `r` on it in `/flowverses`, which is where the places
flows come from are kept.

## Usage

Every flow here is named `official/<flow>`, and one that holds several is
`official/<flow>:<name>`:

```sh
hmz exec -f official/rlar \
    -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max "$(cat TASK.md)"
```

| Flow | What it does | Picked up |
| --- | --- | --- |
| `official/continue_loop` | One session told to continue until it says it is finished. | the round it is on |
| `official/fixed_juice_ralph` | A ralph loop held to an answer size, the effort moved between turns. | the round, and the rung the governor settled at |
| `official/flame_chase` | Two agents chasing each other's work, turn about. | whose turn is next, and the rounds behind it |
| `official/goal` | One agent given a goal, asked again until the goal is met. | the round it is on |
| `official/humanize1:gen-idea` | Opens a loose idea into a repo-grounded draft. | — |
| `official/humanize1:gen-plan` | Turns that draft into a plan both sides have converged on. | — |
| `official/humanize1:rlcr` | Builds the plan under review until nothing is left to say. | the loop's own directory, carried on rather than stamped afresh |
| `official/rlar` | An actor works and a reviewer reads it, until the reviewer has nothing left. | the review the actor is owed |

Each flow's own file says what it drives, what it can be set up with, and the line that starts
it. `hmz exec -f official/<flow> --help` says the same thing at a command line.

Every loop here can be picked up where the last run of it left off: running it again in the
same directory carries on from what that run wrote down, which is the third column. What is
never carried is a conversation -- no backend reopens a session -- so a picked-up run opens
its own, and what the agents know is the repository and what the flow tells them. The two
drafting phases keep nothing: each writes one file, and running one again is meant to write
another.

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
`hmz.flows` -- the mark, the `Agent` and `Session` interfaces it drives, and whatever else it
needs, all handed through from that one name -- and holds a `run(agents, task)` -- or several
entry points marked with `@flow`,
which is what makes one flow three flows. `@flow(resumable=True)` takes a dict after that,
holding whatever the last run of it here wrote there; keep to what JSON holds, and write it as
you go rather than at the end, since a run worth picking up is one that was stopped. A flow
that brings nothing with it may be a single `.py` file instead. Everything a flow needs lives inside its own directory, so it can be
copied, forked and edited whole: `f` on it in humanize's `/flow` menu writes a copy into
`.humanize/flows/` for you to change.

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
