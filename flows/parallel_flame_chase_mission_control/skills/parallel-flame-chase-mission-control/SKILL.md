---
name: parallel-flame-chase-mission-control
description: Plan, execute, or audit a Mission Control Parallel Flame Chase whose first-start orchestrateor chooses and persists task-specific audit triggers and scopes.
---

# Mission Control Parallel Flame Chase

Treat the runtime prompt as authoritative for role, objective, ownership, mission identity, audit
revision, and persisted policy. Conversational memory never crosses fresh sessions.

Route by role:

- Initial orchestrateor: read [audit-planning.md](references/audit-planning.md), inspect the task
  constraints, and return the three missions plus a complete audit policy.
- Lane actor: read [collaboration.md](references/collaboration.md), execute the current mission,
  and return an evidence-backed `LaneReport`.
- Audit orchestrateor: read [mission-audit.md](references/mission-audit.md) and decide exactly the
  immutable targets named by the runtime.

The persisted policy governs whether runtime conditions become audits; it never grants remote
submission, deployment, release, purchase, or messaging authority.
