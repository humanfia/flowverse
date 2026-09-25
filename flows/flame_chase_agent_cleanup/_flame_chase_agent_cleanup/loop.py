from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any, Literal

from hmz.flows import FlowContext, FlowException, FlowParams, OutputSchemaError, flow
from pydantic import BaseModel, Field, field_validator

from .cleaning import clean_epoch
from .config import Config
from .guard import guarded, limits, rest
from .roles import Cleaner, Coder, Here
from .storage import open_store
from .tree import ensure_manifest, footprint, kind, manifest_path

STALLED = 3
FILES_WARNING = 5_000
BYTES_WARNING = 1024**3
_START = "Start anyway"
_ACCEPTED = frozenset({"a", "1", "y", "yes", "是", "继续", _START.casefold()})


class LargeWorkspace(FlowException):
    pass


class Start(BaseModel):
    answer: Literal["Start anyway", "Stop"] = Field(
        default="Stop", description="Start anyway, or stop here"
    )

    @field_validator("answer", mode="before")
    @classmethod
    def _read(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return _START if value.strip().casefold() in _ACCEPTED else "Stop"


class Turn(FlowParams):
    held: Config
    label: str


class Epoch(FlowParams):
    held: Config
    store: str
    epoch: int


async def confirm_size(flow: str, env: Any, held: Config, human: Any) -> None:
    files, size = await footprint(env)
    if files <= FILES_WARNING and size <= BYTES_WARNING:
        return
    warning = (
        f"WARNING: {env.workdir} holds {files:,} files and {size / 1024**2:,.0f} MiB that"
        " git does not ignore (.git included); every cleaning epoch copies them aside"
        f" as its revert point. Warning thresholds: {FILES_WARNING:,} files or"
        f" {BYTES_WARNING // 1024**3} GiB. Nothing has been touched yet."
    )
    print(warning)
    if not held.confirm_large_workspace_copies:
        print("confirm_large_workspace_copies is off; starting anyway")
        return
    session = await human.spawn(env=env)
    try:
        said = await human.run(
            f"{warning}\n\nStart {flow} anyway?", session=session, output_schema=Start
        )
    except OutputSchemaError:
        said = Start()
    if said.answer != _START:
        print(
            f"{flow} did not start; add .gitignore rules, or pass"
            " -p confirm_large_workspace_copies=false to start without asking"
        )
        raise LargeWorkspace(f"{flow}: large workspace, start not confirmed")


async def start(
    flow: str, env: Any, kept: Any, held: Config, human: Any
) -> PurePosixPath:
    if "run_id" not in kept:
        await confirm_size(flow, env, held, human)
    store, resumed = await open_store(flow, env, kept)
    path = manifest_path(store)
    if "manifest_ready" in kept and kept["manifest_ready"] is True:
        if await kind(env, path) != "f":
            raise RuntimeError(f"task manifest is missing or linked: {path}")
    elif resumed and "turns" in kept and kept["turns"]:
        raise RuntimeError("resumable cleanup state lost its task manifest")
    manifest = await ensure_manifest(env, store)
    kept["manifest_ready"] = True
    for key in ("turns", "epoch", "cleaned_at"):
        if key not in kept:
            kept[key] = 0
    print(
        f"{flow} in {env.workdir}: {len(manifest)} task file(s) in the manifest, turn"
        f" {kept['turns'] + 1}, epoch {kept['epoch']}; run storage at {store}"
    )
    return store


def due(held: Config, kept: Any) -> bool:
    return (
        bool(held.cleanup_turns)
        and kept["turns"] - kept["cleaned_at"] >= held.cleanup_turns
    )


async def coding_turn(
    agent: Any, task: str, env: Any, held: Config, label: str
) -> bool:
    session = await agent.spawn(env=env)
    said, timed_out = await guarded(agent, session, task, **limits(held, label))
    return bool(said) or timed_out


@flow(agents=Coder, envs=Here, params=Turn, hidden=True)
async def turn(
    task: str, *, agents: Coder, envs: Here, params: Turn, ctx: FlowContext
) -> bool:
    return await coding_turn(
        agents["coder"], task, envs["workspace"], params.held, params.label
    )


@flow(agents=Cleaner, envs=Here, params=Epoch, hidden=True)
async def epoch(
    task: str, *, agents: Cleaner, envs: Here, params: Epoch, ctx: FlowContext
) -> None:
    env = envs["workspace"]
    store = PurePosixPath(params.store)
    if await kind(env, manifest_path(store)) != "f":
        raise RuntimeError(
            f"task manifest is missing or linked: {manifest_path(store)}"
        )
    manifest = await ensure_manifest(env, store)
    await clean_epoch(
        agents["cleaner"], params.held, env, manifest, store, params.epoch
    )


def _spent(ctx: Any) -> tuple[Any, ...]:
    usage = ctx.usage
    return usage.duration, usage.cost, usage.output_tokens


async def drive(
    flow: str,
    coders: Sequence[Any],
    cleaner: Any,
    human: Any,
    task: str,
    held: Config,
    ctx: Any,
    env: Any,
) -> None:
    kept = ctx.state
    store = await start(flow, env, kept, held, human)
    stalled = 0
    while True:
        if due(held, kept):
            await epoch(
                task,
                agents={"cleaner": cleaner},
                envs={"workspace": env},
                params=Epoch(held=held, store=str(store), epoch=kept["epoch"] + 1),
            )
            kept["epoch"] += 1
            kept["cleaned_at"] = kept["turns"]
            continue
        seat = kept["turns"] % len(coders)
        who = f"chaser {seat + 1} " if len(coders) > 1 else ""
        label = f"{who}turn {kept['turns'] + 1}"
        before = _spent(ctx)
        kept["turns"] += 1
        try:
            landed = await turn(
                task,
                agents={"coder": coders[seat]},
                envs={"workspace": env},
                params=Turn(held=held, label=label),
            )
        except BaseException:
            if _spent(ctx) == before:
                kept["turns"] -= 1
            raise
        if not landed:
            kept["turns"] -= 1
            stalled += 1
            print(f"{label} answered nothing or failed; taking it again")
            if stalled >= STALLED:
                print(f"stopping: {stalled} turns in a row came to nothing")
                return
            await rest()
            continue
        stalled = 0
        done_by = f" by chaser {seat + 1}" if len(coders) > 1 else ""
        print(f"turn {kept['turns']} done{done_by} | epoch {kept['epoch']}")
        await rest()
