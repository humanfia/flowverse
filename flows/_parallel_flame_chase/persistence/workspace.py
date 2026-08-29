"""Run paths, source ownership, snapshots, and artifact validation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self

from ..core.models import LANES, Deliverable, LaneName
from ..core.utils import now

ARTIFACT_FILE_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WorkspaceStats:
    """The regular files and apparent bytes in one snapshot source."""

    regular_files: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class RunPaths:
    """Every runtime-owned location for one parallel run."""

    root: Path
    source: Path

    @property
    def shared(self) -> Path:
        return self.root / "shared"

    @property
    def private(self) -> Path:
        return self.root / "private"

    @property
    def reports(self) -> Path:
        return self.shared / "reports"

    @property
    def artifacts(self) -> Path:
        return self.shared / "artifacts"

    @property
    def checkpoints(self) -> Path:
        return self.shared / "checkpoints"

    @property
    def planning(self) -> Path:
        return self.shared / "planning-workspace"

    @property
    def manifest(self) -> Path:
        return self.shared / "manifest.json"

    @property
    def state_mirror(self) -> Path:
        return self.shared / "state.json"

    @property
    def workspace_map(self) -> Path:
        return self.shared / "workspace-map.json"

    @property
    def leaderboard(self) -> Path:
        return self.shared / "leaderboard.json"

    def workspace(self, lane: LaneName) -> Path:
        return self.source if lane == "lane-1" else self.private / lane

    def artifact_root(self, lane: LaneName) -> Path:
        return self.artifacts / lane

    def checkpoint(self, lane: LaneName) -> Path:
        return self.checkpoints / f"{lane}.json"


class SourceLock:
    """An advisory process lock preventing two integration lanes in one source."""

    def __init__(self, path: Path, source: Path, run_id: str) -> None:
        self.path = path
        self.source = source
        self.run_id = run_id
        self._handle: BinaryIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":  # pragma: no cover - supported agents are POSIX
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as why:
            handle.close()
            raise RuntimeError(
                f"another parallel Flame Chase owns source workspace {self.source}"
            ) from why
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "version": 1,
                    "pid": os.getpid(),
                    "run_id": self.run_id,
                    "source": str(self.source),
                    "acquired_at": now(),
                }
            ).encode()
        )
        handle.flush()
        self._handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        if os.name == "nt":  # pragma: no cover
            import msvcrt

            handle.seek(0)
            with contextlib.suppress(OSError):
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def inspect_workspace_stats(source: Path) -> WorkspaceStats:
    """Validate a snapshot source and return its regular-file count and size."""
    regular_files = 0
    total = 0
    for folder, directories, files in os.walk(source):
        for name in [*directories, *files]:
            path = Path(folder, name)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                regular_files += 1
                total += info.st_size
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError(f"workspace contains unsupported special file: {path}")
    return WorkspaceStats(regular_files=regular_files, total_bytes=total)


def inspect_workspace(source: Path) -> int:
    """Validate a snapshot source and return its regular-file size."""
    return inspect_workspace_stats(source).total_bytes


def snapshot(source: Path, destination: Path, size: int | None = None) -> None:
    """Create an independent snapshot, preferring a filesystem COW clone."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (copy := shutil.which("cp")) is not None:
        destination.mkdir()
        try:
            subprocess.run(
                [
                    copy,
                    "--archive",
                    "--reflink=auto",
                    f"{source}{os.sep}.",
                    str(destination),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            shutil.rmtree(destination, ignore_errors=True)
        else:
            return
    apparent = inspect_workspace(source) if size is None else size
    if shutil.disk_usage(destination.parent).free < apparent:
        raise OSError(f"not enough free space to snapshot {source}")
    shutil.copytree(source, destination, symlinks=True)


def initialize_paths(paths: RunPaths, *, make_snapshots: bool) -> None:
    """Create one run layout and its private research snapshots."""
    paths.reports.mkdir(parents=True, exist_ok=True)
    paths.checkpoints.mkdir(parents=True, exist_ok=True)
    paths.private.mkdir(parents=True, exist_ok=True)
    for lane in LANES:
        paths.artifact_root(lane).mkdir(parents=True, exist_ok=True)
        report = paths.reports / f"{lane}.jsonl"
        if not report.exists() and not report.is_symlink():
            report.touch(exist_ok=False)
    if make_snapshots:
        inspected = inspect_workspace_stats(paths.source)
        snapshot(paths.source, paths.planning, inspected.total_bytes)
        snapshot(paths.source, paths.private / "lane-2", inspected.total_bytes)
        snapshot(paths.source, paths.private / "lane-3", inspected.total_bytes)


def validate_runtime_layout(paths: RunPaths) -> None:
    """Reject deleted, linked, or replaced runtime control paths before use."""
    directories = [
        paths.root,
        paths.shared,
        paths.private,
        paths.reports,
        paths.artifacts,
        paths.checkpoints,
        paths.private / "lane-2",
        paths.private / "lane-3",
        *(paths.artifact_root(lane) for lane in LANES),
    ]
    for path in directories:
        try:
            info = path.lstat()
        except OSError as why:
            raise RuntimeError(f"runtime directory is missing: {path}") from why
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"runtime directory was replaced or linked: {path}")
    files = [
        *(paths.reports / f"{lane}.jsonl" for lane in LANES),
        paths.manifest,
        paths.state_mirror,
        paths.workspace_map,
        paths.leaderboard,
        paths.root / "objective.md",
    ]
    for path in files:
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"runtime control file was replaced or linked: {path}")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_artifact(root: Path, raw: str) -> tuple[Path, str]:
    """Resolve one declared file while rejecting escapes and every symlink hop."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("deliverable artifact root was replaced or linked")
    resolved_root = root.resolve(strict=True)
    relative = Path(raw)
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except OSError as why:
        raise ValueError(f"deliverable artifact is missing: {raw}") from why
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"deliverable artifact escapes its lane root: {raw}")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"deliverable artifact must be a regular file: {raw}")
    if not path.is_file():
        raise ValueError(f"deliverable artifact must be a regular file: {raw}")
    return path, resolved.relative_to(resolved_root).as_posix()


def validate_deliverable(
    root: Path,
    deliverable: Deliverable,
) -> list[dict[str, object]]:
    """Resolve and hash every explicitly published regular artifact."""
    artifacts: list[dict[str, object]] = []
    seen: set[str] = set()
    for declared in deliverable.artifacts:
        path, canonical = _resolve_artifact(root, declared.path)
        if canonical in seen:
            raise ValueError(f"deliverable repeats artifact: {canonical}")
        seen.add(canonical)
        size = path.stat().st_size
        if size > ARTIFACT_FILE_LIMIT:
            raise ValueError(
                f"deliverable artifact exceeds {ARTIFACT_FILE_LIMIT} bytes: {canonical}"
            )
        artifacts.append(
            {
                "path": canonical,
                "description": declared.description,
                "size": size,
                "sha256": _hash_file(path),
            }
        )
    return artifacts


def artifacts_still_match(root: Path, artifacts: list[dict[str, object]]) -> bool:
    """Reject an accepted handoff when an explicit file changed after publication."""
    for artifact in artifacts:
        raw = artifact.get("path")
        digest = artifact.get("sha256")
        if not isinstance(raw, str) or not isinstance(digest, str):
            return False
        try:
            path, _ = _resolve_artifact(root, raw)
        except (OSError, ValueError):
            return False
        if artifact.get("size") != path.stat().st_size or _hash_file(path) != digest:
            return False
    return True
