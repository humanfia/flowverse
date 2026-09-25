from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

JSONL_LINE_LIMIT = 128 * 1024


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    ).encode()


def task_fingerprint(task: str) -> str:
    normalized = task.strip().replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode()).hexdigest()
