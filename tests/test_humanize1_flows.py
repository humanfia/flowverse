from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from hmz.flows import (
    Agent,
    FilesEnvMixin,
    FlowNotFound,
    Outworlder,
    PermissionRequestHookAgentMixin,
    ShellEnvMixin,
)
from hmz.runtime.flowing.engine import load_flow
from hmz.runtime.flowing.environments import local_env
from hmz.runtime.flowing.fakes import FakeAgentDriver, run_fake

from tests.humanize1_kit import FLOW, git, repository

sys.path[:0] = [str(FLOW), str(FLOW.parent)]

CANDIDATE = """# Feature Module

## Goal Description
Add a feature module that says which round wrote it.

## Acceptance Criteria
- AC-1: feature.py exists.

## Task Breakdown
| Task ID | Description | Target AC | Tag (`coding`/`analyze`) | Depends On |
|---------|-------------|-----------|----------------------------|------------|
| task1 | Write feature.py | AC-1 | coding | - |

## Planner-Reviewer Deliberation
### Convergence Status
- Final Status: `converged` or `partially_converged`

## Pending User Decisions
- None.
"""

SETTLED = json.dumps(
    {
        "agree": ["fine"],
        "disagree": [],
        "required_changes": [],
        "optional_improvements": [],
        "unresolved": [],
    }
)
DONE = "It is all there.\n\nMainline Progress Verdict: ADVANCED\nCOMPLETE"
PLACEHOLDER = "[To be populated by the builder based on plan]"

PINNED: dict[str, dict[str, Any]] = {
    "gen-idea": {
        "agents": {"drafter": (Agent, frozenset(), False)},
        "params": ("Idea", ["n", "output"]),
        "resumable": False,
    },
    "gen-plan": {
        "agents": {
            "planner": (Agent, frozenset(), False),
            "analyst": (Agent, frozenset(), False),
        },
        "params": (
            "Plan",
            [
                "input",
                "output",
                "mode",
                "auto_start_rlcr_if_converged",
                "alternative_plan_language",
                "turn_timeout",
                "total_timeout",
                "turn_retries",
            ],
        ),
        "resumable": False,
    },
    "rlcr": {
        "agents": {
            "builder": (None, frozenset({PermissionRequestHookAgentMixin}), False),
            "reviewer": (Agent, frozenset(), False),
            "human": (Outworlder, frozenset(), True),
        },
        "params": (
            "Rlcr",
            [
                "plan_file",
                "max",
                "codex_timeout",
                "full_review_round",
                "base_branch",
                "skip_code_review",
                "track_plan_file",
                "push_every_round",
                "skip_impl",
                "claude_answer_codex",
                "agent_teams",
                "skip_quiz",
                "yolo",
                "privacy",
                "require_bitlesson_entry_for_none",
            ],
        ),
        "resumable": True,
    },
}

CALLER = """
from hmz.flows import (
    Agent, AgentCollection, EnvCollection, FilesEnvMixin, FlowParams, LocalEnv,
    PermissionRequestHookAgentMixin, ShellEnvMixin, flow, load,
)


class Worker(Agent, PermissionRequestHookAgentMixin): ...


class Here(LocalEnv, ShellEnvMixin, FilesEnvMixin): ...


class Agents(AgentCollection):
    worker: Worker
    reviewer: Agent


class Envs(EnvCollection):
    workspace: Here


@flow(agents=Agents, envs=Envs, params=FlowParams)
async def caller(task, *, agents, envs, params, ctx):
    planned = await load("humanize1:gen-plan")(
        task,
        agents={"planner": agents["worker"], "analyst": agents["reviewer"]},
        envs={"workspace": envs["workspace"]},
        params={"input": ".humanize/ideas/draft.md", "mode": "direct", "turn_retries": 0},
    )
    over = await load("humanize1:rlcr")(
        task,
        agents={"builder": agents["worker"], "reviewer": agents["reviewer"]},
        envs={"workspace": envs["workspace"]},
        params={"plan_file": planned, "skip_code_review": True, "privacy": True},
    )
    return planned, over
"""


def _staged(prompt: str) -> Path:
    found = re.search(r"(/\S*\.humanize-plan-[0-9a-f]+\.tmp)", prompt)
    assert found is not None
    return Path(found.group(1))


def _loop(repo: Path) -> Path:
    (loop,) = (repo / ".humanize" / "rlcr").iterdir()
    return loop


def _works(repo: Path) -> Any:
    turns = iter(range(1000))

    def turn(prompt: str, **_: Any) -> str:
        if "Candidate Plan v1" in prompt:
            staged = _staged(prompt)
            held = staged.read_text()
            staged.write_text(
                CANDIDATE
                + "\n--- Original Design Draft Start ---\n"
                + held.split("\n--- Original Design Draft Start ---\n", 1)[1]
            )
            return str(staged)
        if "finish the plan" in prompt:
            return str(_staged(prompt))
        loop = _loop(repo)
        if prompt.startswith("# Finalize Phase"):
            (loop / "finalize-summary.md").write_text("# Finalize Summary\n")
            return "finalized"
        tracker = loop / "goal-tracker.md"
        tracker.write_text(tracker.read_text().replace(PLACEHOLDER, "Write feature.py"))
        (loop / "round-0-summary.md").write_text(
            "# Round 0\n\n## BitLesson Delta\n- Action: none\n- Lesson ID(s): NONE\n"
            "- Notes: none\n"
        )
        (loop / "round-0-contract.md").write_text("# Round 0 Contract\n")
        (repo / "feature.py").write_text(f"TURN = {next(turns)}\n")
        git(repo, "add", "feature.py")
        git(repo, "commit", "-m", "feature")
        return "done"

    return turn


def _reads(prompt: str, *, output_schema: Any, **_: Any) -> Any:
    named = output_schema.__name__ if output_schema is not None else ""
    if named == "Relevance":
        return {"relevant": True, "why": "about this repository"}
    if named == "Compliance":
        return {"relevant": True, "switches_branch": False, "why": "a feature"}
    if named == "Convergence":
        return SETTLED
    if "first planning pass" in prompt:
        return "CORE_RISKS:\n- none"
    return DONE


def _drafts(prompt: str, **_: Any) -> str:
    found = re.search(r"`OUTPUT_FILE`: (\S+)", prompt)
    assert found is not None
    Path(found.group(1)).write_text(
        "# Feature Module\n\n## Original Idea\n\nadd a feature module\n"
    )
    return "drafted"


@pytest.mark.parametrize("name", sorted(PINNED))
def test_each_flow_declares_the_pinned_interface(name: str) -> None:
    pinned = PINNED[name]
    declared = load_flow(f"{FLOW}:{name}", caller_globals={}).describe()  # pyright: ignore[reportAttributeAccessIssue]

    assert declared.name == name
    assert declared.ref == f"humanize1:{name}"
    assert declared.resumable is pinned["resumable"]
    assert not declared.hidden
    agents = {role.name: role for role in declared.agents}
    assert list(agents) == list(pinned["agents"])
    for role_name, (kind, mixins, auto) in pinned["agents"].items():
        role = agents[role_name]
        assert role.required
        assert role.auto is auto
        assert role.capabilities == mixins
        assert role.harness is None
        if kind is not None:
            assert role.declared is kind
    (workspace,) = declared.envs
    assert workspace.name == "workspace"
    assert workspace.auto
    assert workspace.capabilities == frozenset({ShellEnvMixin, FilesEnvMixin})
    params, fields = pinned["params"]
    assert declared.params.__name__ == params
    assert list(declared.params.model_fields) == fields


def test_a_bare_ref_lists_the_three_flows() -> None:
    with pytest.raises(FlowNotFound, match="gen-idea, gen-plan, rlcr"):
        load_flow(str(FLOW), caller_globals={})


@pytest.mark.asyncio
async def test_gen_idea_writes_one_draft_and_never_over_another(
    tmp_path: Path,
) -> None:
    drafter = FakeAgentDriver(reply=_drafts)

    said = await run_fake(
        f"{FLOW}:gen-idea",
        "Add a feature module, please!",
        agents={"drafter": drafter},
        params={"n": 3},
        local=local_env(tmp_path),
    )

    (draft,) = (tmp_path / ".humanize" / "ideas").iterdir()
    assert said == str(draft)
    assert re.fullmatch(r"add-a-feature-module-please-\d{8}-\d{6}\.md", draft.name)
    assert draft.read_text().startswith("# Feature Module")
    (prompt,) = drafter.prompts
    assert "- `N`: 3 directions." in prompt
    assert "Add a feature module, please!" in prompt

    with pytest.raises(ValueError, match="already exists"):
        await run_fake(
            f"{FLOW}:gen-idea",
            "again",
            agents={"drafter": FakeAgentDriver(reply=_drafts)},
            params={"output": str(draft)},
            local=local_env(tmp_path),
        )
    with pytest.raises(ValueError, match="given none"):
        await run_fake(f"{FLOW}:gen-idea", "  ", local=local_env(tmp_path))


@pytest.mark.asyncio
async def test_an_idea_is_planned_and_built_end_to_end(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    (repo / ".git" / "info" / "exclude").write_text("docs/plan.md\n")

    idea = await run_fake(
        f"{FLOW}:gen-idea",
        "add a feature module",
        agents={"drafter": FakeAgentDriver(reply=_drafts)},
        local=local_env(repo),
    )
    plan = await run_fake(
        f"{FLOW}:gen-plan",
        "plan the feature module",
        agents={
            "planner": FakeAgentDriver(reply=_works(repo)),
            "analyst": FakeAgentDriver(reply=_reads),
        },
        params={"turn_retries": 0},
        local=local_env(repo),
    )
    builder = FakeAgentDriver(reply=_works(repo))
    over = await run_fake(
        f"{FLOW}:rlcr",
        "build it",
        agents={"builder": builder, "reviewer": FakeAgentDriver(reply=_reads)},
        params={"skip_code_review": True, "privacy": True},
        local=local_env(repo),
    )

    assert plan == str(repo / "docs" / "plan.md")
    held = Path(plan).read_text()
    assert "Final Status: `converged`" in held
    assert Path(idea).read_text() in held
    assert over == "complete"
    loop = _loop(repo)
    assert (loop / "plan.md").read_text() == held
    assert (loop / "complete-state.md").is_file()
    assert (
        "Add a feature module that says which round wrote it."
        in (loop / "goal-tracker.md").read_text()
    )
    assert git(repo, "status", "--porcelain") == "?? .humanize/"


@pytest.mark.asyncio
async def test_another_flow_calls_gen_plan_and_rlcr_by_ref(tmp_path: Path) -> None:
    verse = tmp_path / "verse"
    (verse / "caller").mkdir(parents=True)
    (verse / "caller" / "__init__.py").write_text(CALLER)
    (verse / "humanize1").symlink_to(FLOW)
    repo = repository(tmp_path / "repo")
    (repo / ".git" / "info" / "exclude").write_text("docs/plan.md\n")
    ideas = repo / ".humanize" / "ideas"
    ideas.mkdir(parents=True)
    (ideas / "draft.md").write_text("Add a feature module.\n")
    worker = FakeAgentDriver(reply=_works(repo))
    reviewer = FakeAgentDriver(reply=_reads)

    planned, over = await run_fake(
        str(verse / "caller"),
        "a feature module",
        agents={"worker": worker, "reviewer": reviewer},
        local=local_env(repo),
    )

    assert planned == str(repo / "docs" / "plan.md")
    assert "Final Status: `partially_converged`" in Path(planned).read_text()
    assert over == "complete"
    assert (_loop(repo) / "complete-state.md").is_file()


@pytest.mark.asyncio
async def test_gen_idea_asks_again_only_while_nothing_is_written(
    tmp_path: Path,
) -> None:
    silent = FakeAgentDriver(reply="")

    with pytest.raises(ValueError, match="answered nothing and wrote no draft"):
        await run_fake(
            f"{FLOW}:gen-idea",
            "an idea",
            agents={"drafter": silent},
            local=local_env(tmp_path),
        )
    assert len(silent.prompts) == 3

    boasting = FakeAgentDriver(reply="done, it is all written")
    with pytest.raises(ValueError, match="wrote no draft, saying: done"):
        await run_fake(
            f"{FLOW}:gen-idea",
            "an idea",
            agents={"drafter": boasting},
            local=local_env(tmp_path),
        )
    assert len(boasting.prompts) == 1

    def quiet(prompt: str, **said: Any) -> str:
        _drafts(prompt, **said)
        return ""

    drafter = FakeAgentDriver(reply=quiet)
    written = await run_fake(
        f"{FLOW}:gen-idea",
        "another idea",
        agents={"drafter": drafter},
        local=local_env(tmp_path),
    )
    assert Path(written).is_file()
    assert len(drafter.prompts) == 1
