"""Parallel Flame Chase Report Share, an additive same-lane handoff experiment."""

from __future__ import annotations

from typing import Any

from _parallel_flame_chase.core.api import BaseConfig, OrchestrateorAgents
from hmz.flows import flow

from parallel_flame_chase_report_share.runtime import execute

Agents = OrchestrateorAgents


class Config(BaseConfig):
    """Isolation, pacing, and resume policy for Report Share."""


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Run report-driven lanes with an explicit same-lane report handoff."""
    execute(agents, task, config or Config(), state)


__all__ = ["Agents", "Config", "run"]
