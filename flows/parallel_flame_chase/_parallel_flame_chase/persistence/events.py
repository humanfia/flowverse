from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, cast

from ..core.models import LANES, LaneName
from ..core.utils import JSONL_LINE_LIMIT
from .probe import RunPaths
from .workspace import append_record

REPORT_LINE_LIMIT = JSONL_LINE_LIMIT
DELIVERY_EVENTS_PER_SOURCE = 12
DELIVERY_BYTES_PER_SOURCE = 128 * 1024


class ReportBus:
    """Every lane's append-only JSONL report log, held here and appended to in the run.

    The runtime is the single writer: it reads the logs when a run opens, and from then on
    delivers from what it appended.
    """

    def __init__(self, store: Any, lanes: tuple[LaneName, ...] = LANES) -> None:
        self.store = store
        self.paths = RunPaths(Path(str(store.workdir)))
        self.lanes = lanes
        self.logs: dict[str, bytes] = {}

    async def open(self) -> None:
        for lane in self.lanes:
            path = str(self.paths.report_log(lane))
            try:
                self.logs[lane] = await self.store.read(path)
            except FileNotFoundError:
                self.logs[lane] = b""

    async def publish(self, lane: LaneName, report: dict[str, object]) -> None:
        encoded = (json.dumps(report, ensure_ascii=False, default=str) + "\n").encode()
        if len(encoded) > REPORT_LINE_LIMIT:
            raise ValueError(f"JSONL record exceeds {REPORT_LINE_LIMIT} bytes")
        await append_record(
            self.store, self.paths, self.lanes, self.paths.report_log(lane), encoded
        )
        self.logs[lane] = self.logs.get(lane, b"") + encoded

    def unread(
        self,
        consumer: LaneName,
        cursors: dict[str, Any],
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        deliveries: list[dict[str, object]] = []
        acknowledgements: dict[str, int] = {}
        consumer_cursors = cast("dict[str, Any]", cursors.setdefault(consumer, {}))
        for source in self.lanes:
            if source == consumer:
                continue
            log = self.logs.get(source, b"")
            offset = consumer_cursors.get(source, 0)
            if not isinstance(offset, int) or offset < 0:
                offset = 0
            if len(log) < offset:
                offset = 0
            end = offset
            count = used = 0
            handle = io.BytesIO(log)
            handle.seek(offset)
            while (
                count < DELIVERY_EVENTS_PER_SOURCE and used < DELIVERY_BYTES_PER_SOURCE
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
                        {**envelope, "health": "oversized_report", "bytes": len(line)}
                    )
                    continue
                try:
                    loaded: object = json.loads(line)
                except json.JSONDecodeError as why:
                    deliveries.append(
                        {**envelope, "health": "invalid_report_json", "error": why.msg}
                    )
                    continue
                if not isinstance(loaded, dict):
                    deliveries.append({**envelope, "health": "invalid_report_shape"})
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
