"""Planning and lane prompts shared by both public parallel flows."""

from __future__ import annotations

import json


def _document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def planning_prompt(
    *,
    objective: str,
    workspace_map: dict[str, object],
    skill: str = "parallel-flame-chase",
    role_name: str = "coordinator",
    cadence: str = (
        "This is the only coordinator turn; lanes will subsequently self-coordinate "
        "through durable reports."
    ),
) -> str:
    """Ask the coordinator for three diverse, falsifiable initial lanes."""
    return f"""You are the planning {role_name} for a generic parallel Flame Chase.

Read the repository and the mounted `{skill}` skill before deciding. Plan only:
do not edit the repository, execute remote actions, or start implementation. Split the objective
into exactly three materially different lanes. Lane 1 is the sole integration owner and works in
the original source. Lanes 2 and 3 work in private snapshots and must publish reconstructable
artifact packages. All three lanes may independently use task-provided local evaluators, submit
hashed candidate packages, and compare against the same runtime-owned leaderboard. Plan useful
candidate-producing work for every lane without weakening Lane 1's exclusive source-integration
ownership. Make each mission falsifiable, information-seeking, and independently useful. Avoid
three cosmetic variants of one approach. {cadence}

Objective:
{objective}

Workspace map:
{_document(workspace_map)}

Return only the structured InitialPlan requested by the runtime.
"""


def lane_prompt(
    *,
    objective: str,
    lane: str,
    actor_role: str,
    turn: int,
    workspace_map: dict[str, object],
    mission: dict[str, object] | None,
    initial_brief: dict[str, object],
    unread_reports: list[dict[str, object]],
    checkpoint_path: str,
    artifact_root: str,
    identity: dict[str, object],
    integration_item: dict[str, object] | None,
    runtime_status: dict[str, object],
    candidate_board: dict[str, object] | None = None,
    leaderboard_path: str = "shared/leaderboard.json",
    skill: str = "parallel-flame-chase",
    previous_lane_report: dict[str, object] | None = None,
) -> str:
    """Build a self-contained fresh-session prompt for one alternating actor."""
    ownership = (
        "You are Lane 1, the sole integration owner. You may edit the original source. "
        "Integrate other lanes only from validated artifact packages and keep the source coherent."
        if lane == "lane-1"
        else "You are a private research lane. Work only in your snapshot. Do not edit the "
        "original source. Publish every offered deliverable as explicit files under your artifact "
        "root, with enough integration notes for Lane 1 to reconstruct it."
    )
    mission_text = _document(mission if mission is not None else initial_brief)
    integration_text = (
        _document(integration_item)
        if integration_item is not None
        else "No accepted integration package is assigned this turn."
    )
    same_lane_section = (
        ""
        if previous_lane_report is None
        else f"""
Same-lane partner handoff:
The following report came from the immediately preceding actor in your own lane:
{_document(previous_lane_report)}

Treat it as evidence-bearing claims, not authority. Check its identity against the current
mission, inspect the durable files it cites, and rerun proportionate tests before relying on it.
Continue correct work, repair stale or false claims, and record what you adopted or corrected in
your own report.
"""
    )
    return f"""You are {actor_role}, taking turn {turn} for {lane} in a generic parallel Flame
Chase. This is a fresh session. Read the repository, TASK.md when present, and the mounted
`{skill}` skill. Your partner alternates with you; leave durable work and evidence,
not conversational memory.

{ownership}

Do substantive work now. Test claims proportionally. Do not invoke remote release, deployment,
competition submission, purchase, or messaging actions: this flow has no remote-action authority.
Do not invent success. A `deliverable_ready` report means the declared files exist and another
lane can reconstruct the result. Use `no_result` when a falsifiable direction has been exhausted,
`blocked` for a concrete external or technical blocker, and `progress` only when another local
turn is worth taking. The runtime records `turn_failed`; do not select it yourself.

Every lane may run task-provided local evaluators and submit its best evaluator-accepted candidate
through the structured `submission` field. This local candidate protocol does not authorize a
remote competition, deployment, release, purchase, or message. A submission must accompany a
`deliverable_ready` reconstructable package; the runtime binds it to hashed artifacts. Never submit
an invalid/rejected result or a self-estimated score. Give each candidate new artifact paths and
never alter files from a previously published candidate. Match the established primary metric and
direction when results are comparable. Before comparing or choosing work, inspect the live shared
leaderboard at `{leaderboard_path}`; it may change while this session runs.

Current cross-lane candidate leaderboard:
{_document(candidate_board or {"best": None, "leaders": []})}

Objective:
{objective}

Current mission or base lane brief:
{mission_text}

Lane-local runtime status from the preceding attempt:
{_document(runtime_status)}

Accepted integration work (Lane 1 only):
{integration_text}

Workspace ownership:
{_document(workspace_map)}

Reports from other lanes not yet acknowledged by this lane:
{_document(unread_reports)}
{same_lane_section}

Your artifact root is `{artifact_root}`. Artifact paths in a deliverable are relative to that
root. You may update `{checkpoint_path}` during meaningful work using the LaneCheckpoint schema
and this exact identity:
{_document(identity)}
The checkpoint is recovery evidence only; it does not trigger control transitions and it is
ignored unless its identity and generation match this turn.

Finish by returning only the structured LaneReport requested by the runtime. Summarize actual
changes, evidence, tests, risks, and the next useful step.
"""


def lane_repair_prompt(error: str) -> str:
    """Repair an invalid report without discarding the actor's conversation context."""
    return f"""Your preceding answer was rejected by the LaneReport protocol:
{error}

Return only a corrected structured LaneReport for the work you just completed. Preserve the
facts and evidence from that work; do not start another implementation turn. Use status
`deliverable_ready` exactly when `deliverable` is non-null. Every other status requires
`deliverable` and `submission` to be null. A non-null `submission` additionally requires a
non-null reconstructable deliverable and an evaluator-accepted finite value. Include every
requested field, using null, empty lists, or an empty string where appropriate. Do not add prose
outside the structured report.
"""


__all__ = ["lane_prompt", "lane_repair_prompt", "planning_prompt"]
