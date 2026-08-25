# Parallel Flame Chase

`parallel_flame_chase` is the report-driven, non-audited Parallel Flame Chase. One coordinator
creates an initial three-lane plan and then leaves the run. Each lane alternates two actors in
fresh sessions and coordinates through durable reports.

This ordinary flow intentionally stops at durable peer coordination. The planning coordinator
does not return for audits, interruptions, redirections, or acceptance decisions.

## Topology

Pass exactly seven agents in this order:

1. initial planning coordinator
2. Lane 1 actor A
3. Lane 1 actor B
4. Lane 2 actor A
5. Lane 2 actor B
6. Lane 3 actor A
7. Lane 3 actor B

```console
hmz exec -f ./flows/parallel_flame_chase \
  -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -c ./flows/parallel_flame_chase/examples/base.yaml \
  "$(cat TASK.md)"
```

Lane 1 alone edits the original working directory. Lanes 2 and 3 receive independent snapshots
and publish reconstructable files into runtime-owned artifact roots. Reports are redelivered until
the receiving lane completes a valid turn and acknowledges them. The coordinator does not return
after planning, and the runtime never pauses a healthy lane for portfolio review.

## Resume and safety

The flow is resumable. The same substantive task resumes compatible state and preserves A/B
alternation, reports, snapshots, and lane-local failure state. A bare `continue` reads `TASK.md`
when present; if the objective changed, the base flow replans against a fresh source snapshot.
Set `resume_mode: fresh` to deliberately start another run.

A per-source advisory lock permits only one Lane 1 owner. Runtime control paths reject links and
replacements, while Lane 2 and Lane 3 remain confined to snapshots rather than the source tree.

Durable runtime data lives under:

```text
~/.humanize/parallel_flame_chase/<workspace-key>/<run-id>/
```

The flow coordinates local work only. It contains no release, deployment, submission, messaging,
purchase, or other remote-action executor.

The bundled `parallel-flame-chase` skill defines the actor, report, artifact, checkpoint, and
resume protocol. Small implementation units for lifecycle, lane scheduling, workspaces, reports,
events, checkpoints, and utilities live in the hidden sibling module
`flows/_parallel_flame_chase`, keeping the public entry point small without making the runtime
monolithic.
