"""Parallel Flame Chase -- durable report-driven work across three isolated lanes.

    hmz exec -f ./flows/parallel_flame_chase \
      -a coordinator -a lane1-a -a lane1-b -a lane2-a -a lane2-b \
      -a lane3-a -a lane3-b "$(cat TASK.md)"

The coordinator plans once and does not return. Lane 1 alone owns the original source while
Lanes 2 and 3 work in private snapshots; the six lane actors alternate in fresh sessions and
coordinate through durable reports and reconstructable artifacts. Before a run that needs
snapshots, an oversized source workspace is shown to the person at the prompt for confirmation.

This ordinary flow has no mission audits or coordinator-driven interruptions. Use the separate
``parallel_flame_chase_mission`` flow when scoped review and redirection are required.

Runs are resumable. Repeating the same task or entering ``continue`` resumes compatible state.
For ``continue``, a changed TASK.md produces a new plan over a fresh source snapshot; a different
substantive task starts a fresh run. Set ``resume_mode: fresh`` to force a new run.
"""

from __future__ import annotations

from typing import Any

from _parallel_flame_chase.core.api import Agents, BaseConfig
from _parallel_flame_chase.runtime import execute
from hmz.flows import flow


class Config(BaseConfig):
    """Isolation, pacing, and resume policy for the report-driven base flow."""


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Run the report-driven Parallel Flame Chase without mission audits."""
    execute(agents, task, config or Config(), state)


__all__ = ["Agents", "Config", "run"]
