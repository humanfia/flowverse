from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

from hmz.flows import (
    Budget,
    HarnessError,
    HarnessNotInstalled,
    HarnessUnrecoverable,
    SessionError,
    UnsupportedOperation,
)

from .config import Config

PAUSE = 5.0
SLACK = 60.0
WRAP_UP = (
    "You have been working for {elapsed}. Stop now; do not start another experiment."
    " Wrap up your current work and end this session within {grace:g} minutes."
)
IDLE = (
    "Nothing has happened for {minutes:g} minutes. Say where your work stands and"
    " carry on with the task."
)


async def rest() -> None:
    await asyncio.sleep(PAUSE)


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


def _progress(session: Any) -> tuple[int, float]:
    usage = session.usage
    return usage.output_tokens, usage.cost


async def _watch(
    agent: Any,
    session: Any,
    *,
    began: float,
    wall: float,
    idle: float,
    grace_minutes: float,
    idle_minutes: float,
    label: str,
) -> None:
    last = _progress(session)
    moved = began
    reminded = timed_out = deaf = False
    interval = min(1.0, max(0.01, min(limit for limit in (wall, idle) if limit) / 10))

    async def say(text: str) -> None:
        nonlocal deaf
        try:
            await agent.steer(text, session=session)
        except Exception as error:  # noqa: BLE001 - a turn that cannot be told
            if not deaf:
                deaf = True
                print(f"{label}: cannot talk to a running turn here: {error}")

    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        seen = _progress(session)
        if seen != last:
            last, moved, reminded = seen, now, False
        if wall and not timed_out and now - began >= wall:
            timed_out = True
            print(f"{label}: {_elapsed(now - began)} on the clock; asking to wrap up")
            await say(
                WRAP_UP.format(elapsed=_elapsed(now - began), grace=grace_minutes)
            )
        elif idle and not reminded and not timed_out and now - moved >= idle:
            reminded = True
            await say(IDLE.format(minutes=idle_minutes))


async def guarded(
    agent: Any,
    session: Any,
    prompt: str,
    *,
    session_timeout_minutes: float,
    idle_timeout_minutes: float,
    stop_grace_minutes: float,
    label: str,
    output_schema: Any = None,
) -> tuple[Any, bool]:
    wall = max(session_timeout_minutes, 0.0) * 60.0
    idle = max(idle_timeout_minutes, 0.0) * 60.0
    grace = max(stop_grace_minutes, 0.0) * 60.0
    budget = (
        Budget(duration=dt.timedelta(seconds=wall + grace), graceful=False)
        if wall
        else None
    )
    began = time.monotonic()
    watch = (
        asyncio.create_task(
            _watch(
                agent,
                session,
                began=began,
                wall=wall,
                idle=idle,
                grace_minutes=stop_grace_minutes,
                idle_minutes=idle_timeout_minutes,
                label=label,
            )
        )
        if wall or idle
        else None
    )
    cap = wall + grace + min(SLACK, (wall + grace) / 10) if wall else None
    said = None
    try:
        async with asyncio.timeout(cap):
            said = await agent.run(
                prompt, session=session, output_schema=output_schema, budget=budget
            )
    except TimeoutError:
        if not wall or time.monotonic() - began < wall:
            raise
        print(f"{label}: cut off after {_elapsed(time.monotonic() - began)}")
    except (
        HarnessUnrecoverable,
        HarnessNotInstalled,
        SessionError,
        UnsupportedOperation,
    ):
        raise
    except HarnessError as error:
        print(f"{label}: the turn failed: {error}")
    finally:
        if watch is not None:
            watch.cancel()
            await asyncio.wait([watch])
            if not watch.cancelled() and watch.exception() is not None:
                print(f"{label}: the watch over this turn failed: {watch.exception()}")
    return said, bool(wall) and time.monotonic() - began >= wall
