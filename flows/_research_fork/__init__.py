"""A prepared research child: branch a conversation and hand the parent a read-only tool.

``Session.fork`` branches a conversation in place, preserving the parent's prefix; this is the
delegation workflow on top of it. A tool callback runs while the parent's turn is active and
so cannot fork then -- it has to have a child already prepared, between turns. That is what
:class:`ResearchFork` is: ``prepare`` does the eager fork and hands back a slot with the
callback and a ``close``, so a flow writes::

    slot = ResearchFork.prepare(parent, permission="read-only")
    parent.offers([slot.tool])
    try:
        answer = await parent.aturn("Use the research tool if evidence is needed.")
    finally:
        slot.close()

The child runs read-only by default and offers no tools of its own, so the research it can do
is bounded and cannot recurse. The helper is one-shot: a flow prepares a new slot for another
parent turn rather than reaching for the one it closed.

Only Claude and Codex have a native fork, so ``prepare`` refuses any other backend before a
child is made -- and it refuses an unopened, running, closed or moved parent for the same
reason ``Session.fork`` does, which is where a loop would otherwise find out.
"""

from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from hmz.flows import Session, Tool

if TYPE_CHECKING:
    from collections.abc import Iterable

#: The stable prefix every error the callback raises carries, so a parent can tell a research
#: failure from any other tool failure -- and so a compiler can pin the failure's wording.
ERROR = "research-fork"
# This flow is shipped outside the hmz package, so keep its public contract's minimum here for
# flowverse tooling and release checks to read without importing a backend implementation.
MIN_HUMANIZE2_VERSION = ">=0.1.0"
_CLEANUP_GRACE = 2.0


class Question(BaseModel):
    """What the parent asks the research child to find out, in the workspace."""

    model_config = {"extra": "forbid"}

    question: str = Field(
        description="The read-only question to research in the workspace."
    )


class Finding(BaseModel):
    """What the research child answers with, as the digest the parent is handed."""

    model_config = {"extra": "forbid"}

    answer: str = Field(description="The answer, in prose the parent can act on.")
    sources: list[str] = Field(
        default_factory=list,
        description="The files and lines the answer rests on, for the parent to check.",
    )


class ResearchFork:
    """A child branched from a parent, prepared to answer one read-only question."""

    def __init__(
        self, parent: Session, child: Session, *, timeout: float, max_output_chars: int
    ) -> None:
        self._parent = parent
        self._child = child
        self._timeout = timeout
        self._max = max_output_chars
        self._closed = False
        self._used = False
        self._busy = False
        self._state = threading.Lock()
        self._parent_tools = tuple(getattr(parent, "tools", ()))

    @classmethod
    def prepare(
        cls,
        parent: Session,
        *,
        permission: str = "read-only",
        timeout: float = 60,
        max_output_chars: int = 32000,
        tools: Iterable[Tool] = (),
    ) -> ResearchFork:
        """Branches the parent eagerly and hands back a slot, before any turn is waiting.

        Args:
          parent: The conversation to branch, which must be a Claude or Codex session and must
            be open, idle and unmoved -- exactly what :meth:`Session.fork` holds it to.
          permission: The rung the child runs at, read-only by default.
          timeout: Seconds the child's turn and its validation may take, before the child is
            closed and the callback answers with a timeout.
          max_output_chars: The most the child's answer may come to.
          tools: Explicit read-only callbacks to offer to the child; empty by default. The
            caller is responsible for ensuring every supplied callback is read-only.

        Returns:
          The slot, whose :attr:`tool` is handed to the parent with
          ``parent.offers([slot.tool])``.

        Raises:
          NotImplementedError: For a parent whose backend has no native fork.
        """
        if not getattr(type(parent), "forks", False) or not callable(
            getattr(parent, "fork", None)
        ):
            raise NotImplementedError(
                f"{type(parent).__name__} has no native fork to prepare a research child from"
            )
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        previous = tuple(getattr(parent, "tools", ()))
        selected_tools = tuple(tools)
        child = parent.fork(permission=permission)
        # Replace inherited callbacks with the explicit allowlist. In particular, the parent
        # must never be exposed back to the child through the research callback itself.
        try:
            child.offers(selected_tools or [])
        except BaseException:
            child.close()
            raise
        slot = cls(parent, child, timeout=timeout, max_output_chars=max_output_chars)
        slot._parent_tools = previous
        return slot

    @property
    def tool(self) -> Tool:
        """The callback the parent reaches for to put a question to the prepared child."""
        return Tool(
            name="research",
            about="Ask a read-only research child for evidence about the workspace.",
            takes=Question,
            call=self._research,
        )

    def _research(self, asked: Question) -> str:
        """Runs the child's one turn under a deadline, and returns a validated digest.

        Raised as a ``RuntimeError`` carrying the ``research-fork`` prefix, which the tool
        server answers to the parent as the callback having failed -- never as an empty
        finding, since a failed research is not a research that found nothing.

        Args:
          asked: What to ask the child.

        Returns:
          The child's answer, stripped and held to the size it was allowed.

        Raises:
          RuntimeError: If the child is closed, its turn failed, answered out of shape, answered
            with nothing, answered too much, or ran past the deadline.
        """
        with self._state:
            if self._closed:
                raise RuntimeError(f"{ERROR}: the research child is closed")
            if self._used:
                raise RuntimeError(
                    f"{ERROR}: the research child is one-shot and already used"
                )
            if self._busy:
                raise RuntimeError(f"{ERROR}: the research child is already running")
            self._busy = True
        deadline = time.monotonic() + self._timeout
        held: dict[str, Any] = {}

        def run() -> None:
            try:
                held["finding"] = self._child(asked.question, schema=Finding)
            except BaseException as why:  # noqa: BLE001 -- the child's failure, not ours
                held["failed"] = why

        working = threading.Thread(target=run, daemon=True)
        working.start()
        # A monotonic deadline, not `asyncio.wait_for` alone: the awaited-turn worker carries on
        # after task cancellation, so the child is closed rather than left to keep thinking.
        try:
            working.join(timeout=max(deadline - time.monotonic(), 0.0))
            if working.is_alive():
                self._child.close()  # Claude joins its process; Codex stops only the fork runtime
                working.join(timeout=_CLEANUP_GRACE)
                if working.is_alive():
                    raise RuntimeError(
                        f"{ERROR}: the child took longer than {self._timeout} seconds; "
                        "child cleanup did not finish"
                    )
                raise RuntimeError(
                    f"{ERROR}: the child took longer than {self._timeout} seconds"
                )
            if failed := held.get("failed"):
                raise RuntimeError(
                    f"{ERROR}: the child turn failed: {failed}"
                ) from failed
            finding = held.get("finding")
            if not isinstance(finding, Finding):
                # The public callback contract reports malformed model output as a tool error.
                # Keep the stable RuntimeError type while making the intent explicit to ruff.
                raise RuntimeError(  # noqa: TRY004 -- stable tool error type
                    f"{ERROR}: the child answered in no shape to read back"
                )
            serialized = finding.model_dump_json()
            if len(serialized) > self._max:
                raise RuntimeError(
                    f"{ERROR}: the child's serialized answer is {len(serialized)} characters, "
                    f"over the {self._max} allowed"
                )
            said = finding.answer.strip()
            if not said:
                raise RuntimeError(f"{ERROR}: the child answered with nothing")
            if len(said) > self._max:
                raise RuntimeError(
                    f"{ERROR}: the child's answer is {len(said)} characters, over the "
                    f"{self._max} allowed"
                )
            return said
        finally:
            with self._state:
                self._busy = False
                self._used = True
            self._child.close()

    def close(self) -> None:
        """Closes the child and takes the callback back off the parent.

        Doing it twice does it once. A flow that offered callbacks beside the research tool
        re-offers them after this, since it is the conversation's list that is taken back.
        """
        if self._closed:
            return
        self._closed = True
        self._child.close()
        self._parent.offers(self._parent_tools or None)
