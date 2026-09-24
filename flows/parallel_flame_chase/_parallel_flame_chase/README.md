# Parallel Flame Chase Runtime

This private package contains the runtime of the Parallel Flame Chase flow. Files are grouped by
responsibility:

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
before any new copy is created. The flow reports its three lane snapshots.

`runtime.py` composes the scheduler layers; leaf modules under `core` and `persistence` do not
import the public flow.
