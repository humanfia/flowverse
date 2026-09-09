"""Recursively plan, prove, compare, review, and catalogue Lean theorems."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Any, NamedTuple

from hmz.flows import Agent, Moment, configures, flow, load
from pydantic import BaseModel, Field, field_validator, model_validator

from _recursive_lean.runtime import Runtime

MIN_RECURSIVE_NODES = 3


class Agents(NamedTuple):
    """Two independent Codex roles; the worker writes and the reviewer only judges."""

    worker: Annotated[Agent, Moment.PERMISSION_REQUEST]
    reviewer: Agent


class Config(BaseModel):
    """Bounds, output locations, and the repository's comparator contract."""

    model_config = {"extra": "forbid", "frozen": True}

    max_depth: int = Field(
        default=2,
        ge=0,
        le=6,
        description="deepest recursive subproblem level; the root is level zero",
    )
    max_children: int = Field(
        default=4,
        ge=2,
        le=12,
        description="most direct subproblems one theorem may activate",
    )
    max_parallel_children: int = Field(
        default=24,
        ge=1,
        le=200,
        description=(
            "global worker-pool size for every dependency-ready node in the DAG"
        ),
    )
    max_nodes: int = Field(
        default=24,
        ge=1,
        le=200,
        description="hard bound on all theorem and subproblem nodes in one run",
    )
    node_attempts: int = Field(
        default=2,
        ge=1,
        le=8,
        description=(
            "legacy compatibility setting; after one scaffold exists, correctness "
            "feedback continuously iterates the NL proof"
        ),
    )
    plan_attempts: int = Field(
        default=1,
        ge=1,
        le=1,
        description="exactly one immutable scaffold-plan generation per node",
    )
    natural_proof_attempts: int = Field(
        default=3,
        ge=1,
        le=8,
        description=(
            "natural-language proof/review revisions per batch; all batches continue "
            "from the latest rejected draft until review passes"
        ),
    )
    decomposition_attempts: int = Field(
        default=2,
        ge=1,
        le=5,
        description="attempts to obtain a valid acyclic subproblem decomposition",
    )
    rlcr_rounds: int = Field(
        default=20,
        ge=1,
        le=200,
        description="maximum official humanize1:rlcr rounds per Lean node",
    )
    plan_turn_timeout: float = Field(
        default=3600,
        ge=0,
        description="seconds for one humanize1 planning turn; zero disables it",
    )
    plan_total_timeout: float = Field(
        default=14400,
        ge=0,
        description="seconds for one complete planning phase; zero disables it",
    )
    comparator_timeout: float = Field(
        default=21600,
        ge=1,
        description="seconds allowed for each independent comparator run",
    )
    problem_id: str = Field(
        default="",
        description=(
            "one Lean-Eval problem id; blank derives it from the task URL, workspace "
            "README, or workspace directory"
        ),
    )
    problem_fetch_attempts: int = Field(
        default=3,
        ge=1,
        le=8,
        description="attempts in one dedicated session to fetch one valid problem page",
    )
    artifact_dir: str = Field(
        default=".humanize/recursive-lean-prover",
        description="untracked directory for plans, proofs, DAGs, logs, and run state",
    )
    wiki_dir: str = Field(
        default=".humanize/math-wiki",
        description="Markdown wiki receiving every comparator-approved theorem",
    )
    reference_dir: str = Field(
        default=".humanize/math-reference-library",
        description="untracked cache for the three mandatory reference repositories",
    )
    huggingface_token_env: str = Field(
        default="HF_TOKEN",
        description=(
            "environment variable holding the Hugging Face read token; the value is "
            "never written to config, prompts, manifests, or subprocess arguments"
        ),
    )
    lean_target: str = Field(
        default="",
        description="Lean file the worker must edit; blank lets it infer the project target",
    )
    agent_hidden_files: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "tracked comparator-only files removed from the main checkout and every "
            "agent worktree before any agent session starts"
        ),
    )
    comparator_command: str = Field(
        default="bash tools/check-with-comparator.sh",
        min_length=1,
        description="argv-style comparator command; node placeholders are supported",
    )
    comparator_success: str = Field(
        default="Your solution is okay!",
        min_length=1,
        description="text that must occur in successful comparator output",
    )
    stop_on_child_failure: bool = Field(
        default=True,
        description="block a parent when any required subproblem exhausts its attempts",
    )

    @field_validator("artifact_dir", "wiki_dir", "reference_dir")
    @classmethod
    def _local_state(cls, value: str) -> str:
        """Keep orchestration output out of RLCR's git-clean gate."""
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(".humanize/"):
            raise ValueError("must be a relative path below .humanize/")
        if ".." in normalized.split("/"):
            raise ValueError("must not contain '..'")
        return normalized

    @field_validator("problem_id")
    @classmethod
    def _problem_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", normalized
        ):
            raise ValueError("must be blank or one Lean-Eval problem id")
        return normalized

    @field_validator("huggingface_token_env")
    @classmethod
    def _token_environment_name(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
            raise ValueError("must be an environment-variable name")
        if normalized.casefold() in {
            "codex_home",
            "home",
            "path",
            "pwd",
            "shell",
            "user",
        }:
            raise ValueError("must not repurpose a common system environment variable")
        return normalized

    @field_validator("lean_target")
    @classmethod
    def _relative_lean_target(cls, value: str) -> str:
        """A target belongs to the repository in which the flow runs."""
        normalized = value.strip()
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("must be blank or a relative path inside the repository")
        if normalized and not normalized.endswith(".lean"):
            raise ValueError("must name a .lean file")
        return normalized

    @field_validator("agent_hidden_files")
    @classmethod
    def _relative_hidden_files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Accept only unique repository-relative file paths outside Git metadata."""
        normalized: list[str] = []
        for value in values:
            one = value.strip().rstrip("/")
            parts = Path(one).parts
            if (
                not one
                or not parts
                or Path(one).is_absolute()
                or ".." in parts
                or parts[0] == ".git"
            ):
                raise ValueError(
                    "entries must be relative file paths inside the repository and "
                    "outside .git"
                )
            if one in normalized:
                raise ValueError("entries must be unique")
            normalized.append(one)
        return tuple(normalized)

    @model_validator(mode="after")
    def _tree_fits(self) -> Config:
        """Reject a bound that cannot even hold a root and one complete fan-out."""
        if self.max_depth and self.max_nodes < MIN_RECURSIVE_NODES:
            raise ValueError("recursive runs need max_nodes >= 3")
        return self


class WorktreeRlcrConfig(BaseModel):
    """The official RLCR settings forwarded by an isolated node process."""

    model_config = {"extra": "forbid", "frozen": True}

    plan_file: str = Field(description="absolute immutable implementation plan path")
    max: int = Field(
        default=20,
        ge=1,
        le=200,
        description="maximum official RLCR implementation/review rounds",
    )
    base_branch: str = Field(
        default="",
        description=(
            "exact post-overlay commit retained in the node audit configuration"
        ),
    )
    track_plan_file: bool = False
    push_every_round: bool = False
    skip_impl: bool = False
    skip_quiz: bool = True
    privacy: bool = True
    agent_teams: bool = False
    claude_answer_codex: bool = True


def _nested_rlcr_config(config: WorktreeRlcrConfig) -> dict[str, Any]:
    """Forward implementation settings without enabling RLCR's generic code review.

    The recursive controller owns the Lean acceptance review: after its machine
    comparator succeeds, a fresh role-distinct Codex reviewer reruns that exact
    comparator.  Giving official RLCR a base branch starts an additional generic
    repository-wide code review that does not know the selected DAG-node boundary
    and can reopen already accepted ancestor work.  Keep the frozen base in the
    durable node-side config, but leave the nested loop's review base blank so it
    returns immediately after its implementation reviewer accepts the candidate.
    """
    forwarded = config.model_dump()
    # An empty base_branch is not itself a no-review setting: official Humanize
    # resolves it to origin/HEAD, main, or master.  Use the explicit setup-only
    # switch so the implementation loop finalizes as soon as its ordinary RLCR
    # rounds accept the work.  The recursive controller then owns both exact
    # Lean comparator gates.
    forwarded["base_branch"] = ""
    forwarded["skip_code_review"] = True
    return forwarded


def _require_explicit_rlcr_review_skip() -> None:
    """Fail closed when the installed official RLCR cannot honor node isolation."""
    model = configures("official/humanize1:rlcr")
    if model is None or "skip_code_review" not in model.model_fields:
        raise RuntimeError(
            "official/humanize1:rlcr is too old: update the Humanize 2 official "
            "flowverse to a version exposing skip_code_review"
        )


@flow(
    resumable=True,
    about=(
        "Fetch one Lean-Eval problem, then recursively prove it with three reference "
        "corpora, RLCR, comparator gates, a live DAG, and a wiki"
    ),
)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Prove one mathematical problem and recursively prove its named subproblems."""
    Runtime(agents, task, config or Config(), state).execute()


@flow(
    name="worktree-rlcr",
    resumable=True,
    selectable=False,
    about="Run official RLCR in one node worktree while inheriting recursive Lean rules",
)
def worktree_rlcr(
    agents: Agents,
    task: str,
    config: WorktreeRlcrConfig,
    state: dict[str, Any] | None = None,
) -> None:
    """Process-isolated bridge whose actual cwd is the formalizing node worktree."""
    # Long-lived recursive supervisors may have been imported before automatic
    # Lake-input provisioning was added.  This newly spawned bridge still runs in
    # the node worktree before the builder starts, so repair a missing ignored
    # manifest from the repository's primary worktree without restarting anything.
    worktree = Path.cwd()
    manifest = worktree / "lake-manifest.json"
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "lake-manifest.json"],
        capture_output=True,
        text=True,
        check=False,
    )
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        not manifest.exists()
        and ignored.returncode == 0
        and common.returncode == 0
        and common.stdout.strip()
    ):
        source = Path(common.stdout.strip()).parent / "lake-manifest.json"
        if source.is_file() and source.resolve() != manifest.resolve():
            shutil.copy2(source, manifest)
    _require_explicit_rlcr_review_skip()
    forwarded = _nested_rlcr_config(config)
    load("official/humanize1:rlcr", inherit_skills=True)(
        agents,
        task,
        forwarded,
    )
    if state is not None:
        state.clear()


__all__ = ["Agents", "Config", "WorktreeRlcrConfig", "run", "worktree_rlcr"]
