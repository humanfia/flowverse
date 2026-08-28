"""Compile the official flows from their descriptions, with real agents -- when asked.

Off by default: a compile is minutes of a real coding agent's turns, and CI has no agent.
Set `AOT_WRITER` -- and, to differ, `AOT_CRITIC` -- to `cli/model:effort` to run the golden
compiles, and `AOT_SMOKE=1` besides to also run the compiled review loop once on a toy
repository with the same agents.

What is asserted is structural equivalence with the flow each description describes, never
text: the compiled flow loads, drives as many agents as the description says, can be set up,
reads clean under the checker, and -- the point of the whole compiler -- ends under the
reviewer that never says done.
"""

# ruff: noqa: D103, PLR2004, S101

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hmz.agents import HumanAgent, driver
from hmz.flows import NEVER_DONE, carries, checked, configures, load, proved, wanted

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "aot"
sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import aot  # noqa: E402

if TYPE_CHECKING:
    from hmz.agents import AgentBase

WRITER = os.environ.get("AOT_WRITER", "")
CRITIC = os.environ.get("AOT_CRITIC", "") or WRITER

pytestmark = pytest.mark.skipif(
    not WRITER,
    reason="a compile is minutes of a real agent; AOT_WRITER=cli/model:effort runs these",
)


def agent_of(spec: str) -> AgentBase:
    """One real agent off `cli/model:effort`, built the way `-a` builds one."""
    cli, _, rest = spec.partition("/")
    model, _, effort = rest.rpartition(":")
    agent, config = driver(cli)
    return agent(config(model=model, effort=effort))


def compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str) -> Path:
    """One real compile in a temporary project, answering with where the flow landed."""
    monkeypatch.chdir(tmp_path)
    agents = aot.Compiling(
        writer=agent_of(WRITER), critic=agent_of(CRITIC), human=HumanAgent()
    )
    # What a run of the flow does before its first turn: the flow's own skills -- the
    # writing-flows contract -- mounted onto every session these agents open.
    carries(str(FLOW), list(agents))
    aot.run(agents, task)
    landed = tmp_path / ".humanize" / "flows"
    flows = [one for one in landed.iterdir() if (one / "__init__.py").is_file()]
    assert len(flows) == 1, f"expected one compiled flow, found {flows}"
    return flows[0]


def equivalent(at: Path, *, drives_count: int, person: bool, takes_config: bool) -> None:
    """The structural bar every compiled flow is held to."""
    entry = at / "__init__.py"
    places = wanted(entry)
    assert len(places) == drives_count, places
    chairs = [one for one in _all_places(entry) if one.person]
    assert bool(chairs) == person
    if takes_config:
        assert configures(entry) is not None
    found = checked(at)
    assert not [one for one in found if one.severity == "error"], found
    assert "unbounded-loop" not in {one.code for one in found}, found
    proof = proved(at, scenarios=(NEVER_DONE,))
    assert proof.findings == (), proof.findings
    assert proof.outcomes[0].finished, proof.outcomes


def _all_places(entry: Path):  # noqa: ANN202
    from hmz.flows.driving import declares

    return declares(entry)[1]


def test_flame_chase_from_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = compiled(
        tmp_path,
        monkeypatch,
        "two agents take turns on the same task, one after the other, until a budget "
        "of output tokens is spent",
    )
    equivalent(at, drives_count=2, person=False, takes_config=True)
    # The golden's shape: both agents take turns, and the budget is what ends it.
    source = (at / "__init__.py").read_text()
    assert "spent()" in source


def test_gen_idea_from_its_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = compiled(
        tmp_path,
        monkeypatch,
        "open a loose idea into a repository-grounded design draft: one agent reads "
        "the repository, expands the idea into a draft with goals, constraints and "
        "open questions, and writes it to a markdown file whose path it prints; one "
        "pass, no loop",
    )
    equivalent(at, drives_count=1, person=False, takes_config=False)


def test_gen_plan_from_its_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = compiled(
        tmp_path,
        monkeypatch,
        "turn a design draft into an implementation plan two sides converge on: a "
        "planner writes and revises the plan file, and an analyst who shares no "
        "context with the planner reviews it fresh each round and answers whether it "
        "is settled; the loop ends when the analyst says settled, and a cap on the "
        "rounds backstops an analyst that never does",
    )
    equivalent(at, drives_count=2, person=False, takes_config=True)
    source = (at / "__init__.py").read_text()
    assert "range(" in source or "spent()" in source  # the backstop is real


def test_rlcr_from_its_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = compiled(tmp_path, monkeypatch, RLCR)
    equivalent(at, drives_count=2, person=False, takes_config=True)
    source = (at / "__init__.py").read_text()
    assert "schema=" in source  # the review is read off a shape, not a marker
    assert "spent()" in source or "range(" in source


#: The rlcr loop, described the way somebody would describe it.
RLCR = (
    "a builder works through a task under review, in one session that remembers: each "
    "round the builder builds, then a reviewer that shares no context with the builder "
    "reads the repository fresh and answers two things in one shape -- whether there is "
    "nothing left to do, and the findings to hand the builder next, written as its next "
    "prompt with the important ones marked [P0] to [P9]; the findings go to the builder "
    "word for word; the loop ends when the reviewer says there is nothing left, and a "
    "budget of output tokens backstops a reviewer that never does"
)


@pytest.mark.skipif(
    os.environ.get("AOT_SMOKE", "") != "1",
    reason="the smoke drives the compiled loop with real agents; AOT_SMOKE=1 runs it",
)
def test_the_compiled_rlcr_runs_once_on_a_toy_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = compiled(tmp_path, monkeypatch, RLCR)
    equivalent(at, drives_count=2, person=False, takes_config=True)
    # A toy repository for the loop to work in: the run is the flow's own directory's.
    workshop = tmp_path / "workshop"
    workshop.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workshop, check=True)
    (workshop / "README.md").write_text("# workshop\n\nA toy repository.\n")
    monkeypatch.chdir(workshop)
    run = load(str(at))
    run(
        (agent_of(WRITER), agent_of(CRITIC)),
        "create a file called hello.txt containing exactly the line `hello`, and "
        "nothing else; the task is done when that file exists with that content",
    )
    assert (workshop / "hello.txt").read_text().strip() == "hello"
