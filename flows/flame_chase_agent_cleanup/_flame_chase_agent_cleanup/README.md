# Agent workspace cleanup

Implementation of the flows that start every coding turn in a fresh session and
periodically hand the repository to a cleaner agent. It is not a flow of its own; each
flow below keeps its own identical copy, so a change here belongs in the other copy too.

| Flow | Coding agents | Cleaner |
| --- | --- | --- |
| [`flame_chase_agent_cleanup`](../../flame_chase_agent_cleanup/README.md) | Two, alternating | A third agent |
| [`ralph_loop_agent_cleanup`](../../ralph_loop_agent_cleanup/README.md) | One | A second agent |

## Configuration

Both flows take the same settings. `work_paths` is required: safe, non-overlapping paths
relative to the repository, where agents may create or revise task work.

```yaml
work_paths: [src]
cleanup_turns: 3                     # counted coding turns since the last epoch; 0 never cleans
next_lines: 10                       # the most lines NEXT.md may hold
comment_lines: 30                    # comment-line cap under work_paths, printed only
repairs: 2                           # over-measures handed back to the cleaner
check_command: ""                    # correctness check after cleaning; empty skips it
session_timeout_minutes: 240         # per turn, then a wrap-up request
stop_grace_minutes: 10               # after the request, the turn is cut off
idle_timeout_minutes: 20             # without token progress, a reminder
max_tracked_file_mb: 10              # larger files are never committed
confirm_large_workspace_copies: true # ask before cleaning a large workspace
budget:                              # the run's allowance, held by humanize
  tokens: 10                         # millions of output tokens (the flows' default)
  hours: 12
  dollars: 100
```

`budget:` is humanize's run allowance, not a flow setting. Whichever of hours, millions
of output tokens or dollars is reached first stops the run. Both flows declare
`Allowance(tokens=10.0)` by default.

## Turns

A coding turn counts when it answers, and also when the clock ended it: its edits are on
disk, so it is not taken again. A turn that answered nothing is taken again on the same
seat, and three of those in a row end the run. Cleaner turns never count.

After `session_timeout_minutes` the turn is asked to wrap up. `stop_grace_minutes` later,
humanize cuts it off through its per-turn `Budget`, and the turn answers with what it said.
An idle reminder goes out once per stretch of `idle_timeout_minutes` without token
progress; it never ends a turn. `0` disables either limit.

## Cleaning epochs

Every `cleanup_turns` counted turns since the last epoch, between turns:

1. The tree git would add (`.gitignore` honoured) and `.git` are saved aside as a revert
   point.
2. A fresh cleaner session distills the work paths, deletes strays, and writes `NEXT.md`.
   Every `.gitignore` is put back as it was saved, after each of its turns.
3. The flow measures what survived. Entries outside the work paths that were not in the
   repository when the run first started count as strays. Anything over is handed back to
   the cleaner up to `repairs` times, summarized by directory. After that, the flow deletes
   strays and truncates `NEXT.md` itself.
4. `check_command`, if set, runs for at most an hour. The last 1 MiB of its output goes
   to `checks/epoch-NNN.log` in the run root. A failure restores the tree from the revert
   point.
5. The history is archived, then replaced by one commit, `epoch N: distilled tree`. After
   a failed check, the commit says the cleaning was reverted instead.

An epoch interrupted for any reason (stopped, out of allowance, or failed) restores the
tree before the run ends.

The ignore rules hold for the whole epoch. A file `.gitignore` ignored when the tree was
saved aside is never counted as a stray, copied into the revert point, taken away by a
restore, or committed, and a `.gitignore` is never a stray itself. A file the
repository tracks always counts, whatever `.gitignore` says of it.

## What git never tracks

A file over `max_tracked_file_mb`, or a nested repository (a submodule too), stays on disk
but out of git:

- the `distilled tree` commit leaves it out, and the new repository's `.git/info/exclude`
  lists it, so `git status` stays clean;
- the archived tree before cleaning leaves it out, and its commit message names it;
- the new repository's pre-commit hook refuses any file over the limit an agent stages.

A large file outside the work paths that was not in the repository at the start is still
a stray, so the cleaner or the flow deletes it.

## History archive

The history an epoch replaces is never deleted. It goes into one bare repository per
workspace, `history.git`, shared by every run, so what the epochs and runs have in
common is stored once. For run `<run-id>` and epoch `NNN`:

| Ref | What it holds |
| --- | --- |
| `refs/runs/<run-id>/epoch-NNN.refs/*` | every ref the replaced repository had: branches, tags, remotes |
| `refs/runs/<run-id>/epoch-NNN` | `epoch N: the tree before cleaning`, the tree the coding turns left, uncommitted work included, on top of the replaced HEAD |
| `refs/runs/<run-id>/epoch-NNN.distilled` | `epoch N: distilled tree`, the commit that replaced it, grafted onto the tree before cleaning |

Each epoch is chained to the one before it, so one command reads the whole run, from the
repository's original history to the latest epoch:

```sh
git --git-dir=$HUMANIZE_HOME/<flow>/<workspace-key>/history.git \
    log --oneline refs/runs/<run-id>/epoch-NNN.distilled
```

The chain is made with `git replace`, which a clone does not carry by default. Fetch
`refs/replace/*` along with the refs to keep it.

## Large workspaces

Before a fresh run starts, the flow counts what a revert point would copy. That is the
listed tree plus `.git`. Past 5,000 files or 1 GiB it prints a warning and asks the person
at the prompt whether to start. If nobody answers, as under `hmz exec`, the run does not
start. Add `.gitignore` rules, or set `confirm_large_workspace_copies: false` to only warn.

## Storage

Everything the flows keep lives under Humanize's managed home, outside the repository:

```text
$HUMANIZE_HOME/<flow>/<workspace-key>/
├── history.git                  # every run's archived history, stored once
└── <run-id>/
    ├── manifest.txt             # the task's own files, recorded at the first start
    ├── revert/                  # only while an epoch is in flight
    └── checks/epoch-NNN.log     # the last 1 MiB of each check
```

A run root holds only a manifest and check logs once its epochs are done. Resuming reuses
it; a fresh run gets a new one. Each epoch packs what it added to `history.git`, and
`git gc --auto` joins the packs as they gather.

The flows run their tree and git work locally. They are not meant for agents anchored on a
remote machine.

## Layout

```text
_flame_chase_agent_cleanup/
├── config.py    # Config and work-path validation
├── storage.py   # the managed run root
├── tree.py      # listing, manifest, measures, revert point, git, check
├── guard.py     # wrap-up request, idle reminder, per-turn cut-off
├── cleaning.py  # prompts and one cleaning epoch
└── loop.py      # start, large-workspace question, cadence, one coding turn
```
