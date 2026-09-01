"""A prepared research child, held to the contract a flow writes against.

The helper is driven against fake native drivers -- plain classes answering to the public
`Session` protocol -- so what is checked is the fork it makes, the tools it hands out and
takes back, and the errors it answers with. Nothing here imports `hmz.agents` or reaches into
a driver.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from pydantic import BaseModel

from _research_fork import ERROR, Finding, Question, ResearchFork
from hmz.flows import Tool


class FakeChild:
    """A forked child, answering in the shape it was asked for or failing as told."""

    def __init__(
        self,
        answer: Finding | None = None,
        *,
        fail: BaseException | None = None,
        block: float | None = None,
    ) -> None:
        self.answer = answer
        self.fail = fail
        self.block = block
        self.offered: Any = None
        self.tools: tuple[Any, ...] = ()
        self.closed = False
        self.prompts: list[tuple[str, type[BaseModel] | None]] = []

    def offers(self, tools: Any) -> None:
        self.offered = tools
        self.tools = tuple(tools or ())

    def __call__(
        self,
        prompt: str,
        *,
        suppress: bool = False,
        schema: type[BaseModel] | None = None,
    ) -> Any:
        self.prompts.append((prompt, schema))
        if self.block is not None:
            time.sleep(self.block)
        if self.fail is not None:
            raise self.fail
        return self.answer

    def close(self) -> None:
        self.closed = True


class FakeParent:
    """A conversation that can be forked, recording what it is asked."""

    forks = True

    def __init__(self, child: FakeChild | None = None) -> None:
        self.child = child or FakeChild()
        self.forked: list[tuple[str | None, str | None]] = []
        self.offered: Any = None
        self.tools: tuple[Any, ...] = ()

    def fork(
        self, *, last_turn_id: str | None = None, permission: str | None = None
    ) -> FakeChild:
        self.forked.append((last_turn_id, permission))
        return self.child

    def offers(self, tools: Any) -> None:
        self.offered = tools
        self.tools = tuple(tools or ())


class NoForkParent(FakeParent):
    """A backend with no native fork, which a research child cannot be made from."""

    forks = False


def finding(answer: str = "it holds", sources: list[str] | None = None) -> Finding:
    return Finding(answer=answer, sources=sources or [])


def test_prepare_forks_eagerly_and_replaces_the_childs_tools() -> None:
    """The fork is made by `prepare`, and the child offers nothing of its own."""
    parent = FakeParent()

    slot = ResearchFork.prepare(parent, permission="read-only")

    assert parent.forked == [(None, "read-only")]  # eager, read-only, latest boundary
    assert parent.child.offered == []  # the inherited tools are replaced with nothing


def test_the_tool_puts_the_question_to_the_child_and_returns_the_digest() -> None:
    parent = FakeParent(FakeChild(finding("the tests pass", ["tests/x.py:1"])))
    slot = ResearchFork.prepare(parent)

    said = slot.tool.called(Question(question="what runs").model_dump())

    assert said == "the tests pass"
    assert parent.child.prompts == [("what runs", Finding)]


def test_prepare_accepts_only_the_explicit_child_tool_allowlist() -> None:
    parent = FakeParent()
    allowed = Tool(name="read", about="read evidence", call=lambda: "ok")

    ResearchFork.prepare(parent, tools=[allowed])

    assert parent.child.offered == (allowed,)


def test_a_research_child_is_one_shot() -> None:
    parent = FakeParent(FakeChild(finding("first")))
    slot = ResearchFork.prepare(parent)
    question = Question(question="what runs").model_dump()

    assert slot.tool.called(question) == "first"
    with pytest.raises(RuntimeError, match="one-shot"):
        slot.tool.called(question)


def test_an_unsupported_parent_is_refused_before_a_child_is_made() -> None:
    with pytest.raises(NotImplementedError, match="no native fork"):
        ResearchFork.prepare(NoForkParent())


def test_a_failed_child_turn_is_a_tool_error_not_an_empty_finding() -> None:
    parent = FakeParent(FakeChild(fail=RuntimeError("the backend refused")))
    slot = ResearchFork.prepare(parent)

    with pytest.raises(RuntimeError, match=ERROR):
        slot.tool.called(Question(question="what runs").model_dump())


def test_an_oversized_answer_is_a_tool_error() -> None:
    parent = FakeParent(FakeChild(finding("x" * 100)))
    slot = ResearchFork.prepare(parent, max_output_chars=10)

    with pytest.raises(RuntimeError, match="over the 10 allowed"):
        slot.tool.called(Question(question="what runs").model_dump())


def test_a_child_that_runs_past_the_deadline_is_closed_and_answered_with_a_timeout() -> (
    None
):
    parent = FakeParent(FakeChild(block=10))
    slot = ResearchFork.prepare(parent, timeout=0.05)

    with pytest.raises(RuntimeError, match="longer than"):
        slot.tool.called(Question(question="what runs").model_dump())

    assert parent.child.closed  # the child was cancelled, not left thinking


def test_close_closes_the_child_and_takes_the_tool_back() -> None:
    parent = FakeParent()
    slot = ResearchFork.prepare(parent)
    parent.offers([slot.tool])

    slot.close()

    assert parent.child.closed
    assert parent.offered is None  # unregistered from the parent
    with pytest.raises(RuntimeError, match="closed"):
        slot.tool.called(Question(question="what runs").model_dump())


def test_close_restores_tools_the_parent_had_before_preparation() -> None:
    parent = FakeParent()
    existing = object()
    parent.offers([existing])
    slot = ResearchFork.prepare(parent)
    parent.offers([slot.tool])

    slot.close()

    assert parent.tools == (existing,)


def test_close_is_one_thing_however_often_it_is_called() -> None:
    parent = FakeParent()
    slot = ResearchFork.prepare(parent)

    slot.close()
    slot.close()

    assert parent.child.closed


def test_the_question_is_the_whole_of_what_the_child_is_asked() -> None:
    """The child is asked the research question and nothing else: no prompt, no boundary."""
    parent = FakeParent(FakeChild(finding("yes")))
    slot = ResearchFork.prepare(parent)

    slot.tool.called(Question(question="does it hold?").model_dump())

    (prompt, schema) = parent.child.prompts[0]
    assert prompt == "does it hold?"
    assert schema is Finding
