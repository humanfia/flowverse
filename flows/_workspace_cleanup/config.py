"""What both agent-cleanup flows are set up with."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


def validate_work_paths(value: tuple[str, ...]) -> tuple[str, ...]:
    """Refuse work paths that leave the repository, touch .git, repeat, or overlap."""
    paths = tuple(Path(raw) for raw in value)
    for path in paths:
        if (
            not path.parts
            or path.is_absolute()
            or path == Path(".")
            or ".." in path.parts
            or ".git" in path.parts
        ):
            raise ValueError("work_paths must be relative paths below the repository")
    if len(set(paths)) != len(paths):
        raise ValueError("work_paths must not contain duplicates")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError("work_paths must not overlap")
    return tuple(path.as_posix() for path in paths)


class Config(BaseModel):
    """The cleanup cadence, the cleaner's limits, and the session guards.

    What a run may spend is not here: `budget:` is the run's allowance, which humanize
    holds every session to.
    """

    model_config = {"extra": "forbid"}

    work_paths: tuple[str, ...] = Field(
        min_length=1,
        description="required relative, non-overlapping files or directories where "
        "agents may create or revise task work",
    )
    cleanup_turns: int = Field(
        default=3,
        ge=0,
        description="completed coding turns between cleaning epochs; 0 never cleans",
    )
    next_lines: int = Field(
        default=10,
        ge=1,
        description="the most lines NEXT.md may hold",
    )
    comment_lines: int = Field(
        default=30,
        ge=0,
        description=(
            "cap on total comment lines across supported sources under work_paths;"
            " an overage is printed, never mechanically stripped"
        ),
    )
    repairs: int = Field(
        default=2,
        ge=0,
        description=(
            "times an over-measure is handed back to the same cleaner session before"
            " the flow cuts mechanically"
        ),
    )
    check_command: str = Field(
        default="",
        description=(
            "correctness check run in the working directory after a cleaning, held to"
            " an hour; empty skips the check and the revert"
        ),
    )
    session_timeout_minutes: float = Field(
        default=240.0,
        ge=0,
        description="minutes per turn before a wrap-up request; 0 disables it",
    )
    idle_timeout_minutes: float = Field(
        default=20.0,
        ge=0,
        description="minutes without token usage increasing before a reminder; 0 disables it",
    )
    stop_grace_minutes: float = Field(
        default=10.0,
        ge=0,
        description="minutes after the wrap-up request before the turn is cut off",
    )
    max_tracked_file_mb: float = Field(
        default=10.0,
        gt=0,
        description=(
            "files over this many MB are never committed: the flow leaves them out and"
            " the repository's pre-commit hook refuses them"
        ),
    )
    confirm_large_workspace_copies: bool = Field(
        default=True,
        description=(
            "ask before cleaning a workspace too large to copy aside every epoch;"
            " off only warns"
        ),
    )

    @field_validator("work_paths")
    @classmethod
    def _validate_work_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_work_paths(value)


def required(config: Config | None, flow: str) -> Config:
    """The config a run was set up with; there is no default for work_paths."""
    if config is None:
        raise ValueError(
            f"{flow} needs work_paths: pass -c with a file saying e.g."
            " `work_paths: [src]`, or set it in /flow"
        )
    return config
