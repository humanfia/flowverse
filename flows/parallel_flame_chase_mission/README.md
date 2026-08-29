# Mission Parallel Flame Chase

`parallel_flame_chase_mission` is the audit-governed Parallel Flame Chase. It is a distinct public
flow from the report-only `parallel_flame_chase`: it has its own config model, mounted skill, and
resumable Humanize state, and it is invoked without a colon-qualified subflow name.

## Topology

Pass exactly seven agents in this order:

1. initial planning and audit coordinator
2. Lane 1 actor A
3. Lane 1 actor B
4. Lane 2 actor A
5. Lane 2 actor B
6. Lane 3 actor A
7. Lane 3 actor B

```console
hmz exec -f ./flows/parallel_flame_chase_mission \
  -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -c ./flows/parallel_flame_chase_mission/examples/mission.yaml \
  "$(cat TASK.md)"
```

Lane 1 is the sole writer and integration owner for the original source. Lanes 2 and 3 work in
private snapshots and publish hashed, reconstructable artifact packages. All actor turns are fresh
sessions and A/B alternation is durable across restarts.

All three lanes may use task-provided local evaluators and attach accepted candidates to their
hashed packages. A runtime-owned `shared/leaderboard.json` exposes the primary cross-lane best to
every lane prompt and to Mission audit manifests. This does not change Lane 1's exclusive source
integration ownership or authorize remote submissions.

## Package layout

Mission-only code is grouped by responsibility, leaving the package root as the public flow
boundary:

```text
parallel_flame_chase_mission/
├── __init__.py       # public flow and Mission config
├── coordination/    # mission state machine, decision models, external events
├── runtime/         # Mission engine, lane state, scheduling hooks
├── audits/          # prompts, coordinator retries, scoped audit scheduling
├── examples/        # Mission configuration
└── skills/          # mounted Mission protocol
```

## Mission behavior

Terminal Lane 2/3 outcomes cause targeted audits; an integrated Lane 1 deliverable causes a global
audit. Deadlines, repeated progress without an outcome, two consecutive actor failures, external
review requests, objective revisions, and the configured portfolio cadence also produce scoped
audits. Only targets pause. A running target receives a checkpoint interjection and, after the
configured grace, only that session is closed.

Coordinator decisions are strict revision-bound objects: `continue`, `redirect`, or `accept`.
Invalid answers receive two same-session repair attempts; targets otherwise remain paused while a
fresh coordinator retries with bounded backoff. New evidence coalesces into a newer revision and
makes an in-flight older answer stale.

An accepted Lane 2/3 package is re-hashed and queued. At its next natural boundary Lane 1 pauses
research, integrates the package, and then resumes or replaces its prior mission according to the
audit decision. A failed integration can be redirected back to research and remains recorded as a
rejected package.

## Durable evidence and external ingress

Humanize resumable state is authoritative. Full audit revisions, prompt projections, manifests,
reports, artifacts, checkpoints, queues, and source snapshots remain under
`~/.humanize/parallel_flame_chase/<workspace-key>/<run-id>/`. Oversized audit histories are reduced
to deterministic identity-preserving coordinator projections; full packets remain on disk.

`external_events` may point to an adapter-owned version-1 JSONL evidence stream. Records are bounded
and tied to the current `run_id`; they can request review or contribute evidence but cannot carry a
command, decision, or remote action.

The flow coordinates local work only. It contains no release, deployment, submission, messaging,
purchase, or other remote-action executor. Its bundled `parallel-flame-chase-mission` skill defines
the complete mission, audit, interruption, integration, and recovery protocol. Only lifecycle,
lane scheduling, workspace, report, checkpoint, and utility primitives are reused from
`flows/_parallel_flame_chase`. This keeps common isolation behavior aligned without making the
base flow import Mission code.
