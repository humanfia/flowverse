"""What both flows do around their turns: start, cadence, one guarded coding turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hmz.flows import Question, Stopped

from .config import Config
from .guard import guarded
from .storage import open_store
from .tree import ensure_manifest, footprint, manifest_path

#: Coding turns in a row that may answer with nothing before the loop gives up. A turn
#: that failed spends nothing, so a token allowance never ends a loop whose account was
#: refused; three rather than one, because one empty answer is not a reason to stop.
STALLED = 3
#: Past either, a workspace is worth asking about before every epoch copies it aside.
FILES_WARNING = 5_000
BYTES_WARNING = 1024**3
_START = "Start anyway"
_ACCEPTED = frozenset({"a", "1", "y", "yes", "是", "继续", _START.casefold()})


class LargeWorkspace(Stopped):
    """Startup ended at the large-workspace question, before anything was touched."""


def confirm_size(flow: str, root: Path, held: Config, human: Any) -> None:
    """Warn about a workspace too large to copy aside every epoch, and ask about it."""
    files, size = footprint(root)
    if files <= FILES_WARNING and size <= BYTES_WARNING:
        return
    warning = (
        f"WARNING: {root} holds {files:,} files and {size / 1024**2:,.0f} MiB that"
        " git does not ignore (.git included); every cleaning epoch copies them aside"
        f" as its revert point. Warning thresholds: {FILES_WARNING:,} files or"
        f" {BYTES_WARNING // 1024**3} GiB. Nothing has been touched yet."
    )
    print(warning)
    if not held.confirm_large_workspace_copies:
        print("confirm_large_workspace_copies is off; starting anyway")
        return
    asked = getattr(human, "asked", None)
    answer = (
        asked(
            Question(
                text=f"{warning}\n\nStart {flow} anyway?", options=(_START, "Stop")
            )
        )
        if callable(asked)
        else None
    )
    if not isinstance(answer, str) or answer.strip().casefold() not in _ACCEPTED:
        print(
            f"{flow} did not start; add .gitignore rules, or set"
            " confirm_large_workspace_copies: false to start without asking"
        )
        raise LargeWorkspace(f"{flow}: large workspace, start not confirmed")


def start(
    flow: str, root: Path, kept: dict[str, Any], held: Config, human: Any
) -> tuple[Path, set[str]]:
    """Open or reopen the run's storage and its task manifest."""
    if not kept:
        confirm_size(flow, root, held, human)
    store, resumed = open_store(flow, root, kept)
    if kept.get("manifest_ready") is True:
        path = manifest_path(store)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"task manifest is missing or linked: {path}")
    else:
        if resumed and kept.get("turns"):
            raise RuntimeError("resumable cleanup state lost its task manifest")
        ensure_manifest(root, store)
        kept["manifest_ready"] = True
    kept.setdefault("turns", 0)
    kept.setdefault("epoch", 0)
    manifest = ensure_manifest(root, store)
    print(
        f"{flow} in {root}: {len(manifest)} task file(s) in the manifest, turn"
        f" {kept['turns'] + 1}, epoch {kept['epoch']}; run storage at {store}"
    )
    return store, manifest


def due(held: Config, kept: dict[str, Any]) -> bool:
    """Whether enough coding turns have landed since the last epoch."""
    return (
        bool(held.cleanup_turns) and kept["turns"] // held.cleanup_turns > kept["epoch"]
    )


def coding_turn(agent: Any, task: str, root: Path, held: Config, label: str) -> bool:
    """One fresh-session turn on the task; whether it landed.

    A turn that answered landed, and so did one the clock ended: its edits are on disk,
    and taking it again would refill a limit it already reached.
    """
    session = agent.new(cwd=str(root))
    with guarded(
        session,
        session_timeout_minutes=held.session_timeout_minutes,
        idle_timeout_minutes=held.idle_timeout_minutes,
        stop_grace_minutes=held.stop_grace_minutes,
        label=label,
    ) as watch:
        said = session(task, suppress=True)
    return bool(said) or watch.timed_out
