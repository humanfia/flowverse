from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any

from .lanes.runtime import STOPPING
from .lanes.scheduler import LaneScheduler
from .orchestration.state import WorkspaceStartupCancelled


class ParallelRuntime(LaneScheduler):
    async def _control_cycle(self) -> None:
        pass

    def _running(self) -> list[asyncio.Task[Any]]:
        return [lane.task for lane in self.lanes.values() if lane.task is not None]

    async def _collect_lanes(self) -> BaseException | None:
        """Collects every landed turn, each lane whatever became of the others.

        Returns:
          The first budget or cancellation a turn raised, or None.

        Raises:
          Exception: The first error recording a turn raised, once every lane is collected.
        """
        stopping: BaseException | None = None
        failed: Exception | None = None
        for lane in self.lanes.values():
            try:
                await self._collect_lane(lane)
            except STOPPING as why:
                stopping = stopping or why
            except Exception as why:  # noqa: BLE001
                failed = failed or why
        if failed is not None:
            raise failed
        return stopping

    async def _finish_turns(self) -> None:
        """Lets the turns under way land and records them, as a spent budget allows."""
        while running := self._running():
            await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            await self._collect_lanes()

    async def _stop_turns(self) -> None:
        tasks = self._running()
        for lane in self.lanes.values():
            lane.task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _record_exit(self, status: str) -> None:
        with contextlib.suppress(Exception):
            await self._collect_lanes()
        await self._stop_turns()
        self.control["status"] = status
        with contextlib.suppress(Exception):
            await self._persist()

    async def run(self) -> None:
        prepared = False
        try:
            await self.prepare()
            prepared = True
            print(
                f"parallel_flame_chase:{self._mode} · run {self.control['run_id']} · "
                f"state {self.paths.root}"
            )
            while True:
                stopping = await self._collect_lanes()
                if stopping is not None:
                    await self._finish_turns()
                    raise stopping
                await self._control_cycle()
                if (
                    self.max_turns is not None
                    and self.completed_turns >= self.max_turns
                ):
                    self.control["status"] = "test-complete"
                    await self._persist()
                    return
                for lane in self.lanes.values():
                    await self._schedule_lane(lane)
                await self.sleeper(self.params.rest_seconds)
        except WorkspaceStartupCancelled:
            return
        except (*STOPPING, asyncio.CancelledError, KeyboardInterrupt):
            if prepared:
                await self._record_exit("stopped")
            raise
        except BaseException:
            if prepared:
                await self._record_exit("failed")
            raise
        finally:
            await self._stop_turns()
            await self.release()


async def execute(
    agents: Any,
    envs: Any,
    task: str,
    params: Any,
    ctx: Any,
    *,
    planner: Any,
    lane_turn: Any,
    _clock: Callable[[], dt.datetime] | None = None,
    _sleep: Callable[[float], Awaitable[object]] | None = None,
    _max_turns: int | None = None,
) -> None:
    await ParallelRuntime(
        agents,
        envs,
        task,
        params,
        ctx.state,
        planner=planner,
        lane_turn=lane_turn,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["ParallelRuntime", "execute"]
