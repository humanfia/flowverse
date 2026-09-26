# Parallel Flame Chase

`parallel_flame_chase` is the report-driven, non-audited Parallel Flame Chase. One coordinator
creates an initial three-lane plan and then leaves the run. Each lane alternates two actors in
fresh sessions and coordinates through durable reports.

This ordinary flow intentionally stops at durable peer coordination. The planning coordinator
does not return for audits, interruptions, redirections, or acceptance decisions.

## Topology

Name all seven agents by role:

| Role | Agent |
| --- | --- |
| `coordinator` | initial planning coordinator |
| `lane_1_actor_a`, `lane_1_actor_b` | Lane 1 actors A and B |
| `lane_2_actor_a`, `lane_2_actor_b` | Lane 2 actors A and B |
| `lane_3_actor_a`, `lane_3_actor_b` | Lane 3 actors A and B |

Two roles are filled by the runtime and take no flag: `human` is the person at the prompt, who
is only asked for the optional startup confirmation, and `workspace` is the directory the flow
is run in. A budget is required.

```console
hmz exec -f ./flows/parallel_flame_chase \
  -a coordinator=codex/gpt-5.6-sol:max \
  -a lane_1_actor_a=claude/claude-opus-5:max,lane_1_actor_b=codex/gpt-5.6-sol:max \
  -a lane_2_actor_a=claude/claude-opus-5:max,lane_2_actor_b=codex/gpt-5.6-sol:max \
  -a lane_3_actor_a=claude/claude-opus-5:max,lane_3_actor_b=codex/gpt-5.6-sol:max \
  -b duration=12h,cost=500 \
  "$(cat TASK.md)"
```

Every agent runs without `/goal`, with the bundled `parallel-flame-chase` skill, and may write
anywhere its user can and use the web: lanes publish into the run directory beside their
workspace and run task-provided builds, tests, and evaluators.

Lane 1 alone edits the original working directory. Lanes 2 and 3 receive independent snapshots
and publish reconstructable files into runtime-owned artifact roots. Reports are redelivered until
the receiving lane completes a valid turn and acknowledges them. The coordinator does not return
after planning, and the runtime never pauses a healthy lane for portfolio review. A lane whose
two consecutive turns fail is blocked until the objective is replanned.

All three lanes may use task-provided local evaluators and attach an accepted candidate to a
`deliverable_ready` report. The runtime binds every candidate to hashed artifacts and maintains a
single-writer cross-lane board at `shared/leaderboard.json`; its primary best is included in every
fresh lane prompt. Candidate submission does not grant source-integration or remote-action rights.

## Params

Set with `-p key=value`:

| Param | Default | Meaning |
| --- | --- | --- |
| `rest_seconds` | `1.0` | Seconds the single-writer scheduler rests between control passes (0.05–60). |
| `resume_mode` | `auto` | `auto` resumes compatible state; `fresh` deliberately starts another run. |
| `confirm_large_workspace_copies` | `false` | Ask before materializing an oversized workspace. |
| `workspace_file_warning_threshold` | `5000` | Regular files that mark the source as oversized. |
| `workspace_copy_warning_threshold_bytes` | `1073741824` | Estimated bytes across new snapshots that mark it oversized. |

## Resume and safety

The flow is resumable: `hmz exec --resume` picks up the newest run in the workspace. The same
substantive task resumes compatible state and preserves A/B alternation, reports, snapshots, and
lane-local failure state. A bare `continue` reads `TASK.md` when present; if the objective changed,
the flow replans against a fresh source snapshot in the same run. Without `--resume`, or with
`-p resume_mode=fresh`, it starts another run.

Before a fresh run, and before a changed objective is snapshotted on resume, the source is
measured. More than `workspace_file_warning_threshold` regular files, or a projected copy size
above `workspace_copy_warning_threshold_bytes`, prints a warning. By default the flow continues
without asking; with `-p confirm_large_workspace_copies=true` it asks the person at the prompt to
approve before creating the planning, Lane 2, and Lane 3 snapshots, and stops without copying
when nobody is there to answer, as under `hmz exec`.

A per-source advisory lock permits only one Lane 1 owner, across processes. Runtime control paths
reject links and replacements: runtime files are replaced by rename and report logs are appended
without following links, so a link planted in their place is never written through. A resumed
run whose snapshots or reports are gone refuses to recreate them, and Lane 2 and Lane 3 remain
confined to snapshots rather than the source tree.

Durable runtime data lives in a scratch directory of the workspace environment, named
`parallel_flame_chase-<run-id>`. Locally it is under humanize's home:

```text
~/.humanize/envs/<workspace>-<digest>/scratch/parallel_flame_chase-<run-id>-<digest>/
├── objective.md
├── private/lane-2/  private/lane-3/        # the private snapshots
└── shared/
    ├── planning-workspace/  planning-revisions/
    ├── reports/  artifacts/  checkpoints/  staging/
    └── leaderboard.json  state.json  manifest.json  workspace-map.json
```

A run that keeps a journal, as every `hmz exec` run of a resumable flow does, keeps this
directory for `--resume`. A run that keeps none, such as this flow called from a flow that is not
resumable, has it removed when it ends, as the environment does for every flow it cannot resume;
Lane 1's work in the source stays.

The flow coordinates local work only. It contains no release, deployment, submission, messaging,
purchase, or other remote-action executor.

The bundled `parallel-flame-chase` skill defines the actor, report, artifact, checkpoint, and
resume protocol. Small shared units for lifecycle, lane scheduling, workspaces, reports, events,
checkpoints, and utilities live in the private package
[`_parallel_flame_chase`](_parallel_flame_chase/README.md) inside this flow.
