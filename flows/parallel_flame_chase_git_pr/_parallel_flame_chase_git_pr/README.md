# Parallel Flame Chase Git/PR Lite Runtime

This private package contains the lane runtime that Parallel Flame Chase Git/PR Lite builds on.
Files are grouped by responsibility:

```text
_parallel_flame_chase_git_pr/
├── runtime.py          # lifecycle, lane scheduling, Report Share and the control cycle
├── models.py           # plan, lane report and checkpoint schemas
├── prompts.py          # planning, lane and repair prompts
├── leaderboard.py      # the cross-lane candidate leaderboard
├── repository.py       # `pfc-runtime`: the runtime's Git, SQLite and file-tree steps
├── agent_cli.py        # `pfc`: the lanes' evaluation-receipt and PR command
├── storage.py          # `pfc_storage.py`: the SQLite/WAL coordination store
└── pre_receive.py      # the central repository's branch-protection hook
```

`runtime.py` never touches a run's files itself. It keeps its state in the flow's resumable
state, one key per top-level field so that a save writes only what changed, writes the run's
JSON views through the workspace's scratch environment, and runs `repository.py` there as
`shared/bin/pfc-runtime` for every other step: Git, SQLite, report archives (appended, never
rewritten), checkpoints, deliverables, and one poll per control cycle. The last four files are
standalone scripts: the runtime writes them into the run's `shared/bin/` when the run starts or
resumes, and the lanes and the Git hook run them from there.

Before materializing workspaces, the runtime counts regular files and apparent bytes. Oversized
plans always print a warning. `confirm_large_workspace_copies` defaults to `false`, so launches
remain unattended by default; set it to `true` to require the person at the prompt to approve
before any new copy is created.
