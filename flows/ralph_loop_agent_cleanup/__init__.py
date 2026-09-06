"""A fresh-session Ralph loop whose periodic cleanup is performed by an agent.

    hmz exec -f ralph_loop_agent_cleanup -a claude -a claude \
        -c cleanup.yaml "improve the project"

The first configured agent gets a fresh session for every Ralph turn.  After each
configured number of landed turns, the second configured agent gets a fresh
cleaning session in the same workspace.  The cleaner is responsible for deciding
what task work is worth keeping; the flow only measures its result, retries
overages, runs the optional check, and restores the revert point on failure.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import shutil
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from _workspace_cleanup_watchdog import run_guarded
from flame_chase_agent_cleanup import (
    Cleaned,
    Measure,
    _clean_epoch,
    _ensure_manifest,
    _manifest_path,
    _validate_work_paths,
)
from hmz.flows import Agent, flow, home
from pydantic import BaseModel, Field, field_validator

FLOW_NAME = "ralph_loop_agent_cleanup"


class Agents(NamedTuple):
    """The Ralph coder and the user-configured cleanup agent."""

    agent: Agent
    cleaner: Agent


class Config(BaseModel):
    """Ralph budget and the cleaner's workspace policy."""

    model_config = {"extra": "forbid"}

    budget: float = Field(
        default=10.0,
        ge=0,
        description="millions of output tokens before the loop stops",
    )
    cleanup_turns: int = Field(
        default=3,
        ge=0,
        description="completed turns between cleanups; 0 disables cleanup",
    )
    work_paths: tuple[str, ...] = Field(
        min_length=1,
        description="relative, non-overlapping paths where task work may remain",
    )
    next_lines: int = Field(
        default=10,
        ge=1,
        description="the most lines NEXT.md may hold",
    )
    comment_lines: int = Field(
        default=30,
        ge=0,
        description="comment-line cap under configured work paths",
    )
    repairs: int = Field(
        default=2,
        ge=0,
        description="cleaner repair turns allowed for measured overages",
    )
    check_command: str = Field(
        default="",
        description="optional correctness check after cleaning",
    )
    session_timeout_minutes: float = Field(
        default=240.0,
        ge=0,
        description="minutes per session before a forced wrap-up prompt; 0 disables it",
    )
    idle_timeout_minutes: float = Field(
        default=10.0,
        ge=0,
        description="minutes without token usage increasing before a reminder; 0 disables it",
    )
    stop_grace_minutes: float = Field(
        default=10.0,
        ge=0,
        description="minutes after the wrap-up prompt before the session is closed",
    )

    @field_validator("work_paths")
    @classmethod
    def validate_work_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_work_paths(value)


def _workspace_key(source: Path) -> str:
    plain = "".join(
        character if character.isalnum() else "-" for character in str(source)
    )
    readable = "-".join(part for part in plain.split("-") if part)[-80:] or "root"
    digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:12]
    return f"{readable}-{digest}"


def _managed_parent(source: Path) -> Path:
    parent = (home() / FLOW_NAME / _workspace_key(source)).resolve()
    if parent.is_relative_to(source):
        raise RuntimeError("HUMANIZE_HOME must sit outside the cleaned repository")
    return parent


def _open_store(source: Path, state: dict[str, Any]) -> tuple[Path, bool]:
    parent = _managed_parent(source)
    run_id = state.get("run_id")
    run_root = state.get("run_root")
    if run_id is not None or run_root is not None:
        if not isinstance(run_id, str) or not run_id or not isinstance(run_root, str):
            raise ValueError("resumable cleanup state has incomplete run storage")
        raw = Path(run_root)
        root = raw.resolve()
        if root.parent != parent or root.name != run_id:
            raise ValueError("resumable cleanup storage is outside HUMANIZE_HOME")
        if raw.is_symlink() or not root.is_dir():
            raise RuntimeError("resumable cleanup storage is missing or linked")
        return root, True
    if state:
        raise ValueError("resumable cleanup state predates managed run storage")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{uuid.uuid4().hex[:10]}"
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / run_id
    root.mkdir()
    state.update(run_id=run_id, run_root=str(root), manifest_ready=False)
    return root, False


def _remove_store(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"cleanup storage was replaced or linked: {root}")
    shutil.rmtree(root)


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Run Ralph turns and hand each due cleanup to ``agents.cleaner``."""
    held = config or Config()
    kept = state if state is not None else {}
    workdir = Path.cwd().resolve()
    store, resumed = _open_store(workdir, kept)
    turns = int(kept.get("turns", 0))
    epoch = int(kept.get("epoch", 0))
    before = int(kept.get("spent", 0))

    if kept.get("manifest_ready") is True:
        manifest_path = _manifest_path(store)
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise RuntimeError(f"task manifest is missing or linked: {manifest_path}")
    else:
        if resumed and turns:
            raise RuntimeError("resumable cleanup state lost its task manifest")
        _ensure_manifest(workdir, store)
        kept["manifest_ready"] = True
    manifest = _ensure_manifest(workdir, store)

    def spent_all() -> int:
        return (
            before
            + int(agents.agent.spent().output)
            + int(agents.cleaner.spent().output)
        )

    def over_budget() -> bool:
        kept["spent"] = spent = spent_all()
        return spent >= held.budget * 1_000_000

    if before >= held.budget * 1_000_000:
        _remove_store(store)
        kept.clear()
        return

    while True:
        kept["spent"] = spent = spent_all()
        if spent >= held.budget * 1_000_000:
            _remove_store(store)
            kept.clear()
            return
        if (
            held.cleanup_turns > 0
            and turns > 0
            and turns % held.cleanup_turns == 0
            and epoch < turns // held.cleanup_turns
        ):
            _clean_epoch(
                agents.cleaner,
                held,
                workdir,
                manifest,
                store,
                epoch + 1,
                over_budget,
            )
            epoch += 1
            kept["epoch"] = epoch
            continue

        session = agents.agent.new(cwd=str(workdir))
        landed = run_guarded(
            session,
            partial(session, task, suppress=True),
            session_timeout_minutes=held.session_timeout_minutes,
            idle_timeout_minutes=held.idle_timeout_minutes,
            stop_grace_minutes=held.stop_grace_minutes,
            label=f"turn {turns + 1}",
        )
        del session
        kept["spent"] = spent_all()
        if landed:
            turns += 1
            kept["turns"] = turns
            print(
                f"turn {turns}: epoch {epoch}, {kept['spent'] / 1_000_000:.2f}M spent"
            )
        else:
            print("turn did not land; taking it again")
        time.sleep(5)


__all__ = ["Agents", "Cleaned", "Config", "Measure", "run"]
