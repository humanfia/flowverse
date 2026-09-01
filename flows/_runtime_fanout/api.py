"""Public inputs of the runtime fan-out flow."""

from __future__ import annotations

from typing import NamedTuple

from hmz.flows import Agent
from pydantic import BaseModel, Field


class Agents(NamedTuple):
    """The planner, a worker template, and the final synthesizer."""

    planner: Agent
    worker: Agent
    synthesizer: Agent


class Config(BaseModel):
    """Limits for the planner-selected fan-out."""

    model_config = {"extra": "forbid"}

    max_workers: int = Field(
        default=12,
        ge=1,
        le=64,
        description="Maximum number of parallel work items",
    )


__all__ = ["Agents", "Config"]
