# Parallel Flame Chase Git/PR Lite Runtime

This private package contains the lane runtime that Parallel Flame Chase Git/PR Lite builds on.
Files are grouped by responsibility:

```text
_parallel_flame_chase_git_pr/
├── runtime.py          # lifecycle entry and control loop
├── report_share.py     # report-sharing runtime extended by the Git/PR runtime
├── core/               # public flow types, durable models, generic utilities
├── orchestration/      # run creation, resume validation, planning, persistence
├── lanes/              # lane prompts, session handles, scheduling and reports
└── persistence/        # workspaces, locks, artifacts, reports, checkpoints, leaderboard
```

Before materializing workspaces, the runtime counts regular files and apparent bytes. Oversized
plans always print a warning. `confirm_large_workspace_copies` defaults to `false`, so launches
remain unattended by default; set it to `true` to require the person at the prompt to approve
before any new copy is created. Git/PR Lite reports its planning, lane, and integration working
trees.

`runtime.py` composes the scheduler layers; leaf modules under `core` and `persistence` do not
import the public flow. PR state, evaluation receipts, branch protection, and integration policy
live in the modules next to this package.
