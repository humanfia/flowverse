# Agent workspace cleanup

Implementation of the flows that start every coding turn in a fresh session and
periodically hand the repository to a cleaner agent. It is not a flow of its own; each
flow below keeps its own identical copy, so a change here belongs in the other copy too.

| Flow | Coding agents | Cleaner |
| --- | --- | --- |
| [`flame_chase_agent_cleanup`](../../flame_chase_agent_cleanup/README.md) | `first_chaser` and `second_chaser`, alternating | `cleaner` |
| [`ralph_loop_agent_cleanup`](../../ralph_loop_agent_cleanup/README.md) | `agent` | `cleaner` |

Both flows are resumable (`hmz exec --resume`). Every agent role asks for
`SteeringAgentMixin`, so it takes a harness that can be told something mid-turn: Claude
Code, Codex, Kimi Code or pi. The `human` role is whoever started the run, and the
`workspace` environment is the directory the run started in; neither is given with `-a` or
`-e`.

## Params

Both flows take the same params, each as `-p key=value`. `work_paths` is required: safe,
non-overlapping paths relative to the repository, where agents may create or revise task
work, comma-separated (`-p work_paths=src,include`) or as a JSON list.

```text
work_paths                            # required, e.g. src or src,include
cleanup_turns=3                       # counted coding turns since the last epoch; 0 never cleans
next_lines=10                         # the most lines NEXT.md may hold
comment_lines=30                      # comment-line cap under work_paths, printed only
repairs=2                             # over-measures handed back to the cleaner
check_command=""                      # correctness check after cleaning; empty skips it
session_timeout_minutes=240           # per turn, then a wrap-up request
stop_grace_minutes=10                 # after the request, the turn is cut off
idle_timeout_minutes=20               # without token progress, a reminder
max_tracked_file_mb=10                # larger files are never committed
confirm_large_workspace_copies=true   # ask before cleaning a large workspace
```

The flows set no budget of their own. `-b duration=12h,cost=100` (or `output_tokens=`) is
the run's, and whichever limit is reached first stops the run; an epoch it stops is put
back first.

## Turns

Every coding turn is a fresh session, and runs as a hidden subflow (`turn`), so the session
and whatever harness process serves it end with the turn. A cleaning epoch is one subflow
(`epoch`) in the same way, its cleaner keeping one session through its repairs.

A coding turn counts when it answers, and also when the clock or the run's end cut it
short: its edits are on disk, so it is not taken again. A turn that answered nothing, or
whose harness failed short of an unrecoverable failure, is taken again on the same seat,
and three of those in a row end the run. A turn a budget refused never started, and does
not count. Cleaner turns never count.

After `session_timeout_minutes` the turn is steered to wrap up. `stop_grace_minutes` later,
humanize cuts it off through the turn's own budget, which is hard (`graceful=False`); where
the run's own budget ends sooner and gracefully, the flow cuts the turn itself shortly
after. An idle reminder is steered in once per stretch of `idle_timeout_minutes` without
the session's output tokens or cost moving; it never ends a turn. `0` disables either limit.

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

An epoch interrupted for any reason (stopped, out of budget, cancelled, or failed) restores
the tree before the run ends.

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
listed tree plus `.git`. Past 5,000 files or 1 GiB it prints a warning and asks the
`human` whether to start. If nobody answers, as under `hmz exec`, where nobody is there to,
the run does not start. Add `.gitignore` rules, or pass
`-p confirm_large_workspace_copies=false` to only warn.

## Storage

Everything the flows keep lives under Humanize's home, outside the repository, so it
outlives every run and is never taken for a stray:

```text
$HUMANIZE_HOME/<flow>/<workspace-key>/   # ~/.humanize/... unless HUMANIZE_HOME is set
├── history.git                  # every run's archived history, stored once
└── <run-id>/
    ├── manifest.txt             # the task's own files, recorded at the first start
    ├── revert/                  # only while an epoch is in flight
    └── checks/epoch-NNN.log     # the last 1 MiB of each check
```

A run root holds only a manifest and check logs once its epochs are done. Resuming reuses
it; a fresh run gets a new one. Each epoch packs what it added to `history.git`, and
`git gc --auto` joins the packs as they gather.

The flows do their tree and git work through the workspace environment's shell: `bash`,
`git` and the usual POSIX tools, in the workspace, never on the event loop.

## Layout

```text
_flame_chase_agent_cleanup/
├── roles.py     # the role types: Worker, Workspace
├── config.py    # Config, the params, and work-path validation
├── storage.py   # the managed run root
├── tree.py      # listing, manifest, measures, revert point, git, check
├── guard.py     # wrap-up steer, idle reminder, per-turn cut-off
├── cleaning.py  # prompts and one cleaning epoch
└── loop.py      # start, large-workspace question, cadence, the turn and epoch subflows
```
