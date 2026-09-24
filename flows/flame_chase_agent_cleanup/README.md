# flame_chase_agent_cleanup

Two coding agents alternate fresh-session turns on a repository task. Every few turns, a
third agent cleans the workspace between them.

## Usage

```sh
hmz exec -f flame_chase_agent_cleanup \
    -a claude/MODEL:EFFORT -a codex/MODEL:EFFORT -a claude/MODEL:EFFORT \
    -c cleanup.yaml "$(cat TASK.md)"
```

The agents fill `first_chaser`, `second_chaser` and `cleaner` in that order. `cleanup.yaml`
needs at least `work_paths`:

```yaml
work_paths: [src]
budget: {tokens: 10, hours: 12}
```

Each cleaning epoch replaces the repository's git history with one commit. The replaced
history is archived in the workspace's `history.git`, never deleted, and files over
`max_tracked_file_mb` (10) are never committed. See
[`_flame_chase_agent_cleanup`](_flame_chase_agent_cleanup/README.md) for every setting, what an epoch
does, how to read the archived history, and where the run keeps its data.
