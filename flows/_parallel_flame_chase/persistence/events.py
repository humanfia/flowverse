"""Durable at-least-once lane-report delivery."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, cast

from ..core.models import LANES, LaneName
from ..core.utils import JSONL_LINE_LIMIT, append_jsonl
from .workspace import RunPaths

REPORT_LINE_LIMIT = JSONL_LINE_LIMIT
DELIVERY_EVENTS_PER_SOURCE = 12
DELIVERY_BYTES_PER_SOURCE = 128 * 1024


class ReportBus:
    """Runtime-owned append logs with per-consumer at-least-once cursors."""

    def __init__(self, paths: RunPaths) -> None:
        self.paths = paths
        self._lock = threading.RLock()

    def publish(self, lane: LaneName, report: dict[str, object]) -> None:
        with self._lock:
            append_jsonl(
                self.paths.reports / f"{lane}.jsonl",
                report,
                line_limit=REPORT_LINE_LIMIT,
            )

    def unread(
        self,
        consumer: LaneName,
        cursors: dict[str, Any],
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        """Read a bounded batch without advancing any cursor."""
        deliveries: list[dict[str, object]] = []
        acknowledgements: dict[str, int] = {}
        with self._lock:
            consumer_cursors = cast("dict[str, Any]", cursors.setdefault(consumer, {}))
            for source in LANES:
                if source == consumer:
                    continue
                path = self.paths.reports / f"{source}.jsonl"
                offset = consumer_cursors.get(source, 0)
                if not isinstance(offset, int) or offset < 0:
                    offset = 0
                if path.stat().st_size < offset:
                    offset = 0
                end = offset
                count = used = 0
                with path.open("rb") as handle:
                    handle.seek(offset)
                    while (
                        count < DELIVERY_EVENTS_PER_SOURCE
                        and used < DELIVERY_BYTES_PER_SOURCE
                    ):
                        start = handle.tell()
                        line = handle.readline(REPORT_LINE_LIMIT + 1)
                        if not line or not line.endswith(b"\n"):
                            break
                        if count and used + len(line) > DELIVERY_BYTES_PER_SOURCE:
                            break
                        end = handle.tell()
                        used += len(line)
                        count += 1
                        digest = hashlib.sha256(line).hexdigest()
                        report_id = f"{source}:{start}:{digest[:16]}"
                        envelope: dict[str, object] = {
                            "report_id": report_id,
                            "source_lane": source,
                        }
                        if len(line) > REPORT_LINE_LIMIT:
                            deliveries.append(
                                {
                                    **envelope,
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
                                    **envelope,
                                    "health": "invalid_report_json",
                                    "error": why.msg,
                                }
                            )
                            continue
                        if not isinstance(loaded, dict):
                            deliveries.append(
                                {**envelope, "health": "invalid_report_shape"}
                            )
                            continue
                        deliveries.append({**envelope, "report": loaded})
                acknowledgements[source] = end
        return deliveries, acknowledgements

    @staticmethod
    def acknowledge(
        consumer: LaneName,
        cursors: dict[str, Any],
        acknowledgements: dict[str, int],
    ) -> None:
        consumer_cursors = cast("dict[str, Any]", cursors.setdefault(consumer, {}))
        consumer_cursors.update(acknowledgements)
