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
history is archived under the run root, never deleted. See
[`_workspace_cleanup`](../_workspace_cleanup/README.md) for every setting, what an epoch
does, how the archives stitch back into one history, and where the run keeps its data.
