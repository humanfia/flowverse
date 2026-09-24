from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import copy_context
from typing import Any

from hmz.flows import Budget

from .config import Config

WRAP_UP = (
    "You have been working for {elapsed}. Stop now; do not start another experiment."
    " Wrap up your current work and end this session within {grace:g} minutes."
)
IDLE = (
    "Nothing has happened for {minutes:g} minutes. Say where your work stands and"
    " carry on with the task."
)


@dataclasses.dataclass
class Watch:
    timed_out: bool = False


def _tokens(session: Any) -> float | None:
    try:
        spent = session.spent()
        value = getattr(spent, "total", getattr(spent, "output", None))
        return None if value is None else float(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _capped(current: Budget | None, seconds: float) -> Budget:
    if current is None:
        return Budget(seconds=seconds, when="immediately", then="end")
    if current.seconds:
        seconds = min(current.seconds, seconds)
    return dataclasses.replace(current, seconds=seconds, when="immediately", then="end")


def limits(held: Config, label: str) -> dict[str, Any]:
    return {
        "session_timeout_minutes": held.session_timeout_minutes,
        "idle_timeout_minutes": held.idle_timeout_minutes,
        "stop_grace_minutes": held.stop_grace_minutes,
        "label": label,
    }


def _elapsed(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} minutes" if minutes else f"{int(seconds)} seconds"


@contextmanager
def guarded(
    session: Any,
    *,
    session_timeout_minutes: float,
    idle_timeout_minutes: float,
    stop_grace_minutes: float,
    label: str,
) -> Iterator[Watch]:
    watch = Watch()
    wall = max(session_timeout_minutes, 0.0) * 60.0
    idle = max(idle_timeout_minutes, 0.0) * 60.0
    grace = max(stop_grace_minutes, 0.0) * 60.0
    if wall:
        session.budget = _capped(session.budget, wall + grace)
    if not wall and not idle:
        yield watch
        return

    began = time.monotonic()
    stop = threading.Event()
    deaf: list[bool] = []

    def say(text: str) -> None:
        try:
            session.interject(text)
        except Exception as error:  # noqa: BLE001 - backends that cannot be told
            if not deaf:
                deaf.append(True)
                print(f"{label}: cannot talk to a running turn here: {error}")

    def keep_watch() -> None:
        last = _tokens(session)
        moved = began
        reminded = False
        interval = min(
            1.0, max(0.01, min(limit for limit in (wall, idle) if limit) / 10)
        )
        while not stop.wait(interval):
            now = time.monotonic()
            tokens = _tokens(session)
            if tokens is not None and (last is None or tokens > last):
                last, moved, reminded = tokens, now, False
            if wall and not watch.timed_out and now - began >= wall:
                watch.timed_out = True
                print(
                    f"{label}: {_elapsed(now - began)} on the clock; asking to wrap up"
                )
                say(
                    WRAP_UP.format(
                        elapsed=_elapsed(now - began), grace=stop_grace_minutes
                    )
                )
            elif idle and not reminded and not watch.timed_out and now - moved >= idle:
                reminded = True
                say(IDLE.format(minutes=idle_timeout_minutes))

    thread = threading.Thread(
        target=copy_context().run,
        args=(keep_watch,),
        name=f"{label}-watch",
        daemon=True,
    )
    thread.start()
    try:
        yield watch
    finally:
        stop.set()
        thread.join()
        if wall and time.monotonic() - began >= wall:
            watch.timed_out = True
