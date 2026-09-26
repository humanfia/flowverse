"""Work on the run's files where they are: run in the workspace environment as a script.

`python probe.py <mode> ...` prints JSON, or a refusal on stderr with status 3:

- `stats <dir>`: the regular files and apparent bytes of a workspace;
- `init <root> <source> fresh|resume <bytes|-> <lane>...`: lay out a run directory,
  snapshotting the source for a fresh run, or check that a resumed one is whole;
- `snapshot <source> <dest>`: copy the source to `dest`, unless a copy is there already;
- `layout <root> <lanes>`: refuse a runtime directory or file that was replaced or linked;
- `commit <root> <lanes> <staged> <target>...`: check the layout, then rename each staged
  file over its target, replacing a link rather than writing through it;
- `append <root> <lanes> <staged> <log>`: check the layout, then append a staged record to a
  report log that is not a link;
- `artifacts <root> <path>...`: check and hash the regular files of an artifact package;
- `checkpoint <path> [text]`: a checkpoint's fingerprint, and its text if asked for.

`<lanes>` is the lanes, comma-separated.

It uses the standard library alone, and everything a flow imports from it is pure.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

ARTIFACT_FILE_LIMIT = 64 * 1024 * 1024
CHECKPOINT_FILE_LIMIT = 1024 * 1024
REFUSED = 3


@dataclass(frozen=True, slots=True)
class WorkspaceStats:
    regular_files: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path

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
    def planning_revisions(self) -> Path:
        return self.shared / "planning-revisions"

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

    @property
    def staging(self) -> Path:
        return self.shared / "staging"

    @property
    def objective(self) -> Path:
        return self.root / "objective.md"

    def workspace(self, lane: str) -> Path:
        return self.private / lane

    def report_log(self, lane: str) -> Path:
        return self.reports / f"{lane}.jsonl"

    def artifact_root(self, lane: str) -> Path:
        return self.artifacts / lane

    def checkpoint(self, lane: str) -> Path:
        return self.checkpoints / f"{lane}.json"


def _private(lanes: list[str]) -> list[str]:
    return [lane for lane in lanes if lane != "lane-1"]


def inspect_workspace_stats(source: Path) -> WorkspaceStats:
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


def snapshot(source: Path, destination: Path, size: int | None = None) -> bool:
    """Copies `source` to `destination` whole, or leaves a copy already there.

    Returns:
      Whether it copied.
    """
    if destination.exists():
        return False
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError(f"cannot snapshot {source} into itself at {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    try:
        copied = False
        if (copy := shutil.which("cp")) is not None:
            partial.mkdir()
            try:
                subprocess.run(
                    [
                        copy,
                        "--archive",
                        "--reflink=auto",
                        f"{source}{os.sep}.",
                        str(partial),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.SubprocessError):
                shutil.rmtree(partial, ignore_errors=True)
            else:
                copied = True
        if not copied:
            apparent = (
                inspect_workspace_stats(source).total_bytes if size is None else size
            )
            if shutil.disk_usage(destination.parent).free < apparent:
                raise OSError(f"not enough free space to snapshot {source}")
            shutil.copytree(source, partial, symlinks=True)
        partial.rename(destination)
    finally:
        shutil.rmtree(partial, ignore_errors=True)
    return True


def initialize_paths(
    paths: RunPaths,
    source: Path,
    lanes: list[str],
    *,
    make_snapshots: bool,
    size: int | None = None,
) -> None:
    if not make_snapshots:
        required = (
            paths.root,
            paths.shared,
            paths.reports,
            *(paths.workspace(lane) for lane in _private(lanes)),
            *(paths.report_log(lane) for lane in lanes),
        )
        if not all(path.exists() for path in required):
            raise ValueError(
                "resumable run is incomplete; refusing to recreate lost state"
            )
        paths.staging.mkdir(exist_ok=True)
        validate_runtime_layout(paths, lanes)
    paths.reports.mkdir(parents=True, exist_ok=True)
    paths.checkpoints.mkdir(parents=True, exist_ok=True)
    paths.staging.mkdir(parents=True, exist_ok=True)
    paths.private.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        paths.artifact_root(lane).mkdir(parents=True, exist_ok=True)
        report = paths.report_log(lane)
        if not report.exists() and not report.is_symlink():
            report.touch(exist_ok=False)
    if make_snapshots:
        snapshot(source, paths.planning, size)
        for lane in _private(lanes):
            snapshot(source, paths.workspace(lane), size)


def validate_runtime_layout(paths: RunPaths, lanes: list[str]) -> None:
    directories = [
        paths.root,
        paths.shared,
        paths.private,
        paths.reports,
        paths.artifacts,
        paths.checkpoints,
        paths.staging,
        *(paths.workspace(lane) for lane in _private(lanes)),
        *(paths.artifact_root(lane) for lane in lanes),
    ]
    for path in directories:
        try:
            info = path.lstat()
        except OSError as why:
            raise ValueError(f"runtime directory is missing: {path}") from why
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"runtime directory was replaced or linked: {path}")
    files = [
        *(paths.report_log(lane) for lane in lanes),
        paths.manifest,
        paths.state_mirror,
        paths.workspace_map,
        paths.leaderboard,
        paths.objective,
    ]
    for path in files:
        if not path.exists() and not path.is_symlink():
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"runtime control file was replaced or linked: {path}")


def _staged(paths: RunPaths, staged: Path) -> Path:
    if staged.parent != paths.staging or staged.is_symlink() or not staged.is_file():
        raise ValueError(f"not a staged runtime file: {staged}")
    return staged


def commit(paths: RunPaths, lanes: list[str], pairs: list[tuple[Path, Path]]) -> None:
    """Renames each staged file over its target once the layout is known to be whole."""
    try:
        validate_runtime_layout(paths, lanes)
        for staged, target in pairs:
            if not target.is_relative_to(paths.root):
                raise ValueError(f"not a runtime file: {target}")
            _staged(paths, staged).replace(target)
    finally:
        for staged, _ in pairs:
            with contextlib.suppress(OSError):
                staged.unlink()


def append(paths: RunPaths, lanes: list[str], staged: Path, log: Path) -> None:
    """Appends a staged record to a report log, refusing one that became a link."""
    try:
        validate_runtime_layout(paths, lanes)
        if log not in {paths.report_log(lane) for lane in lanes}:
            raise ValueError(f"not a report log: {log}")
        record = _staged(paths, staged).read_bytes()
        flags = os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(log, flags)
        except OSError as why:
            raise ValueError(
                f"runtime control file was replaced or linked: {log}"
            ) from why
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(record)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        with contextlib.suppress(OSError):
            staged.unlink()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_artifact(root: Path, raw: str) -> tuple[Path, str]:
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


def describe_artifacts(root: Path, raws: list[str]) -> list[dict[str, object]]:
    described: list[dict[str, object]] = []
    for raw in raws:
        path, canonical = _resolve_artifact(root, raw)
        size = path.stat().st_size
        if size > ARTIFACT_FILE_LIMIT:
            raise ValueError(
                f"deliverable artifact exceeds {ARTIFACT_FILE_LIMIT} bytes: {canonical}"
            )
        described.append({"path": canonical, "size": size, "sha256": _hash_file(path)})
    return described


def checkpoint_state(path: Path, *, text: bool = True) -> dict[str, str | None]:
    """A checkpoint's fingerprint, and its text where asked and readable as UTF-8.

    Both are None unless it is a regular file; the text is None for one over the limit.
    """
    with contextlib.suppress(OSError):
        if not path.is_symlink() and path.is_file():
            info = path.stat()
            said = None
            if info.st_size > CHECKPOINT_FILE_LIMIT:
                digest = "oversized"
            else:
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                with contextlib.suppress(UnicodeDecodeError):
                    said = data.decode("utf-8") if text else None
            fingerprint = f"{info.st_size}:{info.st_mtime_ns}:{digest}"
            return {"fingerprint": fingerprint, "text": said}
    return {"fingerprint": None, "text": None}


def main(argv: list[str]) -> int:
    mode, first, *rest = argv
    try:
        said: object = None
        if mode == "stats":
            said = asdict(inspect_workspace_stats(Path(first)))
        elif mode == "init":
            source, kind, size, *lanes = rest
            initialize_paths(
                RunPaths(Path(first)),
                Path(source),
                lanes,
                make_snapshots=kind == "fresh",
                size=None if size == "-" else int(size),
            )
        elif mode == "snapshot":
            said = snapshot(Path(first), Path(rest[0]))
        elif mode == "layout":
            validate_runtime_layout(RunPaths(Path(first)), rest[0].split(","))
        elif mode == "commit":
            lanes, *files = rest
            pairs = [
                (Path(files[i]), Path(files[i + 1])) for i in range(0, len(files), 2)
            ]
            commit(RunPaths(Path(first)), lanes.split(","), pairs)
        elif mode == "append":
            lanes, staged, log = rest
            append(RunPaths(Path(first)), lanes.split(","), Path(staged), Path(log))
        elif mode == "artifacts":
            said = describe_artifacts(Path(first), rest)
        elif mode == "checkpoint":
            said = checkpoint_state(Path(first), text=rest != ["fingerprint"])
        else:
            raise ValueError(f"unknown probe {mode!r}")
    except (OSError, ValueError) as why:
        print(why, file=sys.stderr)
        return REFUSED
    print(json.dumps(said))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
