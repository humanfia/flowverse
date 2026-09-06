from __future__ import annotations

import importlib.util
import threading
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def watchdog():
    path = Path(__file__).parents[1] / "flows/_workspace_cleanup_watchdog.py"
    spec = importlib.util.spec_from_file_location("_test_cleanup_watchdog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Session:
    def __init__(self, *, reminders_to_finish: int = 0) -> None:
        self.prompts: list[str] = []
        self.tokens = 0
        self.closed = False
        self.released = threading.Event()
        self.finished = threading.Event()
        self.reminders_to_finish = reminders_to_finish

    def spent(self):
        return SimpleNamespace(total=self.tokens)

    def interject(self, prompt: str) -> None:
        self.prompts.append(prompt)
        if self.reminders_to_finish:
            self.tokens += 1
            if len(self.prompts) == self.reminders_to_finish:
                self.released.set()

    def close(self) -> None:
        self.closed = True
        self.released.set()

    def work(self):
        try:
            assert self.released.wait(timeout=3), "watchdog failed to unblock session"
            return "done"
        finally:
            self.finished.set()


def test_timeout_requests_configured_grace_then_waits_for_closed_session(watchdog):
    session = Session()
    result = watchdog.run_guarded(
        session,
        session.work,
        session_timeout_minutes=0.001,
        idle_timeout_minutes=0,
        stop_grace_minutes=0.002,
    )

    assert result is None
    assert session.closed and session.finished.is_set()
    assert watchdog.was_forced(session)
    assert len(session.prompts) == 1
    assert "within 0.002 minutes" in session.prompts[0]


def test_idle_reminder_recovers_tool_stall_and_rearms_after_token_progress(watchdog):
    session = Session(reminders_to_finish=2)
    result = watchdog.run_guarded(
        session,
        session.work,
        session_timeout_minutes=0,
        idle_timeout_minutes=0.001,
        stop_grace_minutes=0,
    )

    assert result == "done"
    assert not session.closed
    assert len(session.prompts) == 2
    assert all("confirm your work status" in prompt for prompt in session.prompts)


def test_one_idle_reminder_without_progress_before_wall_timeout(watchdog):
    session = Session()
    watchdog.run_guarded(
        session,
        session.work,
        session_timeout_minutes=0.008,
        idle_timeout_minutes=0.001,
        stop_grace_minutes=0,
    )

    assert len(session.prompts) == 2
    assert "confirm your work status" in session.prompts[0]
    assert "Stop immediately" in session.prompts[1]
    assert session.closed


def test_grace_deadline_survives_cleaner_repair_calls(watchdog, monkeypatch):
    session = Session(reminders_to_finish=1)
    now = 100.0
    monkeypatch.setattr(watchdog, "time", SimpleNamespace(monotonic=lambda: now))
    limits = {
        "session_timeout_minutes": 1,
        "idle_timeout_minutes": 0,
        "stop_grace_minutes": 2,
    }
    assert watchdog.run_guarded(session, lambda: "first", **limits) == "first"
    now = 161.0
    assert watchdog.run_guarded(session, session.work, **limits) == "done"
    assert len(session.prompts) == 1

    now = 282.0
    called = []
    assert watchdog.run_guarded(session, lambda: called.append(True), **limits) is None
    assert called == []
    assert session.closed
    assert watchdog.was_forced(session)
    assert len(session.prompts) == 1


def test_unsupported_interjection_still_closes_at_deadline(watchdog):
    session = Session()

    def unsupported(_prompt):
        raise NotImplementedError("no mid-turn steering")

    session.interject = unsupported
    assert (
        watchdog.run_guarded(
            session,
            session.work,
            session_timeout_minutes=0.001,
            idle_timeout_minutes=0,
            stop_grace_minutes=0,
        )
        is None
    )
    assert session.closed and session.finished.is_set()


@pytest.mark.parametrize("enabled", [False, True])
def test_result_exceptions_and_context_are_preserved(watchdog, enabled):
    session = Session()
    context = ContextVar("test_cleanup_context")
    token = context.set("run context")
    limits = {
        "session_timeout_minutes": 60 if enabled else 0,
        "idle_timeout_minutes": 0,
        "stop_grace_minutes": 0,
    }
    try:
        assert watchdog.run_guarded(session, context.get, **limits) == "run context"

        def fail():
            raise ValueError("turn failed")

        with pytest.raises(ValueError, match="turn failed"):
            watchdog.run_guarded(session, fail, **limits)
    finally:
        context.reset(token)
    assert not session.prompts
    assert not session.closed
