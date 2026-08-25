# Resume, safety, and external evidence

## Durable authority

Humanize resumable state is the control authority. Runtime-owned JSON files mirror it for
inspection and append reports for inter-lane delivery; actors must not edit control state, report
logs, manifests, or cursors. A source lock permits only one Lane 1 owner for the
same original workspace. Runtime-owned paths and report-log metadata are checked against that state;
an out-of-band replacement or edit fails closed rather than becoming coordination evidence.

The same substantive task resumes compatible state. A bare `continue` or `resume` reads TASK.md
when available. If TASK.md changed, the runtime preserves the prior run and creates a fresh plan
and source snapshot for the revised objective; a different substantive task starts a new run.
Configuration may force a fresh run.

The report-driven flow has no external event ingress and no audit state. Checkpoints are recovery
evidence only; they never pause another lane or summon the planning coordinator.
