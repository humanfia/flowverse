"""Loads a drafted flow through humanize's engine and runs it on fakes, in one world.

    python smoke.py <draft> [<world> <seconds>]

Run by the compiler in a process of its own, so that a draft that never yields can be killed
and a draft's modules never outlive its check. With no world it only loads the draft; with
one it also runs it once, every agent answering as that world does, under a budget of its
turns and seconds. Sleeping is free; blocking the event loop and starting programs on this
machine are refused. Whatever the draft prints is dropped, and the one line on stdout is the
JSON of what was found.
"""

import asyncio
import collections.abc
import contextlib
import datetime
import enum
import json
import os
import subprocess
import sys
import time
import traceback
import types
import typing
from pathlib import Path
from typing import Any

if sys.path and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
    del sys.path[0]

import pydantic
from hmz.flows import (
    Budget,
    BudgetExceeded,
    EnvError,
    FlowNotFound,
    FlowRuntimeError,
    HarnessKind,
    ParamsError,
)
from hmz.runtime.flowing.fakes import FakeAgentDriver, FakeEnvDriver, run_fake
from hmz.runtime.flowing.loading import module_of, pick

TASK = "make the repository's test suite pass"
TURNS = 1000
DONE = frozenset(
    {"accept", "accepted", "approve", "approved", "complete", "completed", "done",
     "finished", "lgtm", "ok", "pass", "passed", "settled", "success", "yes"}
)
SAID = {"never-done": "still working on it", "always-done": "done", "silent": ""}
RAN = {"never-done": (1, "", "still failing\n"), "always-done": (0, "ok\n", ""),
       "silent": (0, "", "")}
ANSWERED_BY_DEFAULT = frozenset({"cat", "ls", "echo", "true", "false", ":", "sleep"})
SEQUENCES = (list, tuple, set, frozenset, collections.abc.Sequence, collections.abc.Set)
MAPPINGS = (dict, collections.abc.Mapping)

class Forbidden(RuntimeError):
    pass


BUGS = (
    Forbidden,
    FlowRuntimeError,
    NameError,
    AttributeError,
    TypeError,
    ImportError,
    LookupError,
)
ALONE = (EnvError, FlowNotFound)


def main(argv: list[str]) -> None:
    draft = Path(argv[1])
    world = argv[2] if len(argv) > 2 else ""
    seconds = float(argv[3]) if len(argv) > 3 else 60.0
    report: dict[str, Any] = {"agents": [], "findings": []}
    _guarded()
    with (
        open(os.devnull, "w") as dropped,
        contextlib.redirect_stdout(dropped),
        contextlib.redirect_stderr(dropped),
    ):
        flow = _loaded(draft, report, quiet=bool(world))
        if flow is not None and world:
            asyncio.run(_smoked(flow, draft, world, seconds, report))
    print(json.dumps(report))


def _found(report: dict[str, Any], severity: str, code: str, said: str) -> None:
    report["findings"].append({"severity": severity, "code": code, "said": said})


def _at(error: BaseException, draft: Path) -> str:
    cause = error.__cause__ or error
    if isinstance(cause, SyntaxError) and cause.filename:
        return f" (at {Path(cause.filename).name}:{cause.lineno})"
    inside = [
        frame
        for frame in traceback.extract_tb(cause.__traceback__)
        if Path(frame.filename).is_relative_to(draft)
    ]
    if not inside:
        return ""
    return f" (at {Path(inside[-1].filename).relative_to(draft)}:{inside[-1].lineno})"


def _guarded() -> None:
    sleep = asyncio.sleep

    async def instant(delay: float, result: Any = None) -> Any:
        del delay
        return await sleep(0, result)

    def blocking(*_: Any, **__: Any) -> Any:
        raise Forbidden("time.sleep blocks the event loop: await asyncio.sleep(...)")

    def starting(*_: Any, **__: Any) -> Any:
        raise Forbidden(
            "a flow starts no program itself: await env.exec([...]) on an environment "
            "declared with ShellEnvMixin"
        )

    asyncio.sleep = instant  # type: ignore[assignment]
    time.sleep = blocking
    subprocess.Popen = starting  # type: ignore[assignment, misc]
    os.system = starting


def _loaded(draft: Path, report: dict[str, Any], *, quiet: bool) -> Any:
    try:
        flow = pick(module_of(draft / "__init__.py", None), "", str(draft))
        declared = flow.describe()
    except Exception as error:  # noqa: BLE001 -- whatever loading the draft raised
        _found(report, "error", "load",
               f"{type(error).__name__}: {error}{_at(error, draft)}")
        return None
    report["agents"] = [
        {"name": role.name, "person": role.auto} for role in declared.agents
    ]
    try:
        flow.params_of({})
    except ParamsError as error:
        _found(report, "error", "params", "every param needs a default, since `hmz exec` "
               f"runs the flow with none set: {error}")
        return None
    if not quiet and flow.name != draft.name:
        _found(report, "warning", "entry-name",
               f"the entry flow is called {flow.name!r}, not after its directory "
               f"{draft.name!r}: a bare ref finds it only while it is the one visible flow")
    return flow


async def _smoked(
    flow: Any, draft: Path, world: str, seconds: float, report: dict[str, Any]
) -> None:
    declared = flow.describe()
    commands = _commands(world)
    agents = {
        role.name: FakeAgentDriver(role.harness or HarnessKind.CLAUDE, reply=_reply(world))
        for role in declared.agents
        if not role.auto
    }
    envs = {
        role.name: FakeEnvDriver(
            workdir=f"/{role.name}",
            cpu_count=max(role.cpu_count, 8),
            memory=max(role.memory, 64 << 30),
            gpu_count=role.gpu_count,
            gpu_memory=role.gpu_memory,
            run=commands,
        )
        for role in declared.envs
        if not role.auto
    }
    here = [role for role in declared.envs if role.auto]
    local = FakeEnvDriver(
        workdir="/here",
        cpu_count=max([8, *(role.cpu_count for role in here)]),
        memory=max([64 << 30, *(role.memory for role in here)]),
        gpu_count=max([0, *(role.gpu_count for role in here)]),
        gpu_memory=max([0, *(role.gpu_memory for role in here)]),
        run=commands,
    )
    budget = Budget(duration=datetime.timedelta(seconds=seconds), output_tokens=TURNS)
    try:
        await run_fake(flow, TASK, agents=agents, envs=envs, budget=budget, local=local)
    except BudgetExceeded as error:
        _found(report, "error", "unbounded",
               f"under {world}, it did not end on its own: the smoke's budget of {TURNS} "
               f"turns and {seconds:g}s stopped it ({error}) -- give every loop a bound "
               "of its own")
    except ALONE as error:
        _found(report, "warning", "alone",
               f"under {world}, it raised {type(error).__name__}: {error}"
               f"{_at(error, draft)} -- the smoke runs it alone, in an empty workspace "
               "where no agent writes a file and no other flow is beside it; make sure "
               "that is only the smoke's doing")
    except BUGS as error:
        _found(report, "error", "raised",
               f"under {world}, it raised {type(error).__name__}: {error}"
               f"{_at(error, draft)}")
    except Exception as error:  # noqa: BLE001 -- how a draft ends is what is reported
        _found(report, "error" if world == "always-done" else "warning", "raised",
               f"under {world}, it ended by raising {type(error).__name__}: {error}"
               f"{_at(error, draft)}, rather than returning")


def _reply(world: str) -> Any:
    def reply(prompt: str, *, output_schema: Any = None, **_: Any) -> Any:
        del prompt
        if output_schema is None:
            return SAID[world]
        return _answer(output_schema, world)

    return reply


def _commands(world: str) -> Any:
    def run(command: Any, env: Any) -> Any:
        del env
        if not isinstance(command, str) and command and command[0] in ANSWERED_BY_DEFAULT:
            return None
        return RAN[world]

    return run


def _answer(schema: type[pydantic.BaseModel], world: str) -> pydantic.BaseModel:
    values = {
        name: _value(field.annotation, world) for name, field in schema.model_fields.items()
    }
    try:
        return schema.model_validate(values)
    except pydantic.ValidationError:
        return schema.model_construct(**values)


def _value(annotation: Any, world: str) -> Any:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if annotation is bool:
        return world == "always-done"
    if annotation is str:
        return {"never-done": "not yet: keep going", "always-done": "done",
                "silent": ""}[world]
    if annotation in (int, float):
        return 0
    if origin is typing.Literal:
        return _pick(list(args), world)
    if origin is typing.Annotated:
        return _value(args[0], world)
    if origin in (typing.Union, types.UnionType):
        chosen = [one for one in args if one is not type(None)]
        if not chosen or (world == "silent" and len(chosen) < len(args)):
            return None
        return _value(chosen[0], world)
    if origin in SEQUENCES or annotation in SEQUENCES:
        return []
    if origin in MAPPINGS or annotation in MAPPINGS:
        return {}
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return annotation(_pick([one.value for one in annotation], world))
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return _answer(annotation, world)
    return None


def _pick(options: list[Any], world: str) -> Any:
    done = [one for one in options if str(one).lower() in DONE]
    other = [one for one in options if str(one).lower() not in DONE]
    wanted = done if world == "always-done" else other
    return (wanted or options)[0]


if __name__ == "__main__":
    main(sys.argv)
