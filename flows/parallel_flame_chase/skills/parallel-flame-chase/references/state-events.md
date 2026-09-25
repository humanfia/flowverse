# Resume, safety, and external evidence

## Durable authority

Humanize resumable state is the control authority. Runtime-owned JSON files mirror it for
inspection, expose the candidate leaderboard, and append reports for inter-lane delivery; actors
must not edit control state, report logs, leaderboard state, manifests, or cursors. A source lock
permits only one Lane 1 owner for the same original workspace. Runtime-owned paths are checked
before the runtime writes them and are never written through a link, so an out-of-band
replacement fails closed rather than becoming coordination evidence.

The same substantive task resumes compatible state. A bare `continue` or `resume` reads TASK.md
when available. If TASK.md changed, the runtime preserves the prior run and creates a fresh plan
and source snapshot for the revised objective; a different substantive task starts a new run.
The `resume_mode=fresh` param forces a fresh run.

The report-driven flow has no external event ingress and no audit state. Checkpoints are recovery
evidence only; they never pause another lane or summon the planning coordinator.
