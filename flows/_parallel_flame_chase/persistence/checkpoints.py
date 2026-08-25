"""Bounded, exact-identity lane checkpoint recovery."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from ..core.models import LaneCheckpoint, LaneReport

CHECKPOINT_FILE_LIMIT = 1024 * 1024
IDENTITY_FIELDS = ("version", "run_id", "lane", "mission_id", "generation")


def checkpoint_fingerprint(path: Path) -> tuple[int, int, str] | None:
    """Identify one checkpoint incarnation without parsing actor-authored data."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        info = path.stat()
        digest = (
            "oversized"
            if info.st_size > CHECKPOINT_FILE_LIMIT
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns, digest


def read_checkpoint(
    path: Path,
    expected: Mapping[str, object],
) -> LaneCheckpoint | None:
    """Read a bounded checkpoint only when its complete identity is current."""
    try:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > CHECKPOINT_FILE_LIMIT
        ):
            return None
        checkpoint = LaneCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    identity = checkpoint.identity.model_dump(mode="json")
    if any(identity.get(key) != expected.get(key) for key in IDENTITY_FIELDS):
        return None
    return checkpoint


def checkpoint_report(
    path: Path,
    before: tuple[int, int, str] | None,
    expected: Mapping[str, object],
) -> LaneReport | None:
    """Recover a changed, exact-identity checkpoint after an interrupted turn."""
    if checkpoint_fingerprint(path) == before:
        return None
    checkpoint = read_checkpoint(path, expected)
    return checkpoint.report if checkpoint is not None else None
