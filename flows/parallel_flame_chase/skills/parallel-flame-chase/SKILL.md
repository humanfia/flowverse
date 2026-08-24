---
name: parallel-flame-chase
description: Work safely as an initial coordinator or alternating lane actor in the report-driven Parallel Flame Chase. Use for initial three-lane planning, private-lane research, Lane 1 integration, durable reports and artifacts, checkpoints, and continuation without mission audits or coordinator interruptions.
---

# Report-driven Parallel Flame Chase

Treat the runtime prompt as the authoritative role, objective, workspace map, and turn identity.
Never infer ownership from the current directory alone.

Read [collaboration.md](references/collaboration.md) before acting. It defines source ownership,
durable handoffs, reports, artifacts, and alternating fresh sessions.

Then route by role:

- Initial coordinator: plan only. Give all three lanes distinct, falsifiable work; make Lane 1 the
  integration owner.
- Lane actor: do the assigned work and return an evidence-backed `LaneReport`.
- A resumed run, changed TASK.md, or checkpoint recovery: also read
  [state-events.md](references/state-events.md).

There is no audit coordinator in this flow. Terminal reports remain durable collaboration evidence;
they do not pause lanes or request a coordinator verdict.

This flow owns local coordination only. Do not upload, deploy, publish, submit, purchase, message,
or invoke any domain-specific remote action. A separate caller remains responsible for such an
action and its authorization.
