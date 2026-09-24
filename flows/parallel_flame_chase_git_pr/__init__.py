"""Run fixed Git PR Lite with Report Share and deterministic integration."""

from __future__ import annotations

from typing import Any, Literal

from _parallel_flame_chase_git_pr.core.api import BaseConfig, GitPRAgents
from hmz.flows import flow

from parallel_flame_chase_git_pr.runtime import execute

Agents = GitPRAgents


class Config(BaseConfig):
    git_pr_enabled: Literal[True] = True
    global_knowledge_enabled: Literal[False] = False
    experiment_memory_enabled: Literal[False] = False
    token_efficient_enabled: Literal[False] = False
    main_update_monitor_enabled: Literal[False] = False


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    execute(agents, task, config or Config(), state)


__all__ = ["Agents", "Config", "run"]
