"""Lifecycle entry point for the report-driven base Parallel Flame Chase."""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any

from hmz.flows import Stopped

from .core.utils import close_safely
from .lanes.scheduler import LaneScheduler
from .orchestration.state import WorkspaceStartupCancelled


class ParallelRuntime(LaneScheduler):
    """Run the single-writer control loop around isolated worker sessions."""

    def _close_sessions(self) -> None:
        for lane in self.lanes.values():
            close_safely(lane.session)

    def _control_cycle(self) -> None:
        """Run mode-specific single-writer work after collecting lane turns."""

    def _record_exit(self, status: str) -> None:
        self._close_sessions()
        self.control["status"] = status
        self._persist()

    def run(self) -> None:
        prepared = False
        try:
            lock = self.prepare()
            prepared = True
            print(
                f"parallel_flame_chase:{self._mode} · run {self.control['run_id']} · "
                f"state {self.paths.root}"
            )
            with lock:
                while True:
                    for lane in self.lanes.values():
                        self._collect_lane(lane)
                    self._control_cycle()
                    if (
                        self.max_turns is not None
                        and self.completed_turns >= self.max_turns
                    ):
                        self.control["status"] = "test-complete"
                        self._persist()
                        return
                    for lane in self.lanes.values():
                        self._schedule_lane(lane)
                    self.sleeper(float(getattr(self.config, "rest_seconds", 1.0)))
        except WorkspaceStartupCancelled:
            return
        except (Stopped, KeyboardInterrupt):
            if prepared:
                self._record_exit("stopped")
            raise
        except BaseException:
            if prepared:
                self._record_exit("failed")
            raise
        finally:
            self._close_sessions()
            self.executor.shutdown(wait=False, cancel_futures=True)


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
    """Run the shared engine; underscored controls support deterministic tests."""
    ParallelRuntime(
        agents,
        task,
        config,
        state,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["ParallelRuntime", "execute"]
