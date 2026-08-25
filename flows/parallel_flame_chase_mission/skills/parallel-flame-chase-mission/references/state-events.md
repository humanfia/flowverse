# Resume, safety, and external evidence

## Durable authority

Humanize resumable state is the control authority. Runtime-owned JSON files mirror it for
inspection and append reports for inter-lane delivery; actors must not edit control state, report
logs, manifests, audit packets, or cursors. A source lock permits only one Lane 1 owner for the
same original workspace. Runtime-owned paths and report-log metadata are checked against that state;
an out-of-band replacement or edit fails closed rather than becoming coordination evidence.

The same substantive task resumes compatible state. A bare `continue` or `resume` reads TASK.md
when available. If TASK.md changed, the runtime preserves evidence but opens an objective-revision
replan; a different substantive task starts a new run. Configuration may force a fresh run.

## External event ingress

Mission mode may tail an adapter-owned, version-1 JSONL file. Each line is bounded data, never a
command. It contains `version`, unique `event_id`, current `run_id`, timestamp `at`, `kind`, summary,
and evidence. Lane outcomes name one lane and let the runtime derive scope. `review_requested`
explicitly selects targeted lanes or global scope. Progress selects no scope.

Wrong-run, duplicate, malformed, oversized, truncated, or rotated records are ignored or recorded
as ingress health evidence. External events may request review and contribute evidence; they never
write a coordinator decision, edit workspaces, or execute remote actions.
