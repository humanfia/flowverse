"""Factorial Report Share runtime with local Git/PR and compact knowledge."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from _parallel_flame_chase.core.models import InitialPlan, LaneName, LaneReport
from _parallel_flame_chase.core.utils import (
    atomic_json,
    atomic_text,
    close_safely,
    json_copy,
)
from _parallel_flame_chase.persistence.workspace import RunPaths, initialize_paths
from _parallel_flame_chase.report_share import ReportShareRuntime
from hmz.flows import Stopped

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Callable
    from concurrent.futures import Future

    from _parallel_flame_chase.lanes.runtime import LaneRuntime
    from hmz.flows import Session

from .models import PRReviewResult
from .prompts import (
    git_planning_prompt,
    lane_protocol,
    pr_review_prompt,
)
from .repository import (
    GitRunPaths,
    commit_parents,
    create_fast_path_merge,
    discover_allowed_paths,
    discover_evaluator_command,
    git,
    initialize_shadow_repository,
    main_sha,
    pr_trailer,
    publish_main,
    validate_changed_paths,
    validate_shadow_repository,
    write_branch_protection_context,
)
from .storage import CoordinationStore
from .system_reports import publish_system_report, unread_system_reports

FINAL_REPAIR_ATTEMPT = 2
MERGE_PARENT_COUNT = 2
GIT_SHA1_LENGTH = 40
KNOWLEDGE_DIGEST_LIMIT = 12
KNOWLEDGE_PROMPT_LIMIT = 4
CYCLES_PATTERN = re.compile(r"(?im)^\s*CYCLES\s*:\s*([0-9]+)\s*$")


@dataclass(slots=True)
class PRReviewWork:
    """Ephemeral handles for the unique active PR review."""

    future: Future[PRReviewResult | None] | None = None
    session: Session | None = None
    pr_id: str | None = None
    workspace: Path | None = None


def run_pr_review(session: Session, prompt: str) -> PRReviewResult | None:
    """Repair only result shape after the coordinator has acted."""
    current = prompt
    for attempt in range(3):
        try:
            result = session(current, suppress=False, schema=PRReviewResult)
        except Stopped:
            raise
        except ValueError as why:
            if attempt == FINAL_REPAIR_ATTEMPT:
                raise
            current = f"""Your completed PR review result failed validation: {why}

Do not repeat Git or evaluation actions. Return only a corrected PRReviewResult describing the
transition you already completed. If no transition completed, use `continue`.
"""
            continue
        if result is not None:
            return result
        current = """Do not perform more actions. Return only PRReviewResult for the review work
you just completed; use `continue` if neither merge nor explicit rejection completed."""
    return None


class GitPRRuntime(ReportShareRuntime):
    """Single-writer coordinator for the complete 2x2 experimental design."""

    mode_name = "git-pr"
    skill_name = "parallel-flame-chase-git-pr"
    orchestrator_role_name = "orchestrateor"
    executor_workers = 3
    planning_cadence = "This is the only planning turn. Later PR and knowledge transitions are deterministic."

    def __init__(
        self,
        agents: Any,
        task: str,
        config: Any,
        state: dict[str, Any] | None,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_turns: int | None = None,
    ) -> None:
        super().__init__(
            agents,
            task,
            config,
            state,
            clock=clock,
            sleeper=sleeper,
            max_turns=max_turns,
        )
        self.git_paths: GitRunPaths
        self.store: CoordinationStore
        self.pr_review = PRReviewWork()

    @property
    def git_pr_enabled(self) -> bool:
        return bool(self.control.get("git_pr_enabled", False))

    @property
    def global_knowledge_enabled(self) -> bool:
        return bool(self.control.get("global_knowledge_enabled", False))

    @property
    def experiment_memory_enabled(self) -> bool:
        return bool(self.control.get("experiment_memory_enabled", False))

    @property
    def token_efficient_enabled(self) -> bool:
        return bool(self.control.get("token_efficient_enabled", False))

    @property
    def main_update_monitor_enabled(self) -> bool:
        return bool(self.control.get("main_update_monitor_enabled", False))

    def _new_mode_control(self) -> dict[str, object]:
        return {
            "git_pr_enabled": bool(getattr(self.config, "git_pr_enabled", True)),
            "global_knowledge_enabled": bool(
                getattr(self.config, "global_knowledge_enabled", True)
            ),
            "experiment_memory_enabled": bool(
                getattr(self.config, "experiment_memory_enabled", False)
            ),
            "token_efficient_enabled": bool(
                getattr(self.config, "token_efficient_enabled", False)
            ),
            "main_update_monitor_enabled": bool(
                getattr(self.config, "main_update_monitor_enabled", False)
            ),
            "git_pr": {
                "observed_main_sha": None,
                "pending_comparison": None,
                "receipt_cursor": 0,
                "review_attempts": {},
            },
            "knowledge": {"digest_updates": 0, "last_summary": None},
        }

    def _validate_mode_control(self) -> None:
        for field in (
            "git_pr_enabled",
            "global_knowledge_enabled",
            "experiment_memory_enabled",
            "token_efficient_enabled",
            "main_update_monitor_enabled",
        ):
            if not isinstance(self.control.get(field), bool):
                raise TypeError(f"resumable {field} must be boolean")
        git_state = self.control.get("git_pr")
        knowledge_state = self.control.get("knowledge")
        if not isinstance(git_state, dict) or not isinstance(knowledge_state, dict):
            raise TypeError("resumable Git/knowledge control is malformed")
        observed = git_state.get("observed_main_sha")
        if self.git_pr_enabled and (
            not isinstance(observed, str) or len(observed) != GIT_SHA1_LENGTH
        ):
            raise ValueError("resumable Git mode requires an observed main commit")
        cursor = git_state.get("receipt_cursor")
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValueError("resumable receipt cursor must be non-negative")
        digest_updates = knowledge_state.get(
            "digest_updates", knowledge_state.get("reviews", 0)
        )
        if (
            not isinstance(digest_updates, int)
            or isinstance(digest_updates, bool)
            or digest_updates < 0
        ):
            raise ValueError(
                "resumable knowledge digest update count must be non-negative"
            )
        knowledge_state["digest_updates"] = digest_updates
        knowledge_state.pop("reviews", None)

    def _source_files(self) -> tuple[Path, Path, Path]:
        directory = Path(__file__).resolve().parent
        return (
            directory / "agent_cli.py",
            directory / "storage.py",
            directory / "pre_receive.py",
        )

    def _workspace_copy_plan(self, *, resume: bool, revised: bool) -> tuple[int, str]:
        """Account for the Git planning, lane, and integration working trees."""
        if not resume and bool(getattr(self.config, "git_pr_enabled", True)):
            copies = len(self.lane_names) + 2
            return (
                copies,
                (
                    f"{copies} Git working trees (planning, each lane, and integration) "
                    "plus Git object storage"
                ),
            )
        return super()._workspace_copy_plan(resume=resume, revised=revised)

    def _create_run(self, objective: str) -> None:
        if not bool(getattr(self.config, "git_pr_enabled", True)):
            super()._create_run(objective)
            return
        self.control = self._new_control(objective)
        self.paths = RunPaths(Path(cast("str", self.control["run_root"])), self.source)
        self.paths.root.mkdir(parents=True, exist_ok=False)
        initialize_paths(self.paths, make_snapshots=False, lanes=self.lane_names)
        self.git_paths = GitRunPaths(self.paths.root)
        cli_source, storage_source, hook_source = self._source_files()
        baseline = initialize_shadow_repository(
            self.git_paths,
            self.source,
            cli_source=cli_source,
            storage_source=storage_source,
            hook_source=hook_source,
            lanes=cast("tuple[str, ...]", self.lane_names),
        )
        self.store = CoordinationStore(self.git_paths.database, self.git_paths.events)
        allowed_paths = discover_allowed_paths(self.source, objective)
        self.store.initialize(
            run_id=cast("str", self.control["run_id"]),
            git_pr_enabled=True,
            global_knowledge_enabled=self.global_knowledge_enabled,
            experiment_memory_enabled=self.experiment_memory_enabled,
            lanes=cast("tuple[str, ...]", self.lane_names),
            allowed_paths=allowed_paths,
            trusted_evaluator_command=discover_evaluator_command(
                self.source, objective
            ),
        )
        cast("dict[str, Any]", self.control["git_pr"])["observed_main_sha"] = baseline
        write_branch_protection_context(self.git_paths, self.store)

    def _initialize_mode_paths(self) -> None:
        self.git_paths = GitRunPaths(self.paths.root)
        self.store = CoordinationStore(self.git_paths.database, self.git_paths.events)
        if not self.git_pr_enabled:
            cli_source, storage_source, _hook_source = self._source_files()
            self.git_paths.bin.mkdir(parents=True, exist_ok=True)
            if not (self.git_paths.bin / "pfc").exists():
                shutil.copy2(cli_source, self.git_paths.bin / "pfc")
            if not (self.git_paths.bin / "pfc_storage.py").exists():
                shutil.copy2(storage_source, self.git_paths.bin / "pfc_storage.py")
            (self.git_paths.bin / "pfc").chmod(0o755)
            self.git_paths.system_reports.mkdir(parents=True, exist_ok=True)
            self.git_paths.evaluation_artifacts.mkdir(parents=True, exist_ok=True)
            self.git_paths.object_store.mkdir(parents=True, exist_ok=True)
            for lane in self.lane_names:
                (self.git_paths.system_reports / f"{lane}.jsonl").touch(exist_ok=True)
        allowed_paths = (
            cast("list[str]", self.store.meta("allowed_paths"))
            if self.git_paths.database.exists()
            else discover_allowed_paths(
                self.source, cast("str", self.control["objective"])
            )
        )
        trusted_evaluator_command = (
            cast("list[str]", self.store.meta("trusted_evaluator_command"))
            if self.git_paths.database.exists()
            else discover_evaluator_command(
                self.source, cast("str", self.control["objective"])
            )
        )
        self.store.initialize(
            run_id=cast("str", self.control["run_id"]),
            git_pr_enabled=self.git_pr_enabled,
            global_knowledge_enabled=self.global_knowledge_enabled,
            experiment_memory_enabled=self.experiment_memory_enabled,
            lanes=cast("tuple[str, ...]", self.lane_names),
            allowed_paths=allowed_paths,
            trusted_evaluator_command=trusted_evaluator_command,
        )
        self._refresh_views()

    def _validate_mode_layout(self) -> None:
        self.git_paths = GitRunPaths(self.paths.root)
        files = (
            self.git_paths.database,
            self.git_paths.events,
            self.git_paths.bin / "pfc",
            self.git_paths.bin / "pfc_storage.py",
            *(
                self.git_paths.system_reports / f"{lane}.jsonl"
                for lane in self.lane_names
            ),
        )
        for path in files:
            try:
                info = path.lstat()
            except OSError as why:
                raise RuntimeError(
                    f"Git/knowledge runtime path is missing: {path}"
                ) from why
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"Git/knowledge runtime file was replaced: {path}")
        directories = (
            self.git_paths.bin,
            self.git_paths.system_reports,
            self.git_paths.evaluation_artifacts,
            self.git_paths.object_store,
        )
        for path in directories:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(
                    f"Git/knowledge runtime directory was replaced: {path}"
                )
        if self.git_pr_enabled:
            validate_shadow_repository(
                self.git_paths, cast("tuple[str, ...]", self.lane_names)
            )

    def _workspace_map(self) -> dict[str, object]:
        mapping = super()._workspace_map()
        if not self.git_pr_enabled:
            if not (self.global_knowledge_enabled or self.experiment_memory_enabled):
                return mapping
            mapping["factorial_cell"] = {
                "git_pr_enabled": False,
                "global_knowledge_enabled": self.global_knowledge_enabled,
                "experiment_memory_enabled": self.experiment_memory_enabled,
                "token_efficient_enabled": self.token_efficient_enabled,
                "main_update_monitor_enabled": self.main_update_monitor_enabled,
            }
            mapping["coordination_cli"] = str(self.git_paths.bin / "pfc")
            return mapping
        mapping["lanes"] = {
            lane: {
                "workspace": str(self.git_paths.lane(lane)),
                "ownership": "isolated-writable-clone-and-equal-pr-author",
            }
            for lane in self.lane_names
        }
        mapping["integration"] = {
            "workspace": str(self.git_paths.integration),
            "ownership": "runtime-only-receipt-fast-path",
        }
        mapping["factorial_cell"] = {
            "git_pr_enabled": True,
            "global_knowledge_enabled": self.global_knowledge_enabled,
            "experiment_memory_enabled": self.experiment_memory_enabled,
            "token_efficient_enabled": self.token_efficient_enabled,
            "main_update_monitor_enabled": self.main_update_monitor_enabled,
        }
        mapping["git_pr"] = {
            "central": str(self.git_paths.central),
            "cli": str(self.git_paths.bin / "pfc"),
            "allowed_paths": self._allowed_paths(),
            "ready_limit_per_lane": 1,
            "review_order": "lowest-receipted-cycles-first",
            "review_mode": "deterministic-receipt-fast-path",
        }
        mapping["remote_actions"] = "local-shadow-git-only"
        return mapping

    def _allowed_paths(self) -> list[str]:
        return cast("list[str]", self.store.meta("allowed_paths"))

    def _plan(self, objective: str, cwd: Path | None = None) -> InitialPlan:
        if not self.git_pr_enabled:
            return super()._plan(objective, cwd)
        prompt = git_planning_prompt(
            objective=objective,
            workspace_map=self._workspace_map(),
            skill=self.skill_name,
        )
        failures: list[str] = []
        for attempt in range(1, 4):
            session = self.agents.coordinator.new(cwd=cwd or self.paths.planning)
            try:
                result = session(prompt, suppress=False, schema=InitialPlan)
            except Stopped:
                raise
            except Exception as why:  # noqa: BLE001 - backend failures are open-ended
                failures.append(
                    f"attempt {attempt}: {type(why).__name__}: {why}"[:1000]
                )
                result = None
            finally:
                close_safely(session)
            if result is not None:
                return result
            if len(failures) < attempt:
                failures.append(f"attempt {attempt}: no structured plan")
        raise RuntimeError(f"initial Git/PR plan failed: {failures}")

    def _prepare_lanes(self) -> None:
        if not self.git_pr_enabled:
            super()._prepare_lanes()
            return
        pairs = {
            "lane-1": (self.agents.lane_1_actor_a, self.agents.lane_1_actor_b),
            "lane-2": (self.agents.lane_2_actor_a, self.agents.lane_2_actor_b),
            "lane-3": (self.agents.lane_3_actor_a, self.agents.lane_3_actor_b),
        }
        for lane in self.lane_names:
            lane_state = cast("dict[str, Any]", self.control["lanes"][lane])
            self.lanes[lane] = self._make_lane_runtime(
                lane=lane,
                actors=pairs[lane],
                workspace=self.git_paths.lane(lane),
                actor_at=int(lane_state.get("next_actor", 0)) % 2,
            )

    def _lane_instructions(self, lane: LaneName) -> str:
        if not (
            self.git_pr_enabled
            or self.global_knowledge_enabled
            or self.experiment_memory_enabled
        ):
            return ""
        return lane_protocol(
            lane=lane,
            run_root=str(self.paths.root),
            cli=str(self.git_paths.bin / "pfc"),
            git_pr_enabled=self.git_pr_enabled,
            global_knowledge_enabled=self.global_knowledge_enabled,
            experiment_memory_enabled=self.experiment_memory_enabled,
            token_efficient_enabled=self.token_efficient_enabled,
            allowed_paths=self._allowed_paths(),
            knowledge_digest=(
                self.store.search_knowledge("", limit=KNOWLEDGE_PROMPT_LIMIT)
                if self.global_knowledge_enabled
                else []
            ),
            experiment_frontier=(
                self.store.experiment_frontier()
                if self.experiment_memory_enabled
                else {}
            ),
        )

    def _lane_ownership(self, lane: LaneName) -> str | None:
        if not self.git_pr_enabled:
            return None
        return (
            f"You are {lane}, an equal PR-authoring research lane. Work only in your assigned "
            "run-owned clone. Do not edit the original source, another lane's clone, the central "
            "repository, or the orchestrateor integration workspace. The source changes only "
            "after a receipt-verified candidate is selected and published by the runtime."
        )

    def _unread_reports(
        self, lane: LaneName, cursors: dict[str, Any]
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        reports, acknowledgements = super()._unread_reports(lane, cursors)
        consumer = cast("dict[str, Any]", cursors.setdefault(lane, {}))
        system, end = unread_system_reports(
            self.git_paths.system_reports / f"{lane}.jsonl",
            consumer.get("system", 0),
        )
        return [*reports, *system], {**acknowledgements, "system": end}

    def _emit_system(
        self,
        *,
        targets: tuple[LaneName, ...],
        kind: str,
        summary: str,
        payload: dict[str, object],
    ) -> None:
        report = publish_system_report(
            self.git_paths.system_reports,
            targets=cast("tuple[str, ...]", targets),
            kind=kind,
            summary=summary,
            payload=payload,
        )
        self.store.record_telemetry(
            "system_report_published",
            {"report_id": report["report_id"], "targets": list(targets), "kind": kind},
        )

    def _observe_report(
        self,
        runtime: LaneRuntime,
        record: dict[str, object],
        report: LaneReport,
        *,
        candidate_became_best: bool,
    ) -> None:
        super()._observe_report(
            runtime,
            record,
            report,
            candidate_became_best=candidate_became_best,
        )
        self.store.record_telemetry(
            "lane_report_recorded",
            {
                "report_id": cast("str", record["report_id"]),
                "status": report.status,
                "candidate_became_best": candidate_became_best,
            },
            lane=runtime.lane,
        )
        if self.experiment_memory_enabled:
            linked = self.store.attach_experiment_report(
                runtime.lane, cast("str", record["report_id"])
            )
            if linked is None:
                self.store.record_telemetry(
                    "experiment_report_unlinked",
                    {
                        "report_id": record["report_id"],
                        "status": report.status,
                    },
                    lane=runtime.lane,
                )
        if self.global_knowledge_enabled and candidate_became_best:
            experience = self.store.add_experience(
                title=(
                    report.submission.title
                    if report.submission is not None
                    else f"Shared best from {runtime.lane}"
                ),
                summary=report.summary[:300],
                scope=(runtime.lane, "shared-candidate-best"),
                evidence=(cast("str", record["report_id"]), *report.tests[:3]),
                pr_id=None,
                commit_sha=None,
            )
            self.store.compact_experiences(limit=KNOWLEDGE_DIGEST_LIMIT)
            knowledge_state = cast("dict[str, Any]", self.control["knowledge"])
            knowledge_state["digest_updates"] = (
                int(knowledge_state.get("digest_updates", 0)) + 1
            )
            knowledge_state["last_summary"] = report.summary[:300]
            self._emit_system(
                targets=self.lane_names,
                kind="success_experience_created",
                summary="The compact digest accepted a new evaluator-backed shared best.",
                payload={
                    "experience_id": experience["id"],
                    "source_lane": runtime.lane,
                    "report_id": record["report_id"],
                },
            )
            self._refresh_views()

    def _record_failure(self, runtime: LaneRuntime, error: str) -> None:
        super()._record_failure(runtime, error)
        self.store.record_telemetry(
            "lane_turn_failed", {"error": error[:2000]}, lane=runtime.lane
        )

    def _review_workspace(self, pr_id: str) -> Path:
        reviews = self.git_paths.shared / "review-workspaces"
        reviews.mkdir(parents=True, exist_ok=True)
        workspace = reviews / pr_id
        if workspace.exists():
            git("rev-parse", "--is-inside-work-tree", cwd=workspace)
            return workspace
        git("fetch", "origin", cwd=self.git_paths.integration)
        git(
            "worktree",
            "add",
            "--detach",
            str(workspace),
            "origin/main",
            cwd=self.git_paths.integration,
        )
        return workspace

    def _remove_review_workspace(self, pr_id: str) -> None:
        workspace = self.git_paths.shared / "review-workspaces" / pr_id
        if not workspace.exists():
            return
        result = git(
            "worktree",
            "remove",
            "--force",
            str(workspace),
            cwd=self.git_paths.integration,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"could not remove closed review worktree {workspace}")
        git("worktree", "prune", cwd=self.git_paths.integration)

    def _start_pr_review(self) -> bool:
        if not self.git_pr_enabled or self.pr_review.future is not None:
            return False
        active = self.store.active_review()
        if active is None:
            active = self.store.activate_next_pr()
        if active is None:
            return False
        pr_id = cast("str", active["id"])
        workspace = self._review_workspace(pr_id)
        prompt = pr_review_prompt(
            objective=cast("str", self.control["objective"]),
            pr=active,
            prior_main_sha=cast(
                "str",
                cast("dict[str, Any]", self.control["git_pr"])["observed_main_sha"],
            ),
            cli=str(self.git_paths.bin / "pfc"),
            allowed_paths=self._allowed_paths(),
            ledger=self.store.ledger(),
        )
        session = self.agents.orchestrateor.new(cwd=workspace)
        self.pr_review = PRReviewWork(
            future=self.executor.submit(run_pr_review, session, prompt),
            session=session,
            pr_id=pr_id,
            workspace=workspace,
        )
        attempts = cast(
            "dict[str, int]",
            cast("dict[str, Any]", self.control["git_pr"])["review_attempts"],
        )
        attempts[pr_id] = int(attempts.get(pr_id, 0)) + 1
        self.store.record_telemetry(
            "orchestrateor_review_session_started",
            {"pr_id": pr_id, "attempt": attempts[pr_id]},
        )
        return True

    def _collect_pr_review(self) -> bool:
        future = self.pr_review.future
        if future is None or not future.done():
            return False
        pr_id = cast("str", self.pr_review.pr_id)
        result: PRReviewResult | None = None
        error: str | None = None
        try:
            result = future.result()
        except Stopped:
            raise
        except Exception as why:  # noqa: BLE001 - preserve backend diagnostics
            error = f"{type(why).__name__}: {why}"[:2000]
        close_safely(self.pr_review.session)
        self.pr_review = PRReviewWork()
        if result is None or result.pr_id != pr_id:
            pr = self.store.pr(pr_id)
            if pr["status"] == "rejected":
                self._emit_system(
                    targets=(cast("LaneName", pr["lane"]),),
                    kind="pr_rejected",
                    summary=cast(
                        "str", pr["rejection_reason"] or "PR explicitly rejected"
                    ),
                    payload={"pr_id": pr_id, "recovered_result": True},
                )
                self._remove_review_workspace(pr_id)
            self.store.record_telemetry(
                "orchestrateor_review_invalid",
                {"pr_id": pr_id, "error": error or "missing/mismatched result"},
            )
            return True

        pr = self.store.pr(pr_id)
        if result.verdict == "rejected":
            if pr["status"] != "rejected":
                self.store.record_telemetry(
                    "orchestrateor_review_invalid",
                    {"pr_id": pr_id, "error": "reject command was not completed"},
                )
                return True
            self._emit_system(
                targets=(cast("LaneName", pr["lane"]),),
                kind="pr_rejected",
                summary=result.summary,
                payload={"pr_id": pr_id, "evidence": result.evidence},
            )
            self._remove_review_workspace(pr_id)
        elif result.verdict == "merged":
            current = main_sha(self.git_paths.central)
            receipts = self.store.staging_receipts(pr_id, current)
            if result.merge_commit != current or not receipts:
                self.store.record_telemetry(
                    "orchestrateor_review_invalid",
                    {
                        "pr_id": pr_id,
                        "error": "merged result does not match protected main/receipt",
                        "reported_commit": result.merge_commit,
                        "current_main": current,
                    },
                )
            cast("dict[str, Any]", self.control["git_pr"])["pending_comparison"] = {
                "pr_id": pr_id,
                "trusted_orchestrateor": True,
                "summary": result.summary,
                "evidence": result.evidence,
                "reported_receipt_ids": result.evaluation_receipt_ids,
            }
        else:
            self._emit_system(
                targets=(cast("LaneName", pr["lane"]),),
                kind="pr_review_continues",
                summary=result.summary,
                payload={"pr_id": pr_id, "evidence": result.evidence},
            )
        self.store.record_telemetry(
            "orchestrateor_review_result",
            {
                "pr_id": pr_id,
                "verdict": result.verdict,
                "summary": result.summary,
            },
        )
        return True

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _receipt_score(self, pr: dict[str, object]) -> tuple[int, str]:
        """Validate the frozen receipt and extract AOPT's objective value."""
        receipt_id = cast("str", pr["provisional_receipt_id"])
        receipt = self.store.receipt(receipt_id)
        expected_command = cast(
            "list[str]", self.store.meta("trusted_evaluator_command")
        )
        recorded_command = json.loads(cast("str", receipt["command_json"]))
        if expected_command and recorded_command != expected_command:
            raise ValueError("receipt did not use the frozen evaluator command")
        if (
            receipt["kind"] != "provisional"
            or int(receipt["exit_code"]) != 0
            or receipt["commit_sha"] != pr["head_sha"]
        ):
            raise ValueError("receipt is not a successful frozen-head evaluation")
        tree = cast(
            "subprocess.CompletedProcess[str]",
            git(
                "rev-parse",
                f"{pr['head_sha']}^{{tree}}",
                cwd=self.git_paths.central,
            ),
        ).stdout.strip()
        if receipt["tree_sha"] != tree:
            raise ValueError("receipt tree does not match its PR head")
        evaluation_root = self.git_paths.evaluation_artifacts.resolve()
        artifacts: dict[str, Path] = {}
        for path_field, hash_field in (
            ("stdout_path", "stdout_sha256"),
            ("stderr_path", "stderr_sha256"),
        ):
            path = Path(cast("str", receipt[path_field]))
            resolved = path.resolve(strict=True)
            if (
                not resolved.is_relative_to(evaluation_root)
                or path.is_symlink()
                or not path.is_file()
                or self._sha256(path) != receipt[hash_field]
            ):
                raise ValueError("receipt artifact integrity check failed")
            artifacts[path_field] = path
        output = artifacts["stdout_path"].read_text(encoding="utf-8", errors="replace")
        matches = CYCLES_PATTERN.findall(output)
        if not matches:
            raise ValueError("official evaluator output has no CYCLES value")
        return int(matches[-1]), receipt_id

    def _reject_fast_path(self, pr: dict[str, object], reason: str) -> None:
        rejected = self.store.reject_pr(pr_id=cast("str", pr["id"]), reason=reason)
        self._emit_system(
            targets=(cast("LaneName", rejected["lane"]),),
            kind="pr_rejected",
            summary=reason,
            payload={"pr_id": rejected["id"], "fast_path": True},
        )

    def _current_official_score(self) -> int | None:
        ledger = self.store.ledger()
        if not ledger:
            return None
        score = cast("dict[str, object]", ledger[-1]["comparison"]).get("score")
        return (
            int(score)
            if isinstance(score, int) and not isinstance(score, bool)
            else None
        )

    def _lane_update_context(
        self, lane: LaneName, *, current: str, changed: list[str]
    ) -> dict[str, object]:
        """Inspect one lane cheaply after fetching the new protected main ref."""
        workspace = self.git_paths.lane(lane)
        fetch = git(
            "fetch",
            "--no-write-fetch-head",
            "origin",
            "main:refs/remotes/origin/main",
            cwd=workspace,
            check=False,
        )
        if fetch.returncode != 0:
            return {"state": "fetch-busy", "main_sha": current[:12]}
        head = cast(
            "subprocess.CompletedProcess[str]",
            git("rev-parse", "HEAD", cwd=workspace),
        ).stdout.strip()
        status = cast(
            "subprocess.CompletedProcess[str]",
            git(
                "status",
                "--porcelain",
                "--untracked-files=all",
                cwd=workspace,
                check=False,
            ),
        ).stdout.splitlines()
        counts = (
            cast(
                "subprocess.CompletedProcess[str]",
                git(
                    "rev-list",
                    "--left-right",
                    "--count",
                    "HEAD...origin/main",
                    cwd=workspace,
                    check=False,
                ),
            )
            .stdout.strip()
            .split()
        )
        divergence: dict[str, int] | None = None
        if len(counts) == 2 and all(item.isdigit() for item in counts):
            divergence = {"ahead": int(counts[0]), "behind": int(counts[1])}
        lane_paths = cast(
            "subprocess.CompletedProcess[str]",
            git(
                "diff",
                "--name-only",
                "origin/main...HEAD",
                cwd=workspace,
                check=False,
            ),
        ).stdout.splitlines()
        overlap = sorted(set(changed) & set(lane_paths))
        conflict = "not-checked"
        if divergence and divergence["ahead"] and divergence["behind"]:
            merge = git(
                "merge-tree",
                "--write-tree",
                "HEAD",
                "origin/main",
                cwd=workspace,
                check=False,
            )
            conflict = "clean" if merge.returncode == 0 else "conflict-likely"
        return {
            "head_sha": head[:12],
            "divergence": divergence,
            "dirty_paths": len(status),
            "overlap": overlap[:8],
            "conflict": conflict,
        }

    def _notify_main_update(
        self,
        *,
        prior: str,
        current: str,
        merged: dict[str, object],
        changed: list[str],
        comparison: dict[str, object],
    ) -> None:
        """Naturally steer one compact update into every active model turn."""
        if not self.main_update_monitor_enabled:
            return
        score = comparison.get("score")
        prior_score = comparison.get("prior_score")
        for lane in self.lane_names:
            context = self._lane_update_context(lane, current=current, changed=changed)
            message = (
                "[Main Update Monitor — runtime event]\n"
                f"main {prior[:12]} -> {current[:12]}; PR {merged['id']} from "
                f"{merged['lane']}; score {prior_score} -> {score}; changed "
                f"{', '.join(changed[:8]) or '(none)'}.\n"
                f"Your lane state: {json.dumps(context, ensure_ascii=False)}.\n"
                "Decide briefly whether to rebase, continue the local hypothesis, or combine "
                "both. Do not answer this event separately and do not stop useful work; record "
                "the decision in the normal LaneReport."
            )
            runtime = self.lanes.get(lane)
            injected = False
            error: str | None = None
            if runtime is not None and runtime.future is not None and runtime.session:
                try:
                    runtime.session.interject(message)
                    injected = True
                except Exception as why:  # noqa: BLE001 - durable fallback is mandatory
                    error = f"{type(why).__name__}: {why}"[:1000]
            if not injected:
                self._emit_system(
                    targets=(lane,),
                    kind="main_update_monitor",
                    summary=message,
                    payload={"main_sha": current, "injection_error": error},
                )
            self.store.record_telemetry(
                "main_update_monitor_delivered",
                {
                    "main_sha": current,
                    "delivery": "interject" if injected else "durable-report",
                    "context": context,
                    "error": error,
                },
                lane=lane,
            )

    def _process_fast_path(self) -> bool:
        """Select the best valid receipt and publish its exact tested tree."""
        if not self.git_pr_enabled:
            return False
        active = self.store.active_review()
        selected_score: int | None = None
        receipt_id: str | None = None
        if active is None:
            ranked: list[tuple[int, str, str, dict[str, object]]] = []
            for ready in self.store.prs(status="ready"):
                try:
                    score, _candidate_receipt = self._receipt_score(ready)
                except (KeyError, OSError, ValueError) as why:
                    activated = self.store.activate_pr(cast("str", ready["id"]))
                    if activated is not None:
                        self._reject_fast_path(
                            activated, f"Receipt fast-path rejected the PR: {why}"
                        )
                    return True
                ranked.append(
                    (
                        score,
                        cast("str", ready["ready_at"]),
                        cast("str", ready["id"]),
                        ready,
                    )
                )
            if not ranked:
                return False
            selected_score, _ready_at, _pr_id, candidate = min(ranked)
            active = self.store.activate_pr(cast("str", candidate["id"]))
            if active is None:
                return False
            selected_score, receipt_id = self._receipt_score(active)
        else:
            selected_score, receipt_id = self._receipt_score(active)

        current_score = self._current_official_score()
        if current_score is not None and selected_score >= current_score:
            self._reject_fast_path(
                active,
                f"Candidate cycles {selected_score} did not improve main {current_score}.",
            )
            return True
        git_state = cast("dict[str, Any]", self.control["git_pr"])
        prior = cast("str", git_state["observed_main_sha"])
        validate_changed_paths(
            self.git_paths.central,
            prior,
            cast("str", active["head_sha"]),
            self._allowed_paths(),
        )
        comparison: dict[str, object] = {
            "pr_id": active["id"],
            "review_mode": "receipt_fast_path",
            "score": selected_score,
            "prior_score": current_score,
            "receipt_id": receipt_id,
            "summary": (
                f"Auto-accepted exact evaluated tree at {selected_score} cycles"
                + (
                    f", improving {current_score}."
                    if current_score is not None
                    else "."
                )
            ),
            "evidence": [receipt_id],
        }
        try:
            merge_sha = create_fast_path_merge(
                self.git_paths,
                pr_id=cast("str", active["id"]),
                prior_sha=prior,
                head_sha=cast("str", active["head_sha"]),
            )
        except (RuntimeError, subprocess.CalledProcessError) as why:
            self._reject_fast_path(
                active, f"Deterministic integration failed: {str(why)[:1000]}"
            )
            return True
        git_state["pending_comparison"] = comparison
        self.store.record_telemetry(
            "pr_fast_path_published",
            {
                "pr_id": active["id"],
                "merge_sha": merge_sha,
                "score": selected_score,
                "receipt_id": receipt_id,
            },
            lane=cast("str", active["lane"]),
        )
        return True

    def _observe_main(self) -> bool:
        if not self.git_pr_enabled:
            return False
        git_state = cast("dict[str, Any]", self.control["git_pr"])
        prior = cast("str", git_state["observed_main_sha"])
        current = main_sha(self.git_paths.central)
        if current == prior:
            return False
        parents = commit_parents(self.git_paths.central, current)
        pr_id = pr_trailer(self.git_paths.central, current)
        if pr_id is None or len(parents) != MERGE_PARENT_COUNT or parents[0] != prior:
            raise RuntimeError(
                "protected main advanced with an invalid merge structure"
            )
        pr = self.store.pr(pr_id)
        if parents[1] != pr["head_sha"]:
            raise RuntimeError("protected main does not merge the frozen PR head")
        changed = validate_changed_paths(
            self.git_paths.central, prior, current, self._allowed_paths()
        )
        publish_main(
            self.git_paths.central,
            self.source,
            prior_sha=prior,
            merge_sha=current,
        )
        already_finalized = pr["status"] == "merged"
        pending = git_state.get("pending_comparison")
        comparison = (
            pending
            if isinstance(pending, dict) and pending.get("pr_id") == pr_id
            else {
                "pr_id": pr_id,
                "review_mode": "receipt_fast_path",
                "recovered_after_restart": True,
            }
        )
        merged = self.store.finalize_merge(
            pr_id=pr_id,
            prior_main_sha=prior,
            merge_sha=current,
            comparison=cast("dict[str, object]", comparison),
        )
        if not already_finalized:
            receipts = self.store.qualifying_receipts(pr_id, current)
            experience_id: object = None
            if self.global_knowledge_enabled:
                experience = self.store.add_experience(
                    title=cast("str", pr["title"]),
                    summary=cast("str", comparison.get("summary", pr["hypothesis"])),
                    scope=("merged-main", *changed),
                    evidence=tuple(cast("str", item["id"]) for item in receipts),
                    pr_id=pr_id,
                    commit_sha=current,
                )
                self.store.compact_experiences(limit=KNOWLEDGE_DIGEST_LIMIT)
                experience_id = experience["id"]
            self._emit_system(
                targets=self.lane_names,
                kind="pr_merged",
                summary=f"{pr_id} was approved and published to the source workspace.",
                payload={
                    "pr_id": pr_id,
                    "author_lane": merged["lane"],
                    "merge_sha": current,
                    "changed_paths": changed,
                    "experience_id": experience_id,
                },
            )
            self._emit_system(
                targets=(cast("LaneName", merged["lane"]),),
                kind="receipt_fast_path_feedback",
                summary=cast("str", comparison.get("summary", "PR merged")),
                payload={"pr_id": pr_id, "evidence": comparison.get("evidence", [])},
            )
            self._notify_main_update(
                prior=prior,
                current=current,
                merged=merged,
                changed=changed,
                comparison=cast("dict[str, object]", comparison),
            )
        git_state["observed_main_sha"] = current
        git_state["pending_comparison"] = None
        self._remove_review_workspace(pr_id)
        self.store.record_telemetry(
            "main_published_to_source",
            {"pr_id": pr_id, "merge_sha": current, "changed_paths": changed},
        )
        self._refresh_views()
        return True

    def _scan_receipts(self) -> bool:
        git_state = cast("dict[str, Any]", self.control["git_pr"])
        cursor = int(git_state.get("receipt_cursor", 0))
        receipts, end = self.store.receipts_after(cursor)
        for receipt in receipts:
            if int(receipt["exit_code"]) == 0:
                continue
            target: LaneName | None = None
            if receipt["lane"] in self.lane_names:
                target = cast("LaneName", receipt["lane"])
            elif receipt.get("pr_id"):
                target = cast(
                    "LaneName", self.store.pr(cast("str", receipt["pr_id"]))["lane"]
                )
            if target is not None:
                self._emit_system(
                    targets=(target,),
                    kind="invalid_evaluation_receipt",
                    summary="An evaluation receipt failed and cannot qualify a PR.",
                    payload={
                        "receipt_id": receipt["id"],
                        "pr_id": receipt.get("pr_id"),
                        "commit_sha": receipt["commit_sha"],
                        "exit_code": receipt["exit_code"],
                    },
                )
        git_state["receipt_cursor"] = end
        return end != cursor

    def _knowledge_markdown(self, index: dict[str, object]) -> str:
        lines = ["# Run-local global knowledge", ""]
        lines.extend(("## Verified facts", ""))
        facts = cast("list[dict[str, object]]", index["facts"])
        if not facts:
            lines.extend(("No verified facts yet.", ""))
        for fact in facts:
            lines.extend(
                (
                    f"### {fact['id']}",
                    "",
                    cast("str", fact["statement"]),
                    "",
                    f"- Scope: {json.dumps(fact.get('scope', []), ensure_ascii=False)}",
                    f"- Proof: {fact.get('proof', '')}",
                    f"- Evidence: {json.dumps(fact.get('evidence', []), ensure_ascii=False)}",
                    "",
                )
            )
        lines.extend(("## Accepted success experiences", ""))
        experiences = cast("list[dict[str, object]]", index["experiences"])
        if not experiences:
            lines.extend(("No accepted experiences yet.", ""))
        for experience in experiences:
            lines.extend(
                (
                    f"### {experience['id']}: {experience['title']}",
                    "",
                    cast("str", experience["summary"]),
                    "",
                    f"- Method: {experience.get('method', '')}",
                    f"- Why it worked: {experience.get('why_it_worked', '')}",
                    "- Limitations: "
                    + json.dumps(experience.get("limitations", []), ensure_ascii=False),
                    "",
                )
            )
        return "\n".join(lines)

    def _refresh_views(self) -> None:
        index = self.store.knowledge_index()
        atomic_json(self.git_paths.knowledge_json, index)
        atomic_text(self.git_paths.knowledge_markdown, self._knowledge_markdown(index))
        atomic_json(
            self.git_paths.official_ledger,
            {"version": 1, "entries": self.store.ledger()},
        )
        atomic_json(
            self.git_paths.shared / "experiment-memory.json",
            {
                "version": 1,
                "enabled": self.experiment_memory_enabled,
                "frontier": self.store.experiment_frontier(),
            },
        )

    def _before_persist(self) -> None:
        super()._before_persist()
        self._refresh_views()

    def _manifest_fields(self) -> dict[str, object]:
        fields = super()._manifest_fields()
        return {
            **fields,
            "factorial_cell": {
                "git_pr_enabled": self.git_pr_enabled,
                "global_knowledge_enabled": self.global_knowledge_enabled,
                "experiment_memory_enabled": self.experiment_memory_enabled,
                "token_efficient_enabled": self.token_efficient_enabled,
                "main_update_monitor_enabled": self.main_update_monitor_enabled,
            },
            "git_pr": json_copy(self.control.get("git_pr")),
            "knowledge": json_copy(self.control.get("knowledge")),
            "experiment_memory": (
                str(self.git_paths.shared / "experiment-memory.json")
                if self.experiment_memory_enabled
                else None
            ),
            "pull_requests": self.store.prs(),
            "official_ledger": str(self.git_paths.official_ledger),
            "knowledge_index": str(self.git_paths.knowledge_json),
        }

    def _control_cycle(self) -> None:
        changed = self._scan_receipts()
        changed = self._observe_main() or changed
        changed = self._process_fast_path() or changed
        changed = self._observe_main() or changed
        if changed:
            self._persist()

    def _close_sessions(self) -> None:
        super()._close_sessions()
        close_safely(self.pr_review.session)


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
    """Execute one resumable factorial run."""
    GitPRRuntime(
        agents,
        task,
        config,
        state,
        clock=_clock,
        sleeper=_sleep,
        max_turns=_max_turns,
    ).run()


__all__ = ["GitPRRuntime", "execute"]
