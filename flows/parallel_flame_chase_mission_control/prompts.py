"""Initial orchestrateor prompt for task-specific audit control."""

from __future__ import annotations

import json


def _document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def control_planning_prompt(
    *,
    objective: str,
    workspace_map: dict[str, object],
    runtime_defaults: dict[str, object],
    skill: str,
) -> str:
    """Ask the first-start orchestrateor for lanes and a complete audit policy."""
    return f"""You are the initial orchestrateor for Mission Control Parallel Flame Chase.

Read the repository, TASK.md when present, and the mounted `{skill}` skill. Read its audit-planning
reference before answering. Plan only: do not edit files, execute remote actions, or begin lane
work.

Create exactly three materially different, falsifiable missions. Lane 1 owns the original source;
Lanes 2 and 3 work in private snapshots. Every lane may produce locally evaluated candidates.

Also decide every supported audit condition exactly once. Base the policy on concrete evidence
about candidate-submission or evaluator-call limits, evaluator duration, feedback latency, total
time budget, integration risk, and audit interruption cost. Record unknown constraints rather than
inventing them. Actions mean:

- off: suppress the audit; terminal missions automatically continue with the suppressed outcome
  retained in their mission state;
- targeted: audit only the event's lane;
- global: audit all lanes;
- original: preserve the original Mission scope for that condition.

For overlapping conditions, such as a deliverable that also refreshes the shared best, the runtime
uses the strongest enabled action (global over targeted). A private-lane deliverable cannot enter
the integration queue without an audit acceptance decision. Setting every original condition to
`original`, setting `shared_best_updated` to `off`, and choosing a six-hour periodic review is
equivalent to the original Mission trigger behavior.

Objective:
{objective}

Workspace map:
{_document(workspace_map)}

Runtime fallbacks and available ingress:
{_document(runtime_defaults)}

Return only the structured MissionControlPlan requested by the runtime.
"""


__all__ = ["control_planning_prompt"]
