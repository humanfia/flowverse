"""The loop both flows run: start, coding turns, and a cleaning epoch when one is due."""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hmz.flows import Question, Stopped

from .cleaning import clean_epoch
from .config import Config
from .guard import guarded, limits
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
    path = manifest_path(store)
    if kept.get("manifest_ready") is True:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"task manifest is missing or linked: {path}")
    elif resumed and kept.get("turns"):
        raise RuntimeError("resumable cleanup state lost its task manifest")
    manifest = ensure_manifest(root, store)
    kept["manifest_ready"] = True
    kept.setdefault("turns", 0)
    kept.setdefault("epoch", 0)
    kept.setdefault("cleaned_at", 0)
    print(
        f"{flow} in {root}: {len(manifest)} task file(s) in the manifest, turn"
        f" {kept['turns'] + 1}, epoch {kept['epoch']}; run storage at {store}"
    )
    return store, manifest


def due(held: Config, kept: dict[str, Any]) -> bool:
    """Whether cleanup_turns coding turns have landed since the last epoch.

    Counted from the last epoch rather than from the first turn, so a run picked up with
    another cleanup_turns cleans once after that many turns rather than catching up.
    """
    return (
        bool(held.cleanup_turns)
        and kept["turns"] - kept["cleaned_at"] >= held.cleanup_turns
    )


def coding_turn(agent: Any, task: str, root: Path, held: Config, label: str) -> bool:
    """One fresh-session turn on the task; whether it landed.

    A turn that answered landed, and so did one the clock ended: its edits are on disk,
    and taking it again would refill a limit it already reached.
    """
    session = agent.new(cwd=str(root))
    with guarded(session, **limits(held, label)) as watch:
        said = session(task, suppress=True)
    return bool(said) or watch.timed_out


def drive(
    flow: str,
    coders: Sequence[Any],
    cleaner: Any,
    human: Any,
    task: str,
    held: Config,
    kept: dict[str, Any],
) -> None:
    """Take turns among the coders, and hand every due epoch to the cleaner.

    Ends by raising `Stopped` once the run's allowance is spent, or by returning after
    STALLED coding turns in a row answered nothing; either way what it kept stays.
    """
    root = Path.cwd().resolve()
    store, manifest = start(flow, root, kept, held, human)
    stalled = 0
    while True:
        if due(held, kept):
            clean_epoch(cleaner, held, root, manifest, store, kept["epoch"] + 1)
            kept["epoch"] += 1
            kept["cleaned_at"] = kept["turns"]
            continue
        seat = kept["turns"] % len(coders)
        who = f"chaser {seat + 1} " if len(coders) > 1 else ""
        label = f"{who}turn {kept['turns'] + 1}"
        if not coding_turn(coders[seat], task, root, held, label):
            stalled += 1
            print(f"{label} answered nothing; taking it again")
            if stalled >= STALLED:
                print(f"stopping: {stalled} turns in a row answered with nothing")
                return
            time.sleep(5)
            continue
        stalled = 0
        kept["turns"] += 1
        done_by = f" by chaser {seat + 1}" if len(coders) > 1 else ""
        print(f"turn {kept['turns']} done{done_by} | epoch {kept['epoch']}")
        time.sleep(5)
