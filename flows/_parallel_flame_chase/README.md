# Shared Parallel Flame Chase Runtime

This hidden package contains implementation reused by the public Parallel Flame Chase flows. Files
are grouped by responsibility:

```text
_parallel_flame_chase/
├── runtime.py          # lifecycle entry and control loop
├── core/               # public flow types, durable models, generic utilities
├── orchestration/      # run creation, resume validation, planning, persistence
├── lanes/              # lane prompts, session handles, scheduling and reports
└── persistence/        # workspaces, locks, artifacts, reports, checkpoints, leaderboard
```

Before materializing workspaces, the runtime counts regular files and apparent bytes. Oversized
plans always print a warning. `confirm_large_workspace_copies` defaults to `false`, so launches
remain unattended by default; set it to `true` to require the person at the prompt to approve
before any new copy is created. The copy plan is mode-specific: the base flow reports its three
snapshots, while Git/PR Lite reports its planning, lane, and integration working trees.

`runtime.py` composes the scheduler layers; leaf modules under `core` and `persistence` do not
import a public flow. PR state, evaluation receipts, branch protection, and integration policy
live under `flows/parallel_flame_chase_git_pr`; importing the base flow does not load that package.
