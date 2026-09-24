"""Run the report-driven Parallel Flame Chase without mission audits."""

from __future__ import annotations

from typing import Any

from _parallel_flame_chase.core.api import Agents, BaseConfig
from _parallel_flame_chase.runtime import execute
from hmz.flows import flow


class Config(BaseConfig):
    pass


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    execute(agents, task, config or Config(), state)


__all__ = ["Agents", "Config", "run"]
