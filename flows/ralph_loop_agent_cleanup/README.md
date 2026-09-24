# ralph_loop_agent_cleanup

A fresh-session Ralph loop on a repository task. Every few turns, a second agent cleans
the workspace.

## Usage

```sh
hmz exec -f ralph_loop_agent_cleanup \
    -a claude/MODEL:EFFORT -a claude/MODEL:EFFORT \
    -c cleanup.yaml "$(cat TASK.md)"
```

The agents fill `agent` and `cleaner` in that order. `cleanup.yaml` needs at least
`work_paths`:

```yaml
work_paths: [src]
budget: {tokens: 10, hours: 12}
```

Each cleaning epoch replaces the repository's git history with one commit. The replaced
history is archived in the workspace's `history.git`, never deleted, and files over
`max_tracked_file_mb` (10) are never committed. See
[`_ralph_loop_agent_cleanup`](_ralph_loop_agent_cleanup/README.md) for every setting, what an epoch
does, how to read the archived history, and where the run keeps its data.
