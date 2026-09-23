"""Two agents flame-chase a repository task while a cleaner distills the workspace between them.

    hmz exec -f flame_chase_agent_cleanup -a claude -a codex -a claude \\
        -c cleanup.yaml 'improve the project'

Two coding agents take turns on the task, each turn a fresh session opened on the
working directory and handed the task verbatim, so every turn reads the repository
rather than any history. A turn that answered counts, and so does one the clock ended;
a turn that answered nothing is taken again by the same agent, and three of those in a
row end the run.

Every cleanup_turns counted turns (default 3) a cleaning epoch runs between turns. The
tree git would add -- `.gitignore` honoured -- and .git are saved aside as a revert
point under the Humanize-managed run root below
``$HUMANIZE_HOME/flame_chase_agent_cleanup/``, and a fresh cleaner session shrinks the
configured work_paths to their essence and distills NEXT.md. The flow then measures what
survived: entries not in the manifest of the task's own files recorded at the first
start, outside the work paths, count as strays; NEXT.md is held to next_lines; comment
lines under the work paths to comment_lines. What is over goes back to the same session
up to repairs times before the flow deletes strays and truncates NEXT.md itself (a
comment overage is only printed). A configured check_command then runs for at most an
hour; a failure restores the tree from the revert point. Last, the repository's history
is replaced by one commit of the cleaned tree. The history it replaces -- with a last
commit of the tree the coding turns left -- goes into one ``history.git`` shared by every
run in the workspace, chained to the commit that replaced it, so one `git log` there
reads the whole run. Files over max_tracked_file_mb and nested repositories are never
committed, and the new repository's pre-commit hook refuses large files. An epoch
interrupted for any reason puts the tree back before the run ends.

What ends the run is its allowance, which is humanize's: hours, millions of output
tokens and dollars, across both chasers and the cleaner. This flow declares ten million
output tokens by default; `budget:` in the file passed with -c says otherwise.
Resumable state keeps the counted turns and epochs, so running it again carries on.
Every turn also has a wall-clock limit that asks the agent to wrap up and cuts the turn
off after a grace period, and an idle reminder that never ends a turn.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from _workspace_cleanup import Config, drive, required
from hmz.flows import Agent, Allowance, Person, flow

FLOW_NAME = "flame_chase_agent_cleanup"


class Agents(NamedTuple):
    """Two chasers taking turns, a cleaner arriving fresh, and whoever runs it."""

    first_chaser: Agent
    second_chaser: Agent
    cleaner: Agent
    human: Person


@flow(budget=Allowance(tokens=10.0), resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    drive(
        FLOW_NAME,
        (agents.first_chaser, agents.second_chaser),
        agents.cleaner,
        agents.human,
        task,
        required(config, FLOW_NAME),
        state if state is not None else {},
    )
