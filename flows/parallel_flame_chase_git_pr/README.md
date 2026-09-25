# Parallel Flame Chase Git/PR

`parallel_flame_chase_git_pr` is the fixed Git-PR-only configuration that performed best in the
12-hour Git PR Lite experiment. It retains Report Share and deterministic receipt-fast-path PR
integration while disabling the mechanisms that were not part of that result:

| Param | Fixed value |
| --- | --- |
| `git_pr_enabled` | `true` |
| `global_knowledge_enabled` | `false` |
| `experiment_memory_enabled` | `false` |
| `token_efficient_enabled` | `false` |
| `main_update_monitor_enabled` | `false` |

These values are literals rather than optional defaults, so a launch cannot accidentally turn the
canonical workflow into a different treatment. This package exposes only the measured Lite
configuration; it does not install the token-efficient, main-monitor, adaptive-gate, or Ralph
experiment variants.

The agent roles are `orchestrator` and the A/B partners `lane_1_actor_a` through
`lane_3_actor_b`. The orchestrator plans once; each lane runs one actor at a time and alternates
its A/B partners across fresh sessions, so the six lane actors represent three concurrent
research lanes. There is no reviewer role. The person at the prompt fills the `human` role and
the directory the run starts in fills the `workspace` environment, so neither takes an `-a` or
`-e`. Every lane owns one clone and has equal PR rights. The runtime owns a bare central
repository and a separate integration clone. A lane may keep many drafts but only one
ready/reviewing PR. Ready heads are frozen, while a newer queued candidate from the same lane
supersedes its older one. The runtime validates the exact official evaluator command and
immutable receipt artifacts, chooses the lowest-cycle candidate, and publishes its exact tested
tree only when it improves main. The server hook accepts main only when the commit has two exact
parents, an allowed-path diff, the tested head's exact tree, a successful head-bound receipt, and
the matching `PFC-PR` trailer. No model review or staging re-evaluation runs on this fast path.

`pfc evaluate -- <command>` records rather than interprets the evaluator. It requires a clean
commit/tree before and after the command, stores stdout/stderr outside Git, and preserves an
immutable receipt. Code and light files belong in Git; large artifacts can use `pfc artifact put`.

SQLite/WAL is live truth, JSONL is the audit stream, and JSON/Markdown are disposable views.
Global Knowledge and Experiment Memory are disabled: cross-lane information comes from Report
Share plus deterministic merge/rejection system reports, and no reviewer agent is used.

```console
hmz exec -f official/parallel_flame_chase_git_pr \
  -a orchestrator=codex/gpt-5.6-sol:max \
  -a lane_1_actor_a=codex/gpt-5.6-sol:max,lane_1_actor_b=claude/claude-opus-5:max \
  -a lane_2_actor_a=claude/claude-opus-5:max,lane_2_actor_b=codex/gpt-5.6-sol:max \
  -a lane_3_actor_a=claude/claude-opus-5:max,lane_3_actor_b=codex/gpt-5.6-sol:max \
  -b duration=12h \
  "$(cat TASK.md)"
```

`hmz exec` requires a budget (`-b`). Params keep their defaults unless set with `-p`, as in
[examples/git-pr.sh](examples/git-pr.sh), which spells every one of them out.

Runs are resumable with `hmz exec --resume`. A run keeps the central refs, receipts, artifacts,
report archives, and official ledger in a scratch directory of the workspace under humanize's
home, which a resumable run (one that keeps a journal, as `hmz exec` does) keeps after it ends;
a run without a journal, such as a call from a flow that is not resumable, has it removed when
it ends, and leaves only what it published to the source. The original source is assumed not to
change outside the flow while a run holds it; a second run over the same source, of this flow or
of `parallel_flame_chase`, refuses to start.

Before creating a fresh shadow repository, the source is measured. More than
`workspace_file_warning_threshold` regular files, or a projected materialization size above
`workspace_copy_warning_threshold_bytes`, prints a warning covering the planning tree, three lane
trees, integration tree, and Git object storage. The default continues without asking; with
`-p confirm_large_workspace_copies=true` the person at the prompt must approve before any of
those copies are made, and a run nobody is there to approve (`hmz exec` is always one) stops.
