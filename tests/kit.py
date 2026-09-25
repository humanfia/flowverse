from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hmz.runtime.flowing.engine import load_flow

FLOWS = Path(__file__).parents[1] / "flows"


def loaded(name: str) -> Any:
    """The flow in `flows/<name>`, through the engine's loader as `hmz exec -f` finds it."""
    return load_flow(str(FLOWS / name), caller_globals={})


def kept(journal: Path) -> dict[str, Any]:
    """What the flow at the top of a resumable run keeps, replayed from its journal."""
    state: dict[str, Any] = {}
    top = None
    for line in journal.read_text().splitlines():
        record = json.loads(line)
        if record["t"] == "call" and record["parent"] == 0:
            top = record["id"]
        elif record["t"] == "set" and record["id"] == top:
            state[record["key"]] = record["value"]
        elif record["t"] == "del" and record["id"] == top:
            state.pop(record["key"], None)
    return state
