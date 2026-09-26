# flame_chase_agent_cleanup

Two coding agents alternate fresh-session turns on a repository task. Every few turns, a
third agent cleans the workspace between them.

## Usage

```sh
hmz exec -f flame_chase_agent_cleanup \
    -a first_chaser=claude/MODEL:EFFORT -a second_chaser=codex/MODEL:EFFORT \
    -a cleaner=claude/MODEL:EFFORT \
    -p work_paths=src -b duration=12h,cost=100 "$(cat TASK.md)"
```

The workspace is the directory `hmz exec` runs in. `work_paths` is required: the paths
below it, comma-separated, where agents may create or revise task work. `-b` is required,
and is the run's only budget: whichever of its limits is reached first stops the run. The
agents must be ones a flow can steer mid-turn (Claude Code, Codex, Kimi Code or pi), since
a turn is asked to wrap up when its clock runs out.

Each cleaning epoch replaces the repository's git history with one commit. The replaced
history is archived in the workspace's `history.git`, never deleted, and files over
`max_tracked_file_mb` (10) are never committed. See
[`_flame_chase_agent_cleanup`](_flame_chase_agent_cleanup/README.md) for every param, what
an epoch does, how to read the archived history, and where the run keeps its data.
