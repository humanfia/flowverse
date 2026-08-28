"""The compiler flow, driven by scripted stand-ins: every gate shown to gate.

The writer is a stub that answers a canned spec and then writes a scripted source tree per
attempt; the critic answers canned reviews; the person is the real HumanAgent, absent by
default and answering only where a test says. What is asserted is the compile around them:
a good draft lands whole, a refused one is handed back word for word, an ask nothing serves
is refused before anything is written, and a name already taken is not written over.
"""

# ruff: noqa: D103, PLR2004, S101

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from hmz.agents import AgentBase, AgentConfig, Event, HumanAgent, SessionBase
from hmz.flows import checked, configures, drives, resumes
from hmz.flows.skills import brought

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "aot"
sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import aot  # noqa: E402

if TYPE_CHECKING:
    import os
    from collections.abc import Iterator, Mapping

    import pytest
    from pydantic import BaseModel

CONFIG = AgentConfig(model="test-model", effort="high")


def spec(name: str = "pair_loop", needs: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "about": "two agents take turns until a reviewer says it is done",
        "name": name,
        "seats": [
            {"name": "actor", "person": False, "moments": [], "goal": False},
            {"name": "reviewer", "person": False, "moments": [], "goal": False},
        ],
        "settings": [
            {
                "name": "budget",
                "kind": "number",
                "default": "1.0",
                "about": "millions of output tokens before the loop stops",
            }
        ],
        "endings": [
            {"by": "verdict", "bound": "the reviewer says done, under the budget"}
        ],
        "needs": list(needs),
        "plan": "the actor works, the reviewer reads it fresh, the budget backstops",
    }


#: A draft that passes every gate: a verdict exit with a budget backstop beside it.
GOOD = {
    "pair_loop/__init__.py": '''
    """Two agents take turns until a reviewer says it is done.

    hmz exec -f local/pair_loop -a claude/MODEL:high -a codex/MODEL:high "the task"

    The actor works in its own turn and a fresh reviewer reads the repository; what ends
    the loop is the reviewer saying so, and the budget is the backstop for a reviewer
    that never does.
    """

    import time
    from typing import NamedTuple

    from hmz.flows import Agent, flow
    from pydantic import BaseModel, Field


    class Agents(NamedTuple):
        actor: Agent
        reviewer: Agent


    class Config(BaseModel):
        model_config = {"extra": "forbid"}

        budget: float = Field(
            default=1.0,
            ge=0,
            description="millions of output tokens before the loop stops",
        )


    class Review(BaseModel):
        model_config = {"extra": "forbid"}

        done: bool = Field(description="whether the task is completely done")


    @flow
    def run(agents: Agents, task: str, config: Config | None = None) -> None:
        held = config or Config()
        while True:
            agents.actor(task, suppress=True)
            review = agents.reviewer(task, suppress=True, schema=Review)
            if review is not None and review.done:
                print("the reviewer says it is done")
                return
            if held.budget and agents.actor.spent().output >= held.budget * 1_000_000:
                print("stopping: the budget is spent")
                return
            time.sleep(5)
    ''',
}

#: A first draft the checker refuses outright: a loop nothing inside can end.
DEAD = {
    "pair_loop/__init__.py": '''
    """A loop nothing can end."""

    from hmz.flows import Agent, flow


    @flow
    def run(agents: tuple[Agent, Agent], task: str) -> None:
        while True:
            agents[0](task, suppress=True)
    ''',
}

#: A draft the static reading trusts and the stubs catch: its one exit can never be taken.
STALLING = {
    "pair_loop/__init__.py": '''
    """A loop whose bound is no bound at all."""

    from hmz.flows import Agent, flow


    @flow
    def run(agents: tuple[Agent, Agent], task: str) -> None:
        while True:
            agents[0](task, suppress=True)
            if agents[0].spent().output < 0:
                return
    ''',
}


class WriterSession(SessionBase):
    """Answers the canned spec, then writes the next scripted tree into its cwd."""

    shapes: ClassVar[bool] = True

    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        agent = self._agent
        assert isinstance(agent, WriterAgent)
        if self._id is None:
            self._adopt(f"writer-{id(self)}")
        if schema is not None and schema.__name__ == "Spec":
            yield Event(kind="result", text=json.dumps(agent.blueprint))
            return
        agent.asked.append(prompt)
        if agent.trees:
            tree = agent.trees.pop(0)
            for rel, source in tree.items():
                at = Path(self._cwd or ".") / rel
                at.parent.mkdir(parents=True, exist_ok=True)
                at.write_text(textwrap.dedent(source).strip() + "\n")
        yield Event(kind="result", text="written")


class WriterAgent(AgentBase):
    def __init__(
        self, spec_: dict[str, object], trees: list[Mapping[str, str]]
    ) -> None:
        super().__init__(CONFIG, name="writer")
        self.blueprint = spec_
        self.trees = list(trees)
        #: Every write or repair prompt, in order -- what the gates handed back.
        self.asked: list[str] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> WriterSession:
        return WriterSession(self, cwd)


class CriticSession(SessionBase):
    """Answers the next canned review, approving where the script ran out."""

    shapes: ClassVar[bool] = True

    def _stream(
        self, prompt: str, *, schema: type[BaseModel] | None = None
    ) -> Iterator[Event]:
        del prompt, schema
        agent = self._agent
        assert isinstance(agent, CriticAgent)
        if self._id is None:
            self._adopt(f"critic-{id(self)}")
        said = (
            agent.reviews.pop(0)
            if agent.reviews
            else {"approved": True, "notes": "sound"}
        )
        yield Event(kind="result", text=json.dumps(said))


class CriticAgent(AgentBase):
    def __init__(self, reviews: list[dict[str, object]] | None = None) -> None:
        super().__init__(CONFIG, name="critic")
        self.reviews = list(reviews or [])

    def new(self, cwd: str | os.PathLike[str] | None = None) -> CriticSession:
        return CriticSession(self, cwd)


def compiled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    spec_: dict[str, object] | None = None,
    trees: list[Mapping[str, str]] | None = None,
    reviews: list[dict[str, object]] | None = None,
    human: HumanAgent | None = None,
    config: aot.Config | None = None,
    task: str = "two agents take turns until a reviewer says it is done",
) -> WriterAgent:
    """One compile, in a temporary working directory, and the writer to read back."""
    monkeypatch.chdir(tmp_path)
    writer = WriterAgent(spec_ or spec(), trees if trees is not None else [GOOD])
    agents = aot.Compiling(
        writer=writer, critic=CriticAgent(reviews), human=human or HumanAgent()
    )
    aot.run(agents, task, config or aot.Config(seconds=30.0))
    return writer


def test_a_good_draft_lands_whole(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = compiled(tmp_path, monkeypatch)
    landed = tmp_path / ".humanize" / "flows" / "pair_loop"
    assert (landed / "__init__.py").is_file()
    # What landed is a flow: it declares its agents, and reads clean.
    assert drives(landed / "__init__.py") == ("actor", "reviewer")
    assert checked(landed) == ()
    # One write turn was enough, and the report says what to run.
    assert len(writer.asked) == 1
    out = capsys.readouterr().out
    assert "compiled: pair_loop" in out
    assert 'hmz exec -f local/pair_loop -a CLI/MODEL:EFFORT -a CLI/MODEL:EFFORT' in out
    assert "ends:     by verdict" in out


def test_a_refused_draft_is_handed_back_word_for_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = compiled(tmp_path, monkeypatch, trees=[DEAD, GOOD])
    assert (tmp_path / ".humanize" / "flows" / "pair_loop" / "__init__.py").is_file()
    assert len(writer.asked) == 2
    # The second prompt is a repair, carrying the checker's own finding.
    assert "dead-loop" in writer.asked[1]
    assert "cannot end" in writer.asked[1]


def test_the_stubs_catch_what_the_static_reading_trusts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = compiled(tmp_path, monkeypatch, trees=[STALLING, GOOD])
    assert (tmp_path / ".humanize" / "flows" / "pair_loop" / "__init__.py").is_file()
    assert len(writer.asked) == 2
    assert "did not end" in writer.asked[1]
    assert "never-done" in writer.asked[1]


def test_the_critics_veto_is_a_repair_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = compiled(
        tmp_path,
        monkeypatch,
        trees=[GOOD, GOOD],
        reviews=[{"approved": False, "notes": "the budget knob wants a ceiling"}],
    )
    assert (tmp_path / ".humanize" / "flows" / "pair_loop" / "__init__.py").is_file()
    assert len(writer.asked) == 2
    assert "the budget knob wants a ceiling" in writer.asked[1]


def test_an_ask_nothing_serves_is_refused_before_anything_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = compiled(
        tmp_path,
        monkeypatch,
        spec_=spec(needs=("interrupting a turn mid-stream",)),
    )
    # Nobody at the prompt to narrow it: the compile stops, and nothing was written.
    assert not (tmp_path / ".humanize").exists()
    assert writer.asked == []
    out = capsys.readouterr().out
    assert (
        "cannot compile -- asks for interrupting a turn mid-stream, which nothing "
        "here serves" in out
    )


def test_a_name_already_taken_is_not_written_over(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    kept = tmp_path / ".humanize" / "flows" / "pair_loop"
    kept.mkdir(parents=True)
    (kept / "__init__.py").write_text('"""Somebody\'s own flow."""\n')
    compiled(tmp_path, monkeypatch)
    # The flow that was there is exactly the flow that is there.
    assert (kept / "__init__.py").read_text() == '"""Somebody\'s own flow."""\n'
    assert "already a flow called 'pair_loop'" in capsys.readouterr().out


def test_a_person_may_take_the_draft_the_repairs_ran_out_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    person = HumanAgent()
    person.ask = lambda question: "yes"  # noqa: ARG005 -- every gate answered yes
    writer = compiled(
        tmp_path,
        monkeypatch,
        trees=[DEAD],
        human=person,
        config=aot.Config(repairs=0, seconds=30.0),
    )
    # The draft lands as it stands, dead loop and all: the person said so.
    landed = tmp_path / ".humanize" / "flows" / "pair_loop"
    assert (landed / "__init__.py").is_file()
    assert [one.code for one in checked(landed)] == ["dead-loop"]
    assert len(writer.asked) == 1


def test_the_compiler_passes_its_own_gates() -> None:
    """Dogfood: the flow that holds drafts to the contract holds to it itself."""
    assert checked(FLOW) == ()


def test_what_the_compiler_declares() -> None:
    entry = FLOW / "__init__.py"
    assert drives(entry) == ("writer", "critic")
    assert not resumes(entry)
    config = configures(entry)
    assert config is not None
    assert set(config.model_fields) == {"name", "into", "repairs", "strict", "seconds"}
    assert [skill.name for skill in brought(FLOW)] == ["writing-flows"]
