# Agent workspace cleanup flows

These flows start each coding turn in a fresh session and periodically hand the
repository to a cleaner agent. Each normally returned, non-empty coding session
counts as one turn. Failed, empty, or forcibly closed sessions retry the same seat
without advancing the cleanup cadence; their token cost still counts toward the
budget. Cleaner sessions and repairs do not advance the coding-turn count.

| Flow | Coding agents | Cleanup policy |
| --- | --- | --- |
| `flame_chase_agent_cleanup` | Two, alternating | A third agent cleans the tree; deterministic limits, repairs, an optional check, and rollback validate the result. |
| `ralph_loop_agent_cleanup` | One | A second agent performs cleanup using the same limits, repairs, and optional check. |

Both flows share these defaults. `work_paths` is required and must contain safe,
non-overlapping paths relative to the working repository:

```yaml
work_paths: [src]
cleanup_turns: 3
session_timeout_minutes: 240
idle_timeout_minutes: 10
stop_grace_minutes: 10
```

After four hours, the session receives a wrap-up request. If it is still running
after the ten-minute grace period, the watchdog closes it and waits for its call
to stop before retrying or cleaning the repository. A session that cannot stop
after close raises an error instead of allowing concurrent cleanup. Cleaner
repairs share the original session deadline and grace period.

After ten minutes without token usage increasing, the watchdog sends a status
reminder, including when an agent stalls after a tool call. It sends one reminder
per idle stretch and resets after token progress. Idle reminders alone do not
close a session; the wall-clock limit provides the eventual forced close.

Set `cleanup_turns` to `0` to disable cleanup. Set either timeout to `0` to disable
that limit. A zero grace period closes the session immediately after the wrap-up
request. Backends that do not support mid-turn interjection still receive the
forced close at the wall-clock deadline plus grace.

Task manifests and cleanup revert points live under Humanize's managed home,
outside the cleaned repository: `$HUMANIZE_HOME/<flow-name>/<workspace-key>/<run-id>/`.
Interrupted runs retain their recovery data; normal budget completion removes it.
