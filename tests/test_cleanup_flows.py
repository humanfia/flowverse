from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import flame_chase_agent_cleanup as flame
import pytest
import ralph_loop_agent_cleanup as ralph
from _workspace_cleanup import Config, loop, storage
from hmz.flows import Allowance, Question, Stopped
from hmz.runtime.flowing import configures, declared, drives, offered, resumes

FLOWS = Path(__file__).parents[1] / "flows"
NAMES = ("flame_chase_agent_cleanup", "ralph_loop_agent_cleanup")


class Ledger:
    """A run's allowance counted in turns: the turn after the last raises Stopped."""

    def __init__(self, turns: int) -> None:
        self.left = turns

    def spend(self) -> None:
        if self.left <= 0:
            raise Stopped("allowance spent")
        self.left -= 1


class FakeSession:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent
        self.budget = None
        self.said: list[str] = []

    def spent(self) -> SimpleNamespace:
        return SimpleNamespace(total=self.agent.turns)

    def interject(self, text: str) -> None:
        self.said.append(text)

    def __call__(self, prompt: str, **_kwargs: Any) -> Any:
        self.agent.ledger.spend()
        self.agent.events.append(self.agent.name)
        self.agent.turns += 1
        return self.agent.answers.pop(0) if self.agent.answers else "done"


class FakeAgent:
    def __init__(
        self,
        name: str,
        events: list[str],
        ledger: Ledger,
        answers: list[Any] | None = None,
        session: type[FakeSession] = FakeSession,
    ) -> None:
        self.name = name
        self.events = events
        self.ledger = ledger
        self.answers = list(answers or [])
        self.turns = 0
        self.session = session
        self.sessions: list[FakeSession] = []

    def new(self, cwd: str | None = None) -> FakeSession:
        session = self.session(self)
        self.sessions.append(session)
        return session


class Human:
    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.questions: list[Question] = []

    def asked(self, question: Question) -> str | None:
        self.questions.append(question)
        return self.answer


@pytest.fixture(autouse=True)
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working repository, with Humanize's home beside it rather than inside."""
    monkeypatch.setattr(storage, "home", lambda: tmp_path / "humanize")
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    for flow in (flame, ralph):
        monkeypatch.setattr(flow, "time", SimpleNamespace(sleep=lambda _seconds: None))
    return root


def _configured(**overrides: Any) -> Config:
    return Config(work_paths=("src",), **overrides)


def test_flows_are_public_resumable_and_declare_a_token_allowance() -> None:
    assert drives(FLOWS / "flame_chase_agent_cleanup" / "__init__.py") == (
        "first_chaser",
        "second_chaser",
        "cleaner",
    )
    assert drives(FLOWS / "ralph_loop_agent_cleanup" / "__init__.py") == (
        "agent",
        "cleaner",
    )
    names = offered(FLOWS)
    assert "_workspace_cleanup" not in names
    for name in NAMES:
        path = FLOWS / name / "__init__.py"
        assert name in names
        assert resumes(path)
        assert declared(path) == Allowance(tokens=10.0)
        model = configures(path)
        assert model is not None
        assert "budget" not in model.model_fields
        assert set(model.model_fields) == {
            "work_paths",
            "cleanup_turns",
            "next_lines",
            "comment_lines",
            "repairs",
            "check_command",
            "session_timeout_minutes",
            "idle_timeout_minutes",
            "stop_grace_minutes",
            "max_tracked_file_mb",
            "confirm_large_workspace_copies",
        }
        held = model(work_paths=("src",))
        assert (held.cleanup_turns, held.next_lines, held.comment_lines) == (3, 10, 30)
        assert (held.repairs, held.check_command) == (2, "")
        assert held.session_timeout_minutes == 240
        assert held.idle_timeout_minutes == 20
        assert held.stop_grace_minutes == 10
        assert held.max_tracked_file_mb == 10
        assert held.confirm_large_workspace_copies is True


@pytest.mark.parametrize("flow", [flame, ralph])
def test_a_run_nobody_set_up_names_work_paths(flow: Any) -> None:
    agents = [FakeAgent("a", [], Ledger(0)) for _ in flow.Agents._fields]
    with pytest.raises(ValueError, match="work_paths"):
        flow.run(flow.Agents(*agents), "task", None, {})


@pytest.mark.parametrize(
    "value", [(), ("../outside",), ("src", "src/generated"), ("src", "src"), (".git",)]
)
def test_work_paths_must_be_safe_and_non_overlapping(value: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        Config(work_paths=value)


def test_flame_chase_alternates_retries_an_empty_turn_and_cleans_between_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    ledger = Ledger(5)
    first = FakeAgent("first", events, ledger, answers=["", "done"])
    second = FakeAgent("second", events, ledger)
    cleaner = FakeAgent("cleaner", events, ledger)
    monkeypatch.setattr(flame, "clean_epoch", lambda *_args: events.append("clean"))
    state: dict[str, Any] = {}

    with pytest.raises(Stopped):
        flame.run(
            flame.Agents(first, second, cleaner, Human(None)),
            "task",
            _configured(),
            state,
        )

    assert events == ["first", "first", "second", "first", "clean", "second"]
    assert (state["turns"], state["epoch"]) == (4, 1)
    assert Path(state["run_root"]).is_dir()


def test_ralph_hands_each_due_cleanup_to_its_cleaner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    ledger = Ledger(4)
    coder = FakeAgent("coder", events, ledger)
    cleaner = FakeAgent("cleaner", events, ledger)

    def clean(agent: FakeAgent, *_args: Any) -> None:
        assert agent is cleaner
        events.append("clean")

    monkeypatch.setattr(ralph, "clean_epoch", clean)
    with pytest.raises(Stopped):
        ralph.run(ralph.Agents(coder, cleaner, Human(None)), "task", _configured(), {})

    assert events == ["coder", "coder", "coder", "clean", "coder"]


# The flow modules imported above, rather than importlib's: humanize reads a flow by
# running its file, which replaces what sys.modules holds under the flow's name.
@pytest.mark.parametrize("flow", [flame, ralph])
def test_three_empty_turns_in_a_row_end_the_run_and_keep_its_state(
    flow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    ledger = Ledger(100)
    agents = [
        FakeAgent(field, events, ledger, answers=[""] * 10)
        for field in flow.Agents._fields[:-1]
    ]
    monkeypatch.setattr(flow, "clean_epoch", lambda *_args: events.append("clean"))
    state: dict[str, Any] = {}

    flow.run(flow.Agents(*agents, Human(None)), "task", _configured(), state)

    assert len(events) == loop.STALLED
    assert len(set(events)) == 1
    assert state["turns"] == 0
    assert "run_root" in state


class Long(FakeSession):
    """A turn the clock ends: it runs past the limit and answers with nothing."""

    def __call__(self, prompt: str, **kwargs: Any) -> Any:
        super().__call__(prompt, **kwargs)
        time.sleep(0.15)
        return ""


def test_a_turn_the_clock_ended_counts_and_hands_over() -> None:
    events: list[str] = []
    ledger = Ledger(2)
    first = FakeAgent("first", events, ledger, session=Long)
    second = FakeAgent("second", events, ledger)
    held = _configured(
        session_timeout_minutes=0.001, idle_timeout_minutes=0, stop_grace_minutes=0
    )
    state: dict[str, Any] = {}

    with pytest.raises(Stopped):
        flame.run(
            flame.Agents(first, second, FakeAgent("c", events, ledger), Human(None)),
            "task",
            held,
            state,
        )

    assert events == ["first", "second"]
    assert state["turns"] == 2
    session = first.sessions[0]
    assert session.budget is not None
    assert session.budget.seconds == pytest.approx(0.06)
    assert any("Wrap up" in said for said in session.said)


@pytest.mark.parametrize("answer", [None, "Stop"])
def test_a_large_workspace_is_asked_about_and_not_started_unless_confirmed(
    answer: str | None, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    monkeypatch.setattr(loop, "FILES_WARNING", 1)
    human = Human(answer)
    agent = FakeAgent("coder", [], Ledger(0))
    state: dict[str, Any] = {}

    with pytest.raises(loop.LargeWorkspace):
        ralph.run(ralph.Agents(agent, agent, human), "task", _configured(), state)

    assert len(human.questions) == 1
    assert "2 files" in human.questions[0].text
    assert state == {}
    assert not (tmp_path / "humanize").exists()


def test_a_confirmed_or_unasked_large_workspace_starts(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    monkeypatch.setattr(loop, "FILES_WARNING", 1)
    for human, held in (
        (Human("Start anyway"), _configured()),
        (Human(None), _configured(confirm_large_workspace_copies=False)),
    ):
        agent = FakeAgent("coder", [], Ledger(0))
        with pytest.raises(Stopped) as raised:
            ralph.run(ralph.Agents(agent, agent, human), "task", held, {})
        assert not isinstance(raised.value, loop.LargeWorkspace)


def test_a_resumed_run_is_not_asked_again(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "a.txt").write_text("a\n")
    agent = FakeAgent("coder", [], Ledger(0))
    state: dict[str, Any] = {}
    with pytest.raises(Stopped):
        ralph.run(ralph.Agents(agent, agent, Human(None)), "task", _configured(), state)

    monkeypatch.setattr(loop, "FILES_WARNING", 0)
    human = Human(None)
    with pytest.raises(Stopped) as raised:
        ralph.run(ralph.Agents(agent, agent, human), "task", _configured(), state)
    assert not isinstance(raised.value, loop.LargeWorkspace)
    assert human.questions == []


def test_run_storage_uses_humanize_home_and_validates_resume(
    repo: Path, tmp_path: Path
) -> None:
    source = repo
    state: dict[str, Any] = {}

    root, resumed = storage.open_store("some_flow", source, state)

    assert not resumed
    assert root.is_relative_to(tmp_path / "humanize" / "some_flow")
    assert state["run_root"] == str(root)
    assert state["run_id"] == root.name
    assert storage.open_store("some_flow", source, state) == (root, True)
    with pytest.raises(ValueError):
        storage.open_store("some_flow", source, {**state, "run_root": str(tmp_path)})
    with pytest.raises(ValueError):
        storage.open_store("some_flow", source, {"turns": 3})


def test_run_storage_refuses_humanize_home_inside_cleaned_repository(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = repo
    monkeypatch.setattr(storage, "home", lambda: source / ".humanize")

    with pytest.raises(RuntimeError):
        storage.open_store("some_flow", source, {})
