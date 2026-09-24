from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hmz.coganchor.agents import HumanAgent, driver
from hmz.flows import NEVER_DONE, carries, checked, configures, load, proved, wanted

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "aot"
sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import aot

if TYPE_CHECKING:
    from hmz.coganchor.agents import AgentBase

WRITER = os.environ.get("AOT_WRITER", "")
CRITIC = os.environ.get("AOT_CRITIC", "") or WRITER

pytestmark = pytest.mark.skipif(
    not WRITER,
    reason="a compile is minutes of a real agent; AOT_WRITER=cli/model:effort runs these",
)


def agent_of(spec: str) -> AgentBase:
    cli, _, rest = spec.partition("/")
    model, _, effort = rest.rpartition(":")
    agent, config = driver(cli)
    return agent(config(model=model, effort=effort))


def compiled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, task: str) -> Path:
    monkeypatch.chdir(tmp_path)
    agents = aot.Compiling(
        writer=agent_of(WRITER), critic=agent_of(CRITIC), human=HumanAgent()
    )
    carries(str(FLOW), list(agents))
    aot.run(agents, task)
    landed = tmp_path / ".humanize" / "flows"
    flows = [one for one in landed.iterdir() if (one / "__init__.py").is_file()]
    assert len(flows) == 1, f"expected one compiled flow, found {flows}"
    return flows[0]


def equivalent(
    at: Path, *, drives_count: int, person: bool, takes_config: bool
) -> None:
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


def _all_places(entry: Path):
    from hmz.flows.driving import declares

    return declares(entry)[1]


def test_flame_chase_from_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = compiled(
        tmp_path,
        monkeypatch,
        "two agents take turns on the same task, one after the other, for a bounded "
        "number of rounds under the run's allowance",
    )
    equivalent(at, drives_count=2, person=False, takes_config=True)
    source = (at / "__init__.py").read_text()
    assert "range(" in source


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
    assert "range(" in source


def test_rlcr_from_its_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = compiled(tmp_path, monkeypatch, RLCR)
    equivalent(at, drives_count=2, person=False, takes_config=True)
    source = (at / "__init__.py").read_text()
    assert "schema=" in source
    assert "range(" in source


RLCR = (
    "a builder works through a task under review, in one session that remembers: each "
    "round the builder builds, then a reviewer that shares no context with the builder "
    "reads the repository fresh and answers two things in one shape -- whether there is "
    "nothing left to do, and the findings to hand the builder next, written as its next "
    "prompt with the important ones marked [P0] to [P9]; the findings go to the builder "
    "word for word; the loop ends when the reviewer says there is nothing left, and a "
    "bounded round cap backstops a reviewer that never does"
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
