# Parallel Flame Chase Runtime

This private package contains the runtime of the Parallel Flame Chase flow. Files are grouped by
responsibility:

```text
_parallel_flame_chase/
├── runtime.py          # lifecycle entry and control loop
├── core/               # flow roles and params, durable models, generic utilities
├── orchestration/      # run creation, resume validation, planning, persistence
├── lanes/              # lane prompts, turn sessions, scheduling and reports
└── persistence/        # run layout, source lock, reports, checkpoints, leaderboard, probe
```

The flow's entry point declares two hidden subflows beside the flow itself: `plan`, the
coordinator's planning turn, and `lane_turn`, one actor turn. Each opens its own fresh session,
which closes when the subflow returns. The scheduler runs one `lane_turn` task per lane at a time,
concurrently, and collects them on each control pass.

Every file operation goes through the workspace environment. The run directory is its scratch
directory, runtime files are read and written through it, and the source lock is a temporary copy
only one holder can take. `persistence/probe.py`, a standard-library script, is run in it with
`exec` for what needs the files where they are: counting the source, laying out the run and
snapshotting the source into it, refusing linked or replaced runtime paths, replacing runtime
files and appending report records staged under `shared/staging` without following links,
checking and hashing artifact packages, and reading checkpoints. The workspace is a `LocalEnv`, so the probe runs on
this machine under humanize's own interpreter.

Before materializing workspaces, the runtime counts regular files and apparent bytes. Oversized
plans always print a warning. `confirm_large_workspace_copies` defaults to `false`, so launches
remain unattended by default; set it to `true` to require the person at the prompt to approve
before any new copy is created. The flow reports its three lane snapshots.

`runtime.py` composes the scheduler layers; leaf modules under `core` and `persistence` do not
import the public flow.
