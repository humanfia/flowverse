"""Time and token-progress guards for the workspace-cleanup flows."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextvars import copy_context
from typing import Any

TIMEOUT_PROMPT = (
    "You have been working for {elapsed}. Stop immediately; do not continue any "
    "experiments. Wrap up your current work and terminate this session within "
    "{grace:g} minutes."
)
IDLE_PROMPT = (
    "No action has been taken for {minutes} {unit}. Please confirm your work status "
    "and continue the task."
)
_STARTED = "_workspace_cleanup_watchdog_started"
_FORCED = "_workspace_cleanup_watchdog_forced"
_TIMEOUT_AT = "_workspace_cleanup_watchdog_timeout_at"


def _tokens(session: Any) -> float | None:
    try:
        spent = session.spent()
        value = getattr(spent, "total", getattr(spent, "output", None))
        return None if value is None else float(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _say(session: Any, prompt: str) -> bool:
    try:
        session.interject(prompt)
    except Exception as error:  # noqa: BLE001 - unsupported backends are best-effort
        print(f"session watchdog could not inject prompt: {error}")
        return False
    return True


def _close(session: Any) -> None:
    try:
        session.close()
    except Exception as error:  # noqa: BLE001 - report and keep the flow alive
        print(f"session watchdog could not close session: {error}")


def _mark_forced(session: Any) -> None:
    try:
        setattr(session, _FORCED, True)
    except (AttributeError, TypeError):
        pass


def was_forced(session: Any) -> bool:
    """Return whether this session was closed by the watchdog."""
    return bool(getattr(session, _FORCED, False))


def _session_started(session: Any, now: float) -> float:
    try:
        started = getattr(session, _STARTED)
    except AttributeError:
        started = now
        try:
            setattr(session, _STARTED, started)
        except (AttributeError, TypeError):
            pass
    return float(started)


def run_guarded(
    session: Any,
    call: Callable[[], Any],
    *,
    session_timeout_minutes: float,
    idle_timeout_minutes: float,
    stop_grace_minutes: float,
    label: str = "agent",
) -> Any:
    """Guard a call; the wall-clock deadline spans every call in the session.

    Idle reminders reset on token progress. A forced close must stop the worker
    before the flow can retry or clean the repository.
    """
    if was_forced(session):
        return None
    if not session_timeout_minutes and not idle_timeout_minutes:
        return call()

    result: list[Any] = []
    failure: list[BaseException] = []
    done = threading.Event()

    def invoke() -> None:
        try:
            result.append(call())
        except BaseException as error:  # noqa: BLE001 - carried back to the flow thread
            failure.append(error)
        finally:
            done.set()

    now = time.monotonic()
    began = _session_started(session, now)
    last_tokens = _tokens(session)
    last_progress = now
    idle_warned = False
    timeout_at = getattr(session, _TIMEOUT_AT, None)

    wall_limit = max(session_timeout_minutes, 0.0) * 60.0
    idle_limit = max(idle_timeout_minutes, 0.0) * 60.0
    grace = max(stop_grace_minutes, 0.0) * 60.0
    interval = 0.1
    forced = False

    # A cleaner can return during its grace period and be called again for a
    # repair. That call must not grant it a new deadline or reopen a closed session.
    if timeout_at is not None and now - timeout_at >= grace:
        _close(session)
        _mark_forced(session)
        return None

    context = copy_context()
    worker = threading.Thread(
        target=context.run, args=(invoke,), name=f"{label}-turn", daemon=True
    )
    worker.start()
    try:
        while not done.wait(interval):
            now = time.monotonic()
            tokens = _tokens(session)
            if tokens is not None and (last_tokens is None or tokens > last_tokens):
                last_tokens = tokens
                last_progress = now
                idle_warned = False

            if wall_limit and timeout_at is None and now - began >= wall_limit:
                elapsed = _format_elapsed(now - began)
                print(
                    f"{label}: session time limit reached after {elapsed}; requesting wrap-up"
                )
                timeout_at = now
                setattr(session, _TIMEOUT_AT, timeout_at)
                _say(
                    session,
                    TIMEOUT_PROMPT.format(elapsed=elapsed, grace=stop_grace_minutes),
                )

            if (
                idle_limit
                and not idle_warned
                and timeout_at is None
                and now - last_progress >= idle_limit
            ):
                _say(
                    session,
                    IDLE_PROMPT.format(
                        minutes=f"{idle_timeout_minutes:g}",
                        unit="minute" if idle_timeout_minutes == 1 else "minutes",
                    ),
                )
                idle_warned = True

            if timeout_at is not None and now - timeout_at >= grace:
                print(f"{label}: wrap-up grace expired; closing the session")
                _mark_forced(session)
                forced = True
                break
    finally:
        if forced or not done.is_set():
            _close(session)
        worker.join(timeout=30.0)
        if worker.is_alive():
            raise RuntimeError(f"{label}: session did not terminate after close")

    if failure and not forced:
        raise failure[0]
    if forced:
        return None
    return result[0] if result else ""


def _format_elapsed(seconds: float) -> str:
    minutes = int(seconds // 60)
    remainder = int(seconds % 60)
    if minutes:
        return f"{minutes} minutes"
    return f"{remainder} seconds"
