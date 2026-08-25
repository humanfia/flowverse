"""Small, dependency-light helpers shared by the parallel runtime."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

JSONL_LINE_LIMIT = 128 * 1024


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


def json_copy(value: Any) -> Any:
    """Make a JSON-safe detached copy of durable state."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def atomic_json(path: Path, value: object) -> None:
    """Atomically replace one formatted JSON document."""
    _atomic_write(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
    )


def atomic_text(path: Path, text: str) -> None:
    """Atomically replace one UTF-8 text file."""
    _atomic_write(path, text)


def append_jsonl(
    path: Path,
    value: object,
    *,
    line_limit: int = JSONL_LINE_LIMIT,
) -> None:
    """Append one bounded, flushed JSON line without following symlinks."""
    encoded = (json.dumps(value, ensure_ascii=False, default=str) + "\n").encode()
    if len(encoded) > line_limit:
        raise ValueError(f"JSONL record exceeds {line_limit} bytes")
    if path.is_symlink():
        raise RuntimeError(f"refusing to append through a linked JSONL file: {path}")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def file_identity(path: Path) -> str:
    """Identify one file incarnation so a rotated stream restarts cleanly."""
    info = path.stat()
    return f"{info.st_dev}:{info.st_ino}"


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


def close_safely(resource: Any | None) -> None:
    """Best-effort close for an ephemeral session or similar handle."""
    if resource is not None:
        with contextlib.suppress(BaseException):
            resource.close()
