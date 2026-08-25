"""Lifecycle entry point for Mission-governed Parallel Flame Chase runs."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any

from ..audits.scheduler import AuditScheduler


class MissionRuntime(AuditScheduler):
    """Run mission state, scoped audits, and lane work in one control loop."""


def execute(
    agents: Any,
    task: str,
    config: Any,
    state: dict[str, Any] | None,
    *,
    _clock: Callable[[], dt.datetime] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _max_turns: int | None = None,
) -> None:
    """Run Mission mode; underscored controls support deterministic tests."""
    MissionRuntime(
        agents,
        task,
        config,
        state,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["MissionRuntime", "execute"]
