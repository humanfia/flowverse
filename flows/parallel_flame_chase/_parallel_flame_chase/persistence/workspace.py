from __future__ import annotations

import contextlib
import json
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from hmz.flows import EnvError, TempCloneBusy

from ..core.models import LANES, Deliverable, LaneName
from ..core.utils import json_bytes, now
from .probe import REFUSED, RunPaths, WorkspaceStats

PROBE = Path(__file__).with_name("probe.py")
LOCK_SCRATCH = "parallel_flame_chase-lock"
LOCK_OWNER = "owner"

__all__ = [
    "PROBE",
    "ProbeFailed",
    "RunPaths",
    "SourceLock",
    "WorkspaceStats",
    "append_record",
    "checkpoint_state",
    "commit_files",
    "initialize_run",
    "inspect_workspace",
    "snapshot",
    "validate_deliverable",
    "validate_layout",
]


class ProbeFailed(OSError):
    """The probe could not be run, or did not answer."""


class SourceLock:
    """One Lane 1 owner per source workspace, across processes.

    The lock is a temporary copy of a small scratch directory kept beside the source: the
    environment lets one holder at a time take a copy's id, and lets it go when the holder's
    process dies.
    """

    def __init__(self, workspace: Any, run_id: str) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self._home: Any | None = None

    async def acquire(self) -> None:
        home = await self.workspace.derive_scratch(LOCK_SCRATCH)
        try:
            owner = await home.derive_temp_clone(LOCK_OWNER)
        except TempCloneBusy as why:
            raise RuntimeError(
                f"another parallel Flame Chase owns source workspace "
                f"{self.workspace.workdir}"
            ) from why
        self._home = home
        await owner.write(
            "owner.json",
            json_bytes(
                {
                    "version": 1,
                    "run_id": self.run_id,
                    "source": str(self.workspace.workdir),
                    "acquired_at": now(),
                }
            ),
        )

    async def release(self) -> None:
        home, self._home = self._home, None
        if home is not None:
            with contextlib.suppress(Exception):
                await home.destroy_temp_clone(LOCK_OWNER)

    async def __aenter__(self) -> Self:
        await self.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.release()


async def _probe(env: Any, *argv: str) -> Any:
    try:
        code, out, err = await env.exec(
            [sys.executable, "-I", str(PROBE), *argv], timeout=0
        )
    except EnvError as why:
        raise ProbeFailed(f"the run probe could not start: {why}") from why
    if code == REFUSED:
        raise ValueError(err.strip() or "the run probe refused")
    if code != 0:
        raise ProbeFailed(f"the run probe failed ({code}): {err.strip()[:1000]}")
    try:
        return json.loads(out)
    except ValueError as why:
        raise ProbeFailed(f"the run probe answered {out[:200]!r}") from why


async def inspect_workspace(env: Any) -> WorkspaceStats:
    said = await _probe(env, "stats", str(env.workdir))
    return WorkspaceStats(
        regular_files=int(said["regular_files"]), total_bytes=int(said["total_bytes"])
    )


async def initialize_run(
    env: Any,
    paths: RunPaths,
    lanes: tuple[LaneName, ...] = LANES,
    *,
    fresh: bool,
    size: int | None = None,
) -> None:
    try:
        await _probe(
            env,
            "init",
            str(paths.root),
            str(env.workdir),
            "fresh" if fresh else "resume",
            "-" if size is None else str(size),
            *lanes,
        )
    except ValueError as why:
        raise RuntimeError(str(why)) from why


async def snapshot(env: Any, destination: Path) -> None:
    await _probe(env, "snapshot", str(env.workdir), str(destination))


async def validate_layout(
    env: Any, paths: RunPaths, lanes: tuple[LaneName, ...] = LANES
) -> None:
    try:
        await _probe(env, "layout", str(paths.root), ",".join(lanes))
    except ValueError as why:
        raise RuntimeError(str(why)) from why


async def _stage(env: Any, paths: RunPaths, data: bytes) -> Path:
    staged = paths.staging / f"{uuid.uuid4().hex}.part"
    await env.write(str(staged), data)
    return staged


async def commit_files(
    env: Any,
    paths: RunPaths,
    lanes: tuple[LaneName, ...],
    files: Mapping[Path, bytes],
) -> None:
    """Replaces runtime files whole, never writing through a link planted in their place."""
    argv: list[str] = []
    for target, data in files.items():
        argv += [str(await _stage(env, paths, data)), str(target)]
    try:
        await _probe(env, "commit", str(paths.root), ",".join(lanes), *argv)
    except ValueError as why:
        raise RuntimeError(str(why)) from why


async def append_record(
    env: Any,
    paths: RunPaths,
    lanes: tuple[LaneName, ...],
    log: Path,
    record: bytes,
) -> None:
    """Appends one record to a report log that is not a link."""
    staged = await _stage(env, paths, record)
    try:
        await _probe(
            env, "append", str(paths.root), ",".join(lanes), str(staged), str(log)
        )
    except ValueError as why:
        raise RuntimeError(str(why)) from why


async def validate_deliverable(
    env: Any,
    root: Path,
    deliverable: Deliverable,
) -> list[dict[str, object]]:
    described = await _probe(
        env, "artifacts", str(root), *(item.path for item in deliverable.artifacts)
    )
    artifacts: list[dict[str, object]] = []
    seen: set[str] = set()
    for declared, found in zip(deliverable.artifacts, described, strict=True):
        canonical = found["path"]
        if canonical in seen:
            raise ValueError(f"deliverable repeats artifact: {canonical}")
        seen.add(canonical)
        artifacts.append(
            {
                "path": canonical,
                "description": declared.description,
                "size": found["size"],
                "sha256": found["sha256"],
            }
        )
    return artifacts


async def checkpoint_state(
    env: Any, path: Path, *, text: bool = True
) -> tuple[str | None, str | None]:
    try:
        said = await _probe(
            env, "checkpoint", str(path), "text" if text else "fingerprint"
        )
    except (OSError, ValueError):
        return None, None
    return said["fingerprint"], said["text"]
