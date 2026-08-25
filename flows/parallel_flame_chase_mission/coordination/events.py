"""Bounded, versioned external evidence ingress for Mission mode."""

from __future__ import annotations

from pathlib import Path

from _parallel_flame_chase.core.utils import file_identity

from .models import ExternalEventV1

EXTERNAL_LINE_LIMIT = 64 * 1024
EXTERNAL_SKIP_LIMIT = 8 * 1024 * 1024


class ExternalEventReader:
    """Tail an adapter-owned JSONL stream without executing anything."""

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
        identity = file_identity(self.path)
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
