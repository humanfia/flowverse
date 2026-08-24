# Parallel Flame Chase

`parallel_flame_chase` is a domain-neutral Humanize flow for one planning coordinator and three
parallel lanes, each driven by two alternating actors. It ships two entry points:

| Entry point | Coordinator after initial planning | Coordination |
| --- | --- | --- |
| `parallel_flame_chase` | Does not return | Durable reports; Lane 1 integrates |
| `parallel_flame_chase:mission` | Fresh session per audit | Scoped audits, explicit missions, integration queue |

Lane 1 is the sole writer and integration owner for the original working directory. Lanes 2 and 3
receive copy-on-write snapshots and publish reconstructable files into runtime-owned artifact
roots. All actor turns are fresh sessions and A/B alternation is durable across restarts.

The flow coordinates local work only. It contains no release, deployment, submission, messaging,
purchase, or other remote-action executor.

## Run locally

Pass exactly seven agents in this order:

1. coordinator
2. Lane 1 actor A
3. Lane 1 actor B
4. Lane 2 actor A
5. Lane 2 actor B
6. Lane 3 actor A
7. Lane 3 actor B

For example:

```console
hmz exec -f ./flows/parallel_flame_chase:mission \
  -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max \
  -c ./flows/parallel_flame_chase/examples/mission.yaml \
  "$(cat TASK.md)"
```

Use `continue` to resume the same run. When `TASK.md` changed, mission mode opens a global
objective-revision audit; base mode produces a fresh three-lane plan over a new source snapshot.
A different substantive prompt starts a new run. Set `resume_mode: fresh` to override automatic
resume.

## Mission behavior

Terminal Lane 2/3 outcomes cause targeted audits; an integrated Lane 1 deliverable causes a global
audit. Deadlines, six progress turns without an outcome, two consecutive actor failures, external
review requests, and the default six-hour portfolio cadence also produce appropriately scoped
audits. Only targets pause. A running target receives an interjection, then its own session is
closed after the configured grace. No process scanning or unrelated termination occurs.

Coordinator decisions are strict, revision-bound objects: `continue`, `redirect`, or `accept`.
There is no guessed runtime fallback. Invalid answers receive two same-session repair attempts;
after that, targets remain paused and a fresh coordinator retries with bounded backoff. New evidence
coalesces into a newer revision and makes an in-flight older answer stale.

An accepted Lane 2/3 deliverable is re-hashed and queued. At its next natural boundary Lane 1 pauses
research, integrates the package, and later resumes or replaces the paused mission according to the
audit decision. A failed integration can be redirected back to research and records the package as
rejected.

## Durable state and safety

Humanize resumable state is authoritative. A diagnostic mirror, manifest, append-only lane reports,
artifacts, checkpoints, audit packets, and snapshots live under:

```text
~/.humanize/parallel_flame_chase/<workspace-key>/<run-id>/
```

A per-source advisory lock prevents concurrent Lane 1 owners. Optional `protected_paths` are hashed
per workspace: a change blocks only that lane and is never rolled back, preserving unrelated user
work. Runtime directories reject link/replacement attacks, and report-log identity, size, and
timestamps are bound into authoritative state so out-of-band edits fail closed. Actor checkpoints
are limited, exact-identity recovery evidence; they do not trigger audits.

## External evidence

Mission mode may tail an adapter-owned JSONL file through `external_events`. Every record is bounded
version-1 data tied to the current `run_id`; it cannot carry commands or decisions. Example:

```json
{"version":1,"event_id":"adapter-42","run_id":"20260823T120000Z-abcd123456","at":"2026-08-23T14:10:00Z","kind":"review_requested","scope":"targeted","targets":["lane-2"],"summary":"A domain adapter observed a decision boundary.","evidence":["metric family changed"]}
```

Wrong-run, duplicate, malformed, oversized, truncated, and rotated input is ignored or retained as
bounded ingress-health evidence. External input never edits a workspace or executes an action.

The bundled `parallel-flame-chase` skill contains the complete actor and coordinator protocol in
three routed references.
