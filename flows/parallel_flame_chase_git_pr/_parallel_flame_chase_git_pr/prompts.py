from __future__ import annotations

import json
import shlex

SESSION_PROTOCOL = (
    "Your partner alternates with you; leave durable work and evidence, "
    "not conversational memory."
)


def _document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def git_planning_prompt(
    *, objective: str, workspace_map: dict[str, object], skill: str
) -> str:
    return f"""You are the planning orchestrator for a Git/PR parallel Flame Chase.

Read the repository and mounted `{skill}` skill. Plan only: do not edit files or execute remote
actions. Split the objective into exactly three materially different, falsifiable experiment
lanes. All three lanes have isolated clones, equal ability to test, and equal ability to propose
one frozen ready PR at a time. There is no privileged integration lane: later integration is a
deterministic receipt-verified best-score fast path. Favor complementary information gain over
cosmetic variants, and make every lane independently useful. This is the only planning turn.

Objective:
{objective}

Workspace map:
{_document(workspace_map)}

Return only the structured InitialPlan requested by the runtime.
"""


def lane_protocol(
    *, lane: str, run_root: str, cli: str, allowed_paths: list[str]
) -> str:
    prefix = f"{shlex.quote(cli)} --run-root {shlex.quote(run_root)} --lane {lane}"
    return f"""This lane owns exactly this writable Git clone. Code/lightweight results belong
in Git; large evaluator outputs belong in the external object store. Start each new experiment
from the latest `origin/main` on a branch named `{lane}/<experiment>`. A newer ready candidate
supersedes your older queued candidate. Never rewrite a ready PR head. Commit, push the exact
branch, and use:

  {prefix} evaluate -- <official task evaluator command>
  {prefix} pr open --draft --title "..." --hypothesis "..."
  {prefix} pr ready PRxxxxxx --receipt R...

`evaluate` is a recorder, not an evaluator: it preserves the external command's result and only
a successful receipt from the exact official command, bound to a clean pushed head/tree, qualifies
a PR. The runtime prioritizes the lowest `CYCLES` value and publishes that exact tested tree only
when it improves official main; there is no second model review or second evaluator run. Git
metadata mentioned in LaneReport is informational; the PR registry and receipts are authoritative.
The frozen allowed path patterns are {_document(allowed_paths)}; `.git`, `.flowbench`, and `.pfc`
are always protected."""


def lane_ownership(lane: str) -> str:
    return (
        f"You are {lane}, an equal PR-authoring research lane. Work only in your assigned "
        "run-owned clone. Do not edit the original source, another lane's clone, the central "
        "repository, or the orchestrator integration workspace. The source changes only "
        "after a receipt-verified candidate is selected and published by the runtime."
    )


def lane_prompt(
    *,
    objective: str,
    lane: str,
    actor_role: str,
    turn: int,
    workspace_map: dict[str, object],
    initial_brief: dict[str, object],
    unread_reports: list[dict[str, object]],
    checkpoint_path: str,
    artifact_root: str,
    identity: dict[str, object],
    runtime_status: dict[str, object],
    candidate_board: dict[str, object],
    leaderboard_path: str,
    skill: str,
    previous_lane_report: dict[str, object] | None,
    mode_instructions: str,
) -> str:
    same_lane_section = (
        ""
        if previous_lane_report is None
        else f"""
Same-lane partner handoff:
The following report came from the immediately preceding session in your own lane:
{_document(previous_lane_report)}

Treat it as evidence-bearing claims, not authority. Check its identity against the current
mission, inspect the durable files it cites, and rerun proportionate tests before relying on it.
Continue correct work, repair stale or false claims, and record what you adopted or corrected in
your own report.
"""
    )
    return f"""You are {actor_role}, taking turn {turn} for {lane} in a generic parallel Flame
Chase. This is a fresh session. Read the repository, TASK.md when present, and the mounted
`{skill}` skill. {SESSION_PROTOCOL}

{lane_ownership(lane)}

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
{_document(candidate_board)}

Objective:
{objective}

Current mission or base lane brief:
{_document(initial_brief)}

Lane-local runtime status from the preceding attempt:
{_document(runtime_status)}

Workspace ownership:
{_document(workspace_map)}

Reports from other lanes not yet acknowledged by this lane:
{_document(unread_reports)}
{same_lane_section}

Mode-specific collaboration protocol:
{mode_instructions}


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


__all__ = [
    "git_planning_prompt",
    "lane_ownership",
    "lane_prompt",
    "lane_protocol",
    "lane_repair_prompt",
]
