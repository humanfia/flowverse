# Shared Parallel Flame Chase Runtime

This hidden package contains only implementation reused by the two public flows. Files are grouped
by responsibility:

```text
_parallel_flame_chase/
├── runtime.py          # lifecycle entry and control loop
├── core/               # public flow types, durable models, generic utilities
├── orchestration/      # run creation, resume validation, planning, persistence
├── lanes/              # lane prompts, session handles, scheduling and reports
└── persistence/        # workspaces, locks, artifacts, reports, checkpoints, leaderboard
```

`runtime.py` composes the scheduler layers; leaf modules under `core` and `persistence` do not
import a public flow. Mission decisions, coordination state, external ingress, audit scheduling,
and Mission runtime extensions all live under `flows/parallel_flame_chase_mission`; importing the
base flow does not load that package.
