from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from hmz.flows import Budget
from hmz.runtime.flowing.engine import load_flow, run_flow
from hmz.runtime.flowing.harnesses import open_agent
from hmz.runtime.flowing.specs import parse_agents

from tests.kit import FLOWS

FLOW = FLOWS / "aot"

WRITER = os.environ.get("AOT_WRITER", "")
CRITIC = os.environ.get("AOT_CRITIC", "") or WRITER
BUDGET = float(os.environ.get("AOT_BUDGET", "20"))

pytestmark = [
    pytest.mark.skipif(
        not WRITER,
        reason="a compile is minutes of a real agent; AOT_WRITER=cli/model:effort runs these",
    ),
    pytest.mark.asyncio,
]


def agent_of(role: str, spec: str) -> Any:
    return open_agent(parse_agents([f"{role}={spec}"])[0])


def local(at: Path) -> Any:
    from hmz.runtime.flowing.environments import local_env

    try:
        return local_env(at)
    except NotImplementedError:
        pytest.skip("this humanize has no local environment driver yet")


async def compiled(tmp_path: Path, task: str) -> Path:
    await run_flow(
        load_flow(str(FLOW), caller_globals={}),
        task,
        agents={"writer": agent_of("writer", WRITER), "critic": agent_of("critic", CRITIC)},
        envs={},
        params={},
        budget=Budget(cost=BUDGET),
        local=local(tmp_path),
    )
    landed = tmp_path / ".humanize" / "flows"
    flows = [one for one in landed.iterdir() if (one / "__init__.py").is_file()]
    assert len(flows) == 1, f"expected one compiled flow, found {flows}"
    return flows[0]


async def equivalent(at: Path, *, drives: int, person: bool, takes_params: bool) -> Any:
    gates = sys.modules["_aot.gates"]
    files = {
        str(one.relative_to(at)): one.read_bytes()
        for one in at.rglob("*")
        if one.is_file() and "__pycache__" not in one.parts
    }
    found = await gates.checked(files, at.name, 60.0)
    assert not found.blocking(strict=False), found.findings
    assert len(found.agents) == drives, found.agents
    assert found.person == person
    landed: Any = load_flow(str(at), caller_globals={})
    declared = landed.describe()
    assert bool(declared.params.model_fields) == takes_params
    return found


async def test_flame_chase_from_one_line(tmp_path: Path) -> None:
    at = await compiled(
        tmp_path,
        "two agents take turns on the same task, one after the other, for a bounded "
        "number of rounds",
    )
    await equivalent(at, drives=2, person=False, takes_params=True)
    assert "range(" in (at / "__init__.py").read_text()


async def test_gen_idea_from_its_description(tmp_path: Path) -> None:
    at = await compiled(
        tmp_path,
        "open a loose idea into a repository-grounded design draft: one agent reads "
        "the repository, expands the idea into a draft with goals, constraints and "
        "open questions, and writes it to a markdown file whose path it prints; one "
        "pass, no loop",
    )
    await equivalent(at, drives=1, person=False, takes_params=False)


async def test_gen_plan_from_its_description(tmp_path: Path) -> None:
    at = await compiled(
        tmp_path,
        "turn a design draft into an implementation plan two sides converge on: a "
        "planner writes and revises the plan file, and an analyst who shares no "
        "context with the planner reviews it fresh each round and answers whether it "
        "is settled; the loop ends when the analyst says settled, and a cap on the "
        "rounds backstops an analyst that never does",
    )
    await equivalent(at, drives=2, person=False, takes_params=True)
    assert "range(" in (at / "__init__.py").read_text()


RLCR = (
    "a builder works through a task under review, in one session that remembers: each "
    "round the builder builds, then a reviewer that shares no context with the builder "
    "reads the repository fresh and answers two things in one shape -- whether there is "
    "nothing left to do, and the findings to hand the builder next, written as its next "
    "prompt with the important ones marked [P0] to [P9]; the findings go to the builder "
    "word for word; the loop ends when the reviewer says there is nothing left, and a "
    "bounded round cap backstops a reviewer that never does"
)


async def test_rlcr_from_its_description(tmp_path: Path) -> None:
    at = await compiled(tmp_path, RLCR)
    await equivalent(at, drives=2, person=False, takes_params=True)
    source = (at / "__init__.py").read_text()
    assert "output_schema=" in source
    assert "range(" in source


@pytest.mark.skipif(
    os.environ.get("AOT_SMOKE", "") != "1",
    reason="the smoke drives the compiled loop with real agents; AOT_SMOKE=1 runs it",
)
async def test_the_compiled_rlcr_runs_once_on_a_toy_repository(tmp_path: Path) -> None:
    at = await compiled(tmp_path, RLCR)
    found = await equivalent(at, drives=2, person=False, takes_params=True)
    workshop = tmp_path / "workshop"
    workshop.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=workshop, check=True)
    (workshop / "README.md").write_text("# workshop\n\nA toy repository.\n")
    builder, reviewer = found.agents
    await run_flow(
        load_flow(str(at), caller_globals={}),
        "create a file called hello.txt containing exactly the line `hello`, and "
        "nothing else; the task is done when that file exists with that content",
        agents={builder: agent_of(builder, WRITER), reviewer: agent_of(reviewer, CRITIC)},
        envs={},
        params={},
        budget=Budget(cost=BUDGET),
        local=local(workshop),
    )
    assert (workshop / "hello.txt").read_text().strip() == "hello"
