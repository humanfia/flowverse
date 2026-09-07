# Parallel Flame Chase Git/PR

`parallel_flame_chase_git_pr` is the fixed Git-PR-only configuration that performed best in the
12-hour Git PR Lite experiment. It retains Report Share and deterministic receipt-fast-path PR
integration while disabling the mechanisms that were not part of that result:

| Setting | Fixed value |
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

The seven ordered agents are `orchestrateor`, followed by A/B partners for lanes 1, 2, and 3. The
orchestrateor plans once; each lane runs one actor at a time and alternates its A/B partners across
fresh turns, so the six lane actors represent three concurrent research lanes. There is no
reviewer agent slot. The person at the prompt is injected automatically and does not require an
eighth `-a`. With Git enabled, every lane owns one clone and has equal PR rights. The
runtime owns a bare central repository and a separate integration clone. A lane may keep many
drafts but only one ready/reviewing PR. Ready heads are frozen, while a newer queued candidate from
the same lane supersedes its older one. The runtime validates the exact official evaluator command
and immutable receipt artifacts, chooses the lowest-cycle candidate, and publishes its exact
tested tree only when it improves main. The server hook accepts main only when the commit has two
exact parents, an allowed-path diff, the tested head's exact tree, a successful head-bound receipt,
and the matching `PFC-PR` trailer. No model review or staging re-evaluation runs on this fast path.
When updating an older eight-agent launch command, remove its second `knowledge_reviewer` argument;
the remaining argument order is orchestrateor, lane 1 A/B, lane 2 A/B, then lane 3 A/B. Existing
durable run state remains compatible because the removed slot produced no runtime state.

`pfc evaluate -- <command>` records rather than interprets the evaluator. It requires a clean
commit/tree before and after the command, stores stdout/stderr outside Git, and preserves an
immutable receipt. Code and light files belong in Git; large artifacts can use `pfc artifact put`.

SQLite/WAL is live truth, JSONL is the audit stream, and JSON/Markdown are disposable views.
Global Knowledge and Experiment Memory are disabled: cross-lane information comes from Report
Share plus deterministic merge/rejection system reports, and no reviewer agent is used.

```console
hmz exec -f ./flows/parallel_flame_chase_git_pr \
  -a codex/gpt-5.6-sol:max \
  -a codex/gpt-5.6-sol:max -a claude/claude-opus-5:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -c ./flows/parallel_flame_chase_git_pr/examples/git-pr.yaml \
  "$(cat TASK.md)"
```

Runs are resumable. Runtime state retains the central refs, receipts, artifacts, report archives,
and official ledger. The original source is assumed not to change outside the flow while it holds
the source lock.

Before creating a fresh shadow repository, the source is measured. More than
`workspace_file_warning_threshold` regular files, or a projected materialization size above
`workspace_copy_warning_threshold_bytes`, prints a warning covering the planning tree, three lane
trees, integration tree, and Git object storage. The default continues without asking; set
`confirm_large_workspace_copies: true` to require approval before any of those copies are made.
