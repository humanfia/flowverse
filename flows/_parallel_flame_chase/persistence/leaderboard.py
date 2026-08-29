"""Runtime-owned cross-lane candidate leaderboard."""

from __future__ import annotations

from typing import cast

from ..core.models import LANES, CandidateSubmission
from ..core.utils import json_copy

LEADERBOARD_VERSION = 1


def empty_leaderboard(run_id: str) -> dict[str, object]:
    """Create an empty board whose first submission establishes the primary metric."""
    return {
        "version": LEADERBOARD_VERSION,
        "run_id": run_id,
        "submission_count": 0,
        "primary": None,
        "best": None,
        "leaders": [],
    }


def _metric_key(metric: str) -> str:
    return " ".join(metric.casefold().split())


def _identity(submission: CandidateSubmission) -> dict[str, str]:
    return {
        "metric_key": _metric_key(submission.metric),
        "direction": submission.direction,
    }


def validate_leaderboard(board: object, run_id: str) -> dict[str, object]:
    """Reject malformed or cross-run resumable leaderboard state."""
    if not isinstance(board, dict):
        raise TypeError("resumable candidate leaderboard is malformed")
    if board.get("version") != LEADERBOARD_VERSION:
        raise ValueError("unsupported candidate leaderboard version")
    if board.get("run_id") != run_id:
        raise ValueError("candidate leaderboard belongs to another run")
    count = board.get("submission_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("candidate submission_count must be a non-negative integer")
    leaders = board.get("leaders")
    if not isinstance(leaders, list):
        raise TypeError("candidate leaderboard leaders are malformed")
    identities: set[tuple[str, str]] = set()
    for leader in leaders:
        if not isinstance(leader, dict):
            raise TypeError("candidate leaderboard entry is malformed")
        metric_key = leader.get("metric_key")
        direction = leader.get("direction")
        best = leader.get("best")
        if (
            not isinstance(metric_key, str)
            or not metric_key
            or direction not in {"minimize", "maximize"}
            or not isinstance(best, dict)
        ):
            raise ValueError("candidate leaderboard entry is incomplete")
        identity = (metric_key, cast("str", direction))
        if identity in identities:
            raise ValueError("candidate leaderboard repeats a metric and direction")
        identities.add(identity)
    primary = board.get("primary")
    best = board.get("best")
    if primary is None:
        if best is not None or leaders or count:
            raise ValueError("non-empty candidate leaderboard has no primary metric")
    elif (
        not isinstance(primary, dict)
        or not isinstance(primary.get("metric_key"), str)
        or primary.get("direction") not in {"minimize", "maximize"}
        or not isinstance(best, dict)
        or (cast("str", primary["metric_key"]), cast("str", primary["direction"]))
        not in identities
    ):
        raise ValueError("candidate leaderboard primary metric is malformed")
    return cast("dict[str, object]", board)


def _candidate_record(
    report: dict[str, object], submission: CandidateSubmission
) -> dict[str, object]:
    lane = report.get("lane")
    artifacts = report.get("artifacts")
    if lane not in LANES:
        raise ValueError("candidate report has an invalid lane")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("candidate submission has no hashed artifacts")
    required = ("report_id", "at", "run_id", "actor", "turn", "generation")
    if any(report.get(field) is None for field in required):
        raise ValueError("candidate report identity is incomplete")
    return {
        "version": 1,
        "submission_id": report["report_id"],
        "report_id": report["report_id"],
        "at": report["at"],
        "run_id": report["run_id"],
        "lane": lane,
        "actor": report["actor"],
        "turn": report["turn"],
        "mission_id": report.get("mission_id"),
        "generation": report["generation"],
        **submission.model_dump(mode="json"),
        "artifacts": json_copy(artifacts),
    }


def _is_better(candidate: dict[str, object], incumbent: dict[str, object]) -> bool:
    value = float(cast("float", candidate["value"]))
    held = float(cast("float", incumbent["value"]))
    return value < held if candidate["direction"] == "minimize" else value > held


def with_submission(
    board: dict[str, object], report: dict[str, object]
) -> tuple[dict[str, object], dict[str, object], bool]:
    """Return an updated board, immutable candidate record, and global-best flag."""
    validated = validate_leaderboard(board, cast("str", report.get("run_id")))
    submission = CandidateSubmission.model_validate(report.get("submission"))
    candidate = _candidate_record(report, submission)
    updated = json_copy(validated)
    updated["submission_count"] = int(updated["submission_count"]) + 1
    identity = _identity(submission)
    leaders = cast("list[dict[str, object]]", updated["leaders"])
    leader = next(
        (
            held
            for held in leaders
            if held.get("metric_key") == identity["metric_key"]
            and held.get("direction") == identity["direction"]
        ),
        None,
    )
    if leader is None:
        leader = {**identity, "metric": submission.metric, "best": candidate}
        leaders.append(leader)
    elif _is_better(candidate, cast("dict[str, object]", leader["best"])):
        leader["best"] = candidate

    if updated["primary"] is None:
        updated["primary"] = identity
    primary = cast("dict[str, str]", updated["primary"])
    is_primary = primary == identity
    became_best = False
    if is_primary:
        prior = updated.get("best")
        if prior is None or _is_better(candidate, cast("dict[str, object]", prior)):
            updated["best"] = candidate
            became_best = True
    updated["updated_at"] = candidate["at"]
    validate_leaderboard(updated, cast("str", candidate["run_id"]))
    return updated, candidate, became_best


__all__ = [
    "LEADERBOARD_VERSION",
    "empty_leaderboard",
    "validate_leaderboard",
    "with_submission",
]
