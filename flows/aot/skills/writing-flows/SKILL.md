---
name: writing-flows
description: The contract a humanize flow is written to. Use when writing or repairing a flow -- its shape on disk, its one import, how its loops end, how its answers are guarded, and how it says what it is.
---

# Writing a flow

A flow is a Python function driving coding agents, and everything below is what a checker,
a stub-driven proof and a fresh critic will hold your draft to. Write to it the first time;
every refusal costs a repair round.

## The shape on disk

A flow is a directory named for the flow, holding the `__init__.py` that is the flow.
Everything it needs lives inside that directory: helper code as an underscore-named sibling
module or package (`_myflow/`), skills it brings as `skills/<name>/SKILL.md`. A flow whose
parts are elsewhere is a flow with a hole in it wherever it is copied to.

Import that sibling by its plain name -- `from _myflow import helpers` -- never relatively
(`from ._myflow import ...`): humanize runs `__init__.py` as a file (`runpy.run_path`) with the
flow's own directory on `sys.path`, not as a package, so a relative import has no parent package
to resolve against.

## One import

The whole of humanize a flow imports is `hmz.flows`, and only names it offers:

```python
from hmz.flows import Agent, Person, flow
```

Plus the standard library and `pydantic`. Never `hmz.agents`, `hmz.backends` or any other
module of humanize's own -- those move, and a flow is somebody else's repository.

## The entry point

```python
from typing import Any, NamedTuple

from hmz.flows import Agent, Person, flow
from pydantic import BaseModel, Field


class Agents(NamedTuple):
    actor: Agent  # one field per agent, named for what it is for
    human: Person  # only if the flow talks to the person at the prompt


@flow
def run(agents: Agents, task: str, config: Config | None = None) -> None: ...
```

- The first parameter is the agents, annotated with a NamedTuple of them (or a fixed-length
  `tuple[Agent, ...spelled out...]`). Never `tuple[Agent, ...]` with an ellipsis: how many
  agents the flow drives is the one thing a command line cannot otherwise know.
- The second is the task. A config, if the flow takes one, is third and defaults to `None`.
- A resumable flow says `@flow(resumable=True)` and takes a `state: dict[str, Any]` as its
  last parameter: what it wrote there last time. Write only the handful of things the next
  run needs (a round counter, spent tokens), and `state.clear()` when the run is over --
  what is over is not picked up.

## Every generated loop has its own bound

The run's allowance is humanize's and is declared with `@flow(budget=Allowance(...))`; it is
not a loop setting for a generated flow to implement. A turn taken once that allowance is
spent raises `Stopped`, which ends the run, but the compiler still refuses an
`unbounded-loop` warning. Give every generated loop an explicit bound of its own, normally a
round cap:

```python
for round_ in range(held.rounds):
    print(f"round {round_ + 1}")
    agent(task, suppress=True)
    time.sleep(5)
```

- Declare a run allowance with `@flow(budget=Allowance(tokens=10.0))` when the flow needs a
  default, and let the run or its caller override it.
- A round cap is `for round_ in range(held.rounds):` -- a `for` over a `range` is bounded by
  construction and is the preferred backstop for AOT-generated loops.
- A loop that only sleeps, or has no `break`/`return`/`raise` at all, is refused outright.

## Every shaped answer is guarded

A turn held to a shape answers `None` when it fails, and `suppress=True` is how a loop
survives a failed turn instead of dying on it:

```python
review = agents.reviewer(prompt, suppress=True, schema=Review)
if review is not None and review.done:
    return
```

Never read a field off a shaped answer something has not tested. A plain turn under
`suppress=True` answers `""` on failure -- test it too, and take the round again rather
than advancing past a turn that never landed.

A shaped answer's model declares every field required -- no defaults. Some backends hold
the model to a strict schema that refuses a field with a default, and a flow's shapes must
work on any backend that shapes. (A *config* model is the opposite: every field carries a
default, since a flow runs unset.)

## Settings

A flow that takes settings declares a pydantic model:

```python
class Config(BaseModel):
    model_config = {"extra": "forbid"}

    rounds: int = Field(
        default=6,
        ge=1,
        description="maximum rounds before the loop stops",
    )
```

`extra: "forbid"` (or `frozen: True`), and every field carries a `Field(description=...)`
-- the descriptions are what whoever sets the flow up is shown.

## What the flow says about itself

The module docstring's first line is what every list of flows shows; under it goes the
`hmz exec` line that runs it, then prose saying how the flow works and what ends it.
`print()` progress as the loop goes -- which round, what has been spent -- because a run is
watched from its transcript.

## What each agent is

- A session held across turns remembers; `agent(task)` alone is a fresh session per turn
  and remembers nothing. Choose deliberately per seat.
- A reviewer that must arrive fresh gets a new session (or a bare `agent(...)` call) each
  round, so it reads the repository rather than its own last review.
- The person at the prompt is a `Person` seat. Asking them stops the turn until they
  answer; run where nobody is, they answer nothing -- so a flow written to stop on nothing
  stops. Never loop on asking them without a bound.
- A capability only some backends serve -- a moment, the goal feature -- is declared on the
  seat (`Annotated[Agent, Moment.PERMISSION_REQUEST]`, `Annotated[Agent, Goal]`), so an
  unfit agent is refused before the first turn.

## Rest between rounds

`time.sleep(5)` at the foot of a loop, so a loop that is spinning on failures does not
hammer anything. The proof's world sleeps for free, so this costs the proof nothing.
