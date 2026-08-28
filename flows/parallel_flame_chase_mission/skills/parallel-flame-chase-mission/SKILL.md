---
name: parallel-flame-chase-mission
description: Work safely as a coordinator or alternating lane actor in Mission Parallel Flame Chase. Use for falsifiable missions, private-lane research, Lane 1 integration, durable reports and artifacts, scoped audits, interruption checkpoints, continuation, and bounded external evidence.
---

# Mission Parallel Flame Chase

Treat the runtime prompt as the authoritative role, objective, workspace map, mission identity,
and audit revision. Never infer ownership from the current directory alone.

Read [collaboration.md](references/collaboration.md) before acting. It defines source ownership,
durable handoffs, reports, artifacts, and alternating fresh sessions.

Then route by role:

- Initial coordinator: plan only. Give all three lanes distinct, falsifiable work; make Lane 1 the
  integration owner while allowing every lane to produce locally evaluated candidates.
- Lane actor: do the assigned work and return an evidence-backed `LaneReport`.
- Audit coordinator: also read [mission-audit.md](references/mission-audit.md). Decide only the
  exact immutable audit packet and targets.
- A resumed run, changed TASK.md, checkpoint recovery, or external event:
  also read [state-events.md](references/state-events.md).

This mission flow owns local coordination, including task-provided local evaluator submissions.
Those evaluator calls do not authorize uploading, deploying, publishing, remotely submitting to
a competition or service, purchasing, messaging, or any other domain-specific remote action. A
separate caller remains responsible for such an action and its authorization.
