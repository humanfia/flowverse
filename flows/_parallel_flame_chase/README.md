# Shared Parallel Flame Chase Runtime

This hidden package contains the ordinary flow implementation, grouped by responsibility:

```text
_parallel_flame_chase/
├── runtime.py          # lifecycle entry and control loop
├── core/               # public flow types, durable models, generic utilities
├── orchestration/      # run creation, resume validation, planning, persistence
├── lanes/              # lane prompts, session handles, scheduling and reports
└── persistence/        # workspaces, locks, artifacts, JSONL events and checkpoints
```

`runtime.py` composes the scheduler layers; leaf modules under `core` and `persistence` do not
import the public flow. Mission decisions, coordinator audits, external ingress, and remote-action
execution are outside this package.
