"""Targeted runtime-authored report delivery for PR and knowledge events."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING

from _parallel_flame_chase.core.utils import JSONL_LINE_LIMIT, append_jsonl, now

if TYPE_CHECKING:
    from pathlib import Path

SYSTEM_REPORT_BATCH = 24
SYSTEM_REPORT_BYTES = 256 * 1024


def publish_system_report(
    directory: Path,
    *,
    targets: tuple[str, ...],
    kind: str,
    summary: str,
    payload: dict[str, object],
) -> dict[str, object]:
    """Append one separately typed report to each intended lane."""
    record: dict[str, object] = {
        "version": 1,
        "report_id": uuid.uuid4().hex,
        "at": now(),
        "kind": kind,
        "summary": summary,
        "payload": payload,
        "audience": list(targets),
    }
    for target in targets:
        append_jsonl(
            directory / f"{target}.jsonl",
            record,
            line_limit=JSONL_LINE_LIMIT,
        )
    return record


def unread_system_reports(
    path: Path,
    offset: object,
) -> tuple[list[dict[str, object]], int]:
    """Read one bounded at-least-once batch without mutating its cursor."""
    start_offset = offset if isinstance(offset, int) and offset >= 0 else 0
    if path.stat().st_size < start_offset:
        start_offset = 0
    deliveries: list[dict[str, object]] = []
    end = start_offset
    count = used = 0
    with path.open("rb") as handle:
        handle.seek(start_offset)
        while count < SYSTEM_REPORT_BATCH and used < SYSTEM_REPORT_BYTES:
            line_start = handle.tell()
            line = handle.readline(JSONL_LINE_LIMIT + 1)
            if not line or not line.endswith(b"\n"):
                break
            if count and used + len(line) > SYSTEM_REPORT_BYTES:
                break
            end = handle.tell()
            count += 1
            used += len(line)
            envelope: dict[str, object] = {
                "report_id": (
                    f"system:{line_start}:{hashlib.sha256(line).hexdigest()[:16]}"
                ),
                "source_lane": "system",
            }
            if len(line) > JSONL_LINE_LIMIT:
                deliveries.append(
                    {
                        **envelope,
                        "health": "oversized_system_report",
                        "bytes": len(line),
                    }
                )
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError as why:
                deliveries.append(
                    {
                        **envelope,
                        "health": "invalid_system_report_json",
                        "error": why.msg,
                    }
                )
                continue
            if not isinstance(loaded, dict):
                deliveries.append({**envelope, "health": "invalid_system_report_shape"})
                continue
            deliveries.append({**envelope, "report": loaded})
    return deliveries, end


__all__ = ["publish_system_report", "unread_system_reports"]
