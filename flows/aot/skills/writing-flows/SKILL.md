---
name: writing-flows
description: The contract a humanize flow is written to. Use when writing or repairing a flow -- its shape on disk, its one import, what it declares, how its loops end, how its answers are guarded, and how it says what it is.
---

# Writing a flow

A flow is an async Python function driving coding agents. It declares the agents it drives,
the environments they work in and the params it takes, and humanize hands it exactly those.
Everything below is what the compiler will hold your draft to: it loads the draft through
humanize's engine, runs it on fakes in the worst worlds there are, and hands it to a fresh
critic. Write to it the first time; every refusal costs a repair round.

## The shape on disk

A flow is a directory named for the flow, holding the `__init__.py` that is the flow.
Everything it needs lives inside that directory: helper code as an underscore-named sibling
module or package (`_myflow/`), skills its roles bring as `skills/<name>/SKILL.md`. A flow
whose parts are elsewhere is a flow with a hole in it wherever it is copied to.

Import that sibling by its plain name -- `from _myflow import helpers` -- never relatively
(`from ._myflow import ...`): humanize puts the flow's own directory on `sys.path` and claims
what sits beside `__init__.py` by its plain name, so that is the name it is found by.

## One import

The whole of humanize a flow imports is `hmz.flows`, and only names it offers:

```python
from hmz.flows import Agent, AgentCollection, EnvCollection, FlowContext, FlowParams, LocalEnv, flow
```

Plus the standard library and `pydantic`. Never `hmz.runtime`, `hmz.coganchor` or any other
module of humanize's own -- those move, and a flow is somebody else's repository.

## The entry point

```python
"""Pair loop -- an actor works until a fresh reviewer says the task is done.

    hmz exec -f local/pair_loop -a actor=claude/claude-opus-5:high \
        -a reviewer=codex/gpt-5.6-sol:high -b cost=10 "the task"

The actor works in one session that remembers; a reviewer reads the repository fresh each
round. The reviewer saying done ends it, and the round cap backstops one that never does.
"""

import asyncio

from hmz.flows import (
    Agent, AgentCollection, EnvCollection, FlowContext, FlowParams, LocalEnv,
    OutputSchemaError, flow,
)
from pydantic import BaseModel, Field


class Agents(AgentCollection):
    actor: Agent
    reviewer: Agent


class Envs(EnvCollection):
    workspace: LocalEnv


class Params(FlowParams):
    rounds: int = Field(default=6, ge=1, description="the most rounds before it stops")


class Review(BaseModel):
    model_config = {"extra": "forbid"}

    done: bool = Field(description="true only if the task is completely done")
    notes: str = Field(description="what the actor is told next, word for word")


@flow(agents=Agents, envs=Envs, params=Params)
async def pair_loop(
    task: str, *, agents: Agents, envs: Envs, params: Params, ctx: FlowContext
) -> str:
    """An actor works until a fresh reviewer says the task is done."""
    actor, reviewer = agents["actor"], agents["reviewer"]
    workspace = envs["workspace"]
    working = await actor.spawn(env=workspace)
    prompt = task
    for round_ in range(params.rounds):
        print(f"round {round_ + 1}/{params.rounds}")
        await actor.run(prompt, session=working)
        reading = await reviewer.spawn(env=workspace)
        try:
            review = await reviewer.run(task, session=reading, output_schema=Review)
        except OutputSchemaError:
            continue
        if review.done:
            print(review.notes)
            return review.notes
        prompt = review.notes or task
        await asyncio.sleep(5)
    print(f"stopping: {params.rounds} rounds, and the reviewer never said done")
    return ""
```

- The flow is an `async def` taking `(task, *, agents, envs, params, ctx)`, decorated with
  `@flow(agents=..., envs=..., params=...)`. The entry flow is named after its directory --
  the function's own name, or `name=` -- since that is what `-f <flow>` finds. More flows in
  the same module are subflows: mark them `hidden=True`.
- `agents` is an `AgentCollection` subclass, one key per role, named for what the role is
  for. `-a <role>=<harness>/<model>:<effort>` fills each one.
- `envs` is an `EnvCollection` subclass. The directory the run was started in is the role
  `workspace`, typed as `LocalEnv` or a subclass of it: humanize fills it itself, and nobody
  passes it.
- `params` is a `FlowParams` subclass with a default and a `Field(description=...)` for every
  field -- `hmz exec` runs the flow with none set, and `-p key=value` sets one. A flow that
  takes none says `params=FlowParams`.
- The function's docstring is the one line every list of flows shows. The module docstring
  says the same, with the `hmz exec` line that runs it under it, then how it works and what
  ends it.

## Declare what each role does, and nothing more

A role's type says what the flow will do with it, and the flow is handed an agent or
environment that can do exactly that. Using a capability the role did not declare raises
`CapabilityNotGranted`, whatever the machine or harness underneath could do; declaring one
the flow never uses shuts out every harness that does not serve it. So subclass for exactly
what is used:

```python
class Worker(Agent, GoalCommandAgentMixin):  # `run("/goal ...")`
    _skills = ("review-notes",)  # skills/review-notes/ in this flow's directory


class Workspace(LocalEnv, ShellEnvMixin, FilesEnvMixin):  # `exec([...])`, `read`, `write`
    ...
```

- Agent mixins: `GoalCommandAgentMixin` (`/goal <objective>`), `LoopCommandAgentMixin`
  (`/loop <interval> <task>`), `SteeringAgentMixin` (`steer`), and the hooks only some
  harnesses reach: `PermissionRequestHookAgentMixin`, `SubagentStartHookAgentMixin`,
  `SubagentStopHookAgentMixin`, `AskUserHookAgentMixin`. The hooks every agent has need no
  mixin: `on_session_start`, `on_user_prompt_submit`, `on_pre_tool_use`, `on_notification`,
  `on_stop`, `on_session_end`, each taking an `async def hook(params) -> XHookResult`.
- `_permission = Permission(local=PermissionKind.READ)` narrows what a role may touch -- a
  reviewer that only reads, say. The default may write the workspace, read the rest of the
  machine, and not go online.
- Environment mixins: `ShellEnvMixin` (`await env.exec(["git", "status"])` answering
  `(code, stdout, stderr)`), `BashEnvMixin` (`exec` of a script string too),
  `FilesEnvMixin` (`read`, `write`), `GitWorktreeEnvMixin` (`derive_worktree`),
  `TemporaryClonedDirEnvMixin` (`derive_temp_clone`), `ScratchDirEnvMixin`
  (`derive_scratch`), and the resources `CPUEnvMixin`, `MemoryEnvMixin`, `GPUEnvMixin`.
- The person outside the run is a role typed `Outworlder`, which humanize fills itself.
  Under `hmz exec` nobody is there: a text question answers `""`, and a shaped one answers
  its model's defaults -- so every field of a model put to the person has a default, and
  whatever the defaults mean is a safe answer (no, stop, nothing).

## Sessions

- `session = await agent.spawn(env=envs["workspace"])` opens a conversation;
  `await agent.run(prompt, session=session)` takes one turn and answers its text. A session
  held across turns remembers; a fresh one per turn remembers nothing. Choose deliberately
  per role.
- A reviewer that must arrive fresh gets a new session each round, so it reads the
  repository rather than its own last review.
- `await agent.fork(session, env=...)` branches a conversation; `agent.derive(...)` is the
  same agent with a narrower permission or fewer skills.
- Sessions close by themselves when the flow ends.

## Every loop has its own bound

The budget a run is given -- `-b cost=10`, or what a calling flow passes -- is the runner's.
When it is spent, the next turn raises `BudgetExceeded` and the run ends there. It is a
backstop, not a loop condition: never declare one in the flow, and never write a loop that
only the budget can end. The compiler runs every draft under a budget of its own, and a
draft that runs until that budget stops it is refused, always.

```python
for round_ in range(params.rounds):
    print(f"round {round_ + 1}/{params.rounds}")
    ...
```

A `for` over a `range` is bounded by construction and is the preferred backstop, even where
what ends the loop is a verdict.

## End by returning, in every world

The compiler runs the draft with every agent answering one way throughout: never saying
done, saying done at once, and answering nothing (`""`, and models whose strings are empty
and whose flags are false) -- and every command the flow runs failing in the first world
and succeeding in the other two. In each of them the flow must come to its end and return --
print what happened, and return what the caller might want (the last notes, a path), or
`None`. Raising to report an outcome is not ending well.

Those runs are of the draft alone: its workspace starts empty, no agent writes a file, and
no other flow is beside it. A file the flow reads after an agent was to write it may not be
there -- in the smoke run, or when the agent did not do it -- so catch `EnvFileNotFound` and
take the round again, or tell the agent, rather than letting the run die on it.

## Shaped answers

`await agent.run(prompt, session=s, output_schema=Review)` answers a `Review`. The model's
fields are what the agent is asked for, so each carries a `Field(description=...)` that says
exactly what to put there, and none has a default: some harnesses hold the model to a strict
schema that refuses one. `model_config = {"extra": "forbid"}`.

An agent that answers something the model cannot hold raises `OutputSchemaError`. Where a
loop should survive one bad answer, catch it and take the round again:

```python
try:
    review = await reviewer.run(task, session=reading, output_schema=Review)
except OutputSchemaError:
    continue
```

## Never block the event loop

A flow is async, and so is everything humanize runs beside it.

- Wait with `await asyncio.sleep(5)`, never `time.sleep`.
- Run programs with `await env.exec([...])` on an environment declared with
  `ShellEnvMixin`, never `subprocess` or `os.system`; read and write files with
  `FilesEnvMixin`, or let the agents do it.
- Do things at once with `asyncio.TaskGroup`, never threads.

## Remembering across runs

A flow that can be picked up where it stopped says `@flow(..., resumable=True)`, and
`ctx.state` is then what it keeps: `state["rounds"] = 3`, `state["rounds"]`,
`"rounds" in state`, `del state["rounds"]` -- nothing else, and only values JSON can hold.
Write only the handful of things the next run needs, and delete them when the run is over.
`ctx.resumed` says whether this run picked one up.

## Calling another flow

`load(":review")` is another flow in the same module, marked `hidden=True`;
`load("other_flow")` is one in the same flowverse, which the compiler cannot check beside
the draft -- prefer subflows of the draft's own. Call it as `await review(task,
agents={"reviewer": agents["reviewer"]}, envs=envs, params=ReviewParams())` -- the roles it
declares, each given an agent or environment that can do at least what it asks.

## Rest between rounds

`await asyncio.sleep(5)` at the foot of a loop, so a loop spinning on empty answers does not
hammer anything. The compiler's worlds sleep for free, so this costs its runs nothing.
