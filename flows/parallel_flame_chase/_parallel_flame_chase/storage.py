"""Filesystem isolation, durable buses, hashing, and source ownership."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Self, cast

from .models import LANES, Deliverable, ExternalEventV1, LaneName

REPORT_LINE_LIMIT = 131_072
EXTERNAL_LINE_LIMIT = 65_536
EXTERNAL_SKIP_LIMIT = 8 * 1024 * 1024
DELIVERY_EVENTS_PER_SOURCE = 12
DELIVERY_BYTES_PER_SOURCE = 131_072
ARTIFACT_FILE_LIMIT = 64 * 1024 * 1024
PROTECTED_FINGERPRINT_VERSION = 2


def now() -> str:
    """Return a stable millisecond UTC timestamp."""
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_time(value: object) -> dt.datetime | None:
    """Read one ISO timestamp as UTC, returning None for malformed input."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def atomic_json(path: Path, value: object) -> None:
    """Replace one JSON document after its complete successor is durable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def atomic_text(path: Path, text: str) -> None:
    """Atomically replace a UTF-8 text file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def append_jsonl(path: Path, value: object) -> None:
    """Append one bounded, flushed JSON line."""
    encoded = (json.dumps(value, ensure_ascii=False, default=str) + "\n").encode()
    if len(encoded) > REPORT_LINE_LIMIT:
        raise ValueError(f"JSONL record exceeds {REPORT_LINE_LIMIT} bytes")
    if path.is_symlink():
        raise RuntimeError(f"refusing to append through a linked JSONL file: {path}")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def workspace_key(source: Path) -> str:
    """Return a readable collision-resistant key for one source directory."""
    plain = "".join(
        character if character.isalnum() else "-" for character in str(source)
    )
    readable = "-".join(part for part in plain.split("-") if part)[-80:] or "root"
    digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:12]
    return f"{readable}-{digest}"


def task_fingerprint(task: str) -> str:
    """Bind resumable state to the exact normalized objective."""
    normalized = task.strip().replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode()).hexdigest()


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
    def audits(self) -> Path:
        return self.shared / "audits"

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
            if os.name == "nt":  # pragma: no cover - CI and supported agents are POSIX
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


def inspect_workspace(source: Path) -> int:
    """Validate a snapshot source and return its regular-file size."""
    total = 0
    for folder, directories, files in os.walk(source):
        for name in [*directories, *files]:
            path = Path(folder, name)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError(f"workspace contains unsupported special file: {path}")
    return total


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
    paths.audits.mkdir(parents=True, exist_ok=True)
    paths.private.mkdir(parents=True, exist_ok=True)
    for lane in cast("tuple[LaneName, ...]", LANES):
        paths.artifact_root(lane).mkdir(parents=True, exist_ok=True)
        report = paths.reports / f"{lane}.jsonl"
        if not report.exists() and not report.is_symlink():
            report.touch(exist_ok=False)
    if not make_snapshots:
        return
    size = inspect_workspace(paths.source)
    snapshot(paths.source, paths.planning, size)
    snapshot(paths.source, paths.private / "lane-2", size)
    snapshot(paths.source, paths.private / "lane-3", size)


def validate_runtime_layout(paths: RunPaths) -> None:
    """Reject deleted, linked, or replaced runtime control paths before using them."""
    directories = [
        paths.root,
        paths.shared,
        paths.private,
        paths.reports,
        paths.artifacts,
        paths.checkpoints,
        paths.audits,
        paths.private / "lane-2",
        paths.private / "lane-3",
        *(paths.artifact_root(cast("LaneName", lane)) for lane in LANES),
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


def _fingerprint_entries(entries: dict[str, object]) -> dict[str, object]:
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "entries": entries}


def _inside_generated_python_cache(protected_root: Path, candidate: Path) -> bool:
    """Ignore only real ``__pycache__`` directories below a protected root.

    An explicitly protected cache path remains protected because the cache component is then the
    root, not a descendant. A symlink or regular file named ``__pycache__`` is also retained so an
    actor cannot hide an import redirection behind the generated-cache exception.
    """
    suffix = candidate.relative_to(protected_root)
    for index, part in enumerate(suffix.parts):
        if part != "__pycache__":
            continue
        cache_root = protected_root.joinpath(*suffix.parts[: index + 1])
        try:
            return stat.S_ISDIR(cache_root.lstat().st_mode)
        except OSError:
            return False
    return False


def migrate_legacy_tree_fingerprint(
    fingerprint: object, protected: tuple[str, ...]
) -> dict[str, object]:
    """Project a version-1 fingerprint onto the generated-cache-safe policy.

    The legacy checksum is verified before projection. This lets a stopped run migrate only when
    every non-cache entry still agrees with the original baseline; callers must compare the result
    with a fresh version-2 fingerprint before accepting the migration.
    """
    if not isinstance(fingerprint, dict):
        raise TypeError("legacy protected fingerprint is not an object")
    entries = fingerprint.get("entries")
    if not isinstance(entries, dict) or not all(
        isinstance(path, str) and isinstance(value, dict)
        for path, value in entries.items()
    ):
        raise ValueError("legacy protected fingerprint entries are malformed")
    checked = _fingerprint_entries(cast("dict[str, object]", entries))
    if fingerprint.get("sha256") != checked["sha256"]:
        raise ValueError(
            "legacy protected fingerprint checksum does not match its entries"
        )

    normalized: dict[str, object] = {}
    parsed_entries = {
        PurePosixPath(path): (path, value) for path, value in entries.items()
    }
    for raw in protected:
        protected_root = PurePosixPath(Path(raw).as_posix())
        for candidate, (path, value) in parsed_entries.items():
            if candidate == protected_root:
                normalized[path] = value
                continue
            try:
                suffix = candidate.relative_to(protected_root)
            except ValueError:
                continue
            cache_root: PurePosixPath | None = None
            for index, part in enumerate(suffix.parts):
                if part == "__pycache__":
                    cache_root = protected_root.joinpath(*suffix.parts[: index + 1])
                    break
            if cache_root is not None:
                cache_entry = parsed_entries.get(cache_root)
                if (
                    cache_entry is not None
                    and cache_entry[1].get("kind") == "directory"
                ):
                    continue
            normalized[path] = value
    return _fingerprint_entries(normalized)


def tree_fingerprint(root: Path, protected: tuple[str, ...]) -> dict[str, object]:
    """Hash configured paths while excluding generated Python cache directories."""
    entries: dict[str, object] = {}
    for raw in protected:
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"protected path must be relative: {raw}")
        path = root / relative
        if not path.exists() and not path.is_symlink():
            entries[relative.as_posix()] = {"kind": "missing"}
            continue
        candidates = [path]
        if path.is_dir() and not path.is_symlink():
            candidates.extend(sorted(path.rglob("*")))
        for candidate in candidates:
            if _inside_generated_python_cache(path, candidate):
                continue
            rel = candidate.relative_to(root).as_posix()
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = {"kind": "symlink", "target": os.readlink(candidate)}
            elif stat.S_ISDIR(info.st_mode):
                entries[rel] = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
            elif stat.S_ISREG(info.st_mode):
                entries[rel] = {
                    "kind": "file",
                    "mode": stat.S_IMODE(info.st_mode),
                    "size": info.st_size,
                    "sha256": _hash_file(candidate),
                }
            else:
                entries[rel] = {"kind": "special", "mode": info.st_mode}
    return _fingerprint_entries(entries)


def validate_deliverable(
    root: Path, deliverable: Deliverable
) -> list[dict[str, object]]:
    """Resolve and hash every explicitly published regular artifact."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("deliverable artifact root was replaced or linked")
    resolved_root = root.resolve()
    artifacts: list[dict[str, object]] = []
    seen: set[str] = set()
    for declared in deliverable.artifacts:
        relative = Path(declared.path)
        path = root / relative
        try:
            resolved = path.resolve(strict=True)
        except OSError as why:
            raise ValueError(
                f"deliverable artifact is missing: {declared.path}"
            ) from why
        if not resolved.is_relative_to(resolved_root):
            raise ValueError(
                f"deliverable artifact escapes its lane root: {declared.path}"
            )
        cursor = root
        symlinked = False
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                symlinked = True
                break
        if symlinked or not path.is_file():
            raise ValueError(
                f"deliverable artifact must be a regular file: {declared.path}"
            )
        canonical = resolved.relative_to(resolved_root).as_posix()
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
    """Reject an accepted handoff if its explicit files changed after publication."""
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return False
    for artifact in artifacts:
        raw = artifact.get("path")
        digest = artifact.get("sha256")
        if not isinstance(raw, str) or not isinstance(digest, str):
            return False
        path = root / raw
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        cursor = root
        symlinked = False
        for part in Path(raw).parts:
            cursor /= part
            if cursor.is_symlink():
                symlinked = True
                break
        if (
            symlinked
            or not resolved.is_relative_to(resolved_root)
            or not path.is_file()
            or artifact.get("size") != path.stat().st_size
            or _hash_file(path) != digest
        ):
            return False
    return True


class ReportBus:
    """Runtime-owned append logs with per-consumer at-least-once cursors."""

    def __init__(self, paths: RunPaths) -> None:
        self.paths = paths
        self._lock = threading.RLock()

    def publish(self, lane: LaneName, report: dict[str, object]) -> dict[str, object]:
        with self._lock:
            append_jsonl(self.paths.reports / f"{lane}.jsonl", report)
            return self.head(lane)

    def head(self, lane: LaneName) -> dict[str, object]:
        """Return report-log identity and timestamps bound into authoritative flow state."""
        path = self.paths.reports / f"{lane}.jsonl"
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"report log was replaced or linked: {path}")
        info = path.stat()
        return {
            "identity": f"{info.st_dev}:{info.st_ino}",
            "size": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }

    @staticmethod
    def _identity(path: Path) -> str:
        info = path.stat()
        return f"{info.st_dev}:{info.st_ino}"

    def unread(
        self,
        consumer: LaneName,
        cursors: dict[str, Any],
    ) -> tuple[list[dict[str, object]], dict[str, dict[str, object]]]:
        """Read a bounded batch without advancing any cursor."""
        deliveries: list[dict[str, object]] = []
        acknowledgements: dict[str, dict[str, object]] = {}
        with self._lock:
            consumer_cursors = cast("dict[str, Any]", cursors.setdefault(consumer, {}))
            for source in cast("tuple[LaneName, ...]", LANES):
                if source == consumer:
                    continue
                path = self.paths.reports / f"{source}.jsonl"
                identity = self._identity(path)
                raw_cursor = consumer_cursors.get(source)
                cursor = (
                    cast("dict[str, object]", raw_cursor)
                    if isinstance(raw_cursor, dict)
                    else {}
                )
                offset = cursor.get("offset", 0)
                if not isinstance(offset, int) or offset < 0:
                    offset = 0
                if cursor.get("identity") != identity or path.stat().st_size < offset:
                    offset = 0
                end = offset
                count = 0
                used = 0
                with path.open("rb") as handle:
                    handle.seek(offset)
                    while (
                        count < DELIVERY_EVENTS_PER_SOURCE
                        and used < DELIVERY_BYTES_PER_SOURCE
                    ):
                        start = handle.tell()
                        line = handle.readline(REPORT_LINE_LIMIT + 1)
                        if not line:
                            break
                        if not line.endswith(b"\n"):
                            break
                        if count and used + len(line) > DELIVERY_BYTES_PER_SOURCE:
                            break
                        end = handle.tell()
                        used += len(line)
                        count += 1
                        digest = hashlib.sha256(line).hexdigest()
                        report_id = f"{source}:{start}:{digest[:16]}"
                        if len(line) > REPORT_LINE_LIMIT:
                            deliveries.append(
                                {
                                    "report_id": report_id,
                                    "source_lane": source,
                                    "health": "oversized_report",
                                    "bytes": len(line),
                                }
                            )
                            continue
                        try:
                            loaded: object = json.loads(line)
                        except json.JSONDecodeError as why:
                            deliveries.append(
                                {
                                    "report_id": report_id,
                                    "source_lane": source,
                                    "health": "invalid_report_json",
                                    "error": why.msg,
                                }
                            )
                            continue
                        if not isinstance(loaded, dict):
                            deliveries.append(
                                {
                                    "report_id": report_id,
                                    "source_lane": source,
                                    "health": "invalid_report_shape",
                                }
                            )
                            continue
                        deliveries.append(
                            {
                                "report_id": report_id,
                                "source_lane": source,
                                "report": loaded,
                            }
                        )
                acknowledgements[source] = {"offset": end, "identity": identity}
        return deliveries, acknowledgements

    @staticmethod
    def acknowledge(
        consumer: LaneName,
        cursors: dict[str, Any],
        acknowledgements: dict[str, dict[str, object]],
    ) -> None:
        consumer_cursors = cast("dict[str, Any]", cursors.setdefault(consumer, {}))
        for source, cursor in acknowledgements.items():
            consumer_cursors[source] = dict(cursor)


class ExternalEventReader:
    """Tail a versioned adapter-owned JSONL stream without executing anything."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def read(
        self,
        run_id: str,
        cursor: dict[str, object] | None,
        seen_ids: list[str],
    ) -> tuple[
        list[ExternalEventV1], list[dict[str, object]], dict[str, object], list[str]
    ]:
        if self.path is None or not self.path.is_file():
            return [], [], cursor or {"offset": 0, "identity": None}, seen_ids
        identity = ReportBus._identity(self.path)
        held = cursor or {}
        offset = held.get("offset", 0)
        if not isinstance(offset, int) or offset < 0:
            offset = 0
        if held.get("identity") != identity or self.path.stat().st_size < offset:
            offset = 0
        events: list[ExternalEventV1] = []
        errors: list[dict[str, object]] = []
        seen = list(seen_ids[-1000:])
        known = set(seen)
        end = offset
        with self.path.open("rb") as handle:
            handle.seek(offset)
            for _ in range(100):
                start = handle.tell()
                line = handle.readline(EXTERNAL_LINE_LIMIT + 1)
                if not line:
                    break
                if not line.endswith(b"\n") and len(line) <= EXTERNAL_LINE_LIMIT:
                    errors.append(
                        {"offset": start, "error": "event line is currently truncated"}
                    )
                    break
                if len(line) > EXTERNAL_LINE_LIMIT:
                    consumed = len(line)
                    while not line.endswith(b"\n") and consumed < EXTERNAL_SKIP_LIMIT:
                        line = handle.readline(EXTERNAL_LINE_LIMIT + 1)
                        if not line:
                            break
                        consumed += len(line)
                    end = handle.tell()
                    errors.append(
                        {
                            "offset": start,
                            "error": "event line is oversized",
                            "discarded_bytes": consumed,
                        }
                    )
                    if not line.endswith(b"\n"):
                        break
                    continue
                end = handle.tell()
                try:
                    event = ExternalEventV1.model_validate_json(line)
                except ValueError as why:
                    errors.append({"offset": start, "error": str(why)[:2000]})
                    continue
                if event.run_id != run_id or event.event_id in known:
                    continue
                known.add(event.event_id)
                seen.append(event.event_id)
                events.append(event)
        return events, errors, {"offset": end, "identity": identity}, seen[-1000:]
