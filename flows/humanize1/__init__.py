"""RLCR (humanize 1) -- PolyArch/humanize as three flows, each set up before it starts.

    hmz exec -f official/humanize1:gen-idea -a claude/claude-opus-4-8:max \
        "add undo/redo to the editor"
    hmz exec -f official/humanize1:gen-plan \
        -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max \
        "add undo/redo to the editor"
    hmz exec -f official/humanize1:rlcr \
        -a claude/claude-opus-4-8:max -a codex/gpt-5.6-sol:max "build it"

which are the plugin's three commands: `gen-idea` opens a loose idea into a repo-grounded
draft, `gen-plan` turns that draft into a plan both sides have converged on, and `rlcr` builds
the plan under review until nothing is left to say. Everything each of them can be told is on
`/config` -- one field per flag the plugin takes, under the name the plugin gives it. Add
`-c setup.yaml` to run one set up rather than as it comes, and `hmz -f official/humanize1:rlcr
-c setup.yaml` opens the interface on the same setup.

Three rather than one because each is set up on its own and stops on its own: `/agents` asks
one flow for the drafter, one for the planner and the analyst that reads it, and one for the
builder and the reviewer that reads it. What passes between them is a file, as it is in the
plugin -- the draft, then the plan -- so an idea may be opened on one model, planned on
another and built on a third, with whatever reading and editing you like in between.

Run in a git repository: the work is anchored to the commit the plan was fixed in, and every
review reads what came after it.

`rlcr` is a loop meant to run for days, so a run of it can be picked up where the last one
stopped. What it keeps is where the loop is -- `.humanize/rlcr/<stamp>` -- and the round it
has reached; everything else about the loop is already in that directory in the plugin's own
format, and a second copy of it here would be a second place for it to be wrong. So running
`rlcr` again carries on in that directory instead of stamping a new one: the loop's live
state file is read back as it stands, none of what it anchors the loop to is worked out again
from the repository, the setup that has already happened does not happen twice, and the
builder -- which is a new session, no backend having any way to reopen the old one -- is
started on the prompt the loop last wrote down. Whichever phase it stopped in: the finalize
round and the methodology analysis rename the state file rather than ending the loop, so a
loop stopped in one of them is carried on inside it, which is a round away from the end
rather than a week of work to plan again.

A loop carries on with the settings it was set up with, which is what carrying on means: a
loop whose `max` was halved halfway is not the loop it was, and the rounds behind it were
judged as the loop it was. So a run set up differently is neither quietly overridden nor
quietly ignored -- it says which setting it disagrees with the loop about and starts a loop
of its own, set up the way this run asked for. The agents are the one thing that is not a
setting: `-c` has no field for them, `/agents` chooses them per run, so the reviewer is
whoever was chosen this time and the state file is brought up to date to say who is reading
the rounds.

A loop that ended renamed its state file on the way out and so is never picked up; neither is
one whose directory has gone, one whose state this version of the flow cannot read, one this
run was set up differently from, or one the repository has moved out from under -- the work
on another branch now, the plan changed since, which is what the plugin tells you to do when
a plan is wrong. Each of those starts a fresh loop and says so. `gen-idea` and
`gen-plan` keep nothing and are not picked up. What each of them does is write one file,
running one again is meant to write another, and between their turns there is nothing a
second run could honestly carry on from.

The side that writes remembers and the side that reads does not. The planner holds one session
for the whole of the planning and the builder holds one for the whole of the loop; every review
is a session that has just started, reads the repository itself, and is told nothing about how
the work was arrived at.

The loop itself is a hook. The plugin blocks Claude's exit and puts the round to Codex there;
so does this -- a `Stop` hook on the builder, which is the same sentence: a round ends when the
builder believes the whole plan is done and tries to stop, and what the reviewer says is what
it hears instead of stopping. The plugin's tool validators are hooks too, on the one moment a
refusal reaches the agent, so the plan stays fixed and the state file stays the loop's. Every
gate its stop hook runs is run here, in the order it runs them, in its own words -- and what it
writes is written where it writes it, so `humanize monitor rlcr` reads a run of this.

Four things are the plugin's mechanism rather than its behaviour, and are done another way:

- `codex review --base <ref>` takes no prompt and is a Codex feature. Here the reviewer is
  whichever agent was chosen, so the code review is asked for -- in a prompt that asks for
  exactly the `[P0-9]` output the loop then reads the same way.
- `--codex-timeout` cannot cut a turn short from here: a review that ran over is treated as a
  review that failed, which is the state the plugin's own timeout leaves the round in.
- A task the plan tags `analyze` is `/humanize:ask-codex` there, which is a shell script the
  builder runs. Here the builder has no way to reach the reviewer mid-round, so it is told to
  put the question in its round summary, where the reviewer answers it.
- Its `PostToolUse` hook patches the session id into `state.md` so a later hook can tell whose
  loop it is. This flow is holding the loop, so there is nothing to look up.

`ask-codex`, `ask-gemini`, `refine-plan` and `cancel-rlcr-loop` are commands of their own
rather than phases of this one, and are not here: stopping the flow is what cancels it.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple, cast

from _humanize1 import guards, loop, planning, prompts
from _humanize1.loop import Loop, State, answered, git, spoken
from _humanize1.prompts import render
from pydantic import BaseModel, Field, model_validator

# Under a name of its own: what a person is put is one of these, and the shape of the
# quiz below has a `Question` of its own that is a field of the model the reviewer fills.
from hmz.flows import Agent, Moment, Person, Session, Stopped, Unrecoverable, flow
from hmz.flows import Question as Asking

if TYPE_CHECKING:
    from collections.abc import Callable


class Drafting(NamedTuple):
    """`gen-idea`'s one agent, which explores the idea and writes the draft."""

    drafter: Agent


class Planning(NamedTuple):
    """`gen-plan`'s two: the one that writes the plan, and the one that reads it back."""

    planner: Agent
    analyst: Agent


class Building(NamedTuple):
    """`rlcr`'s two, and the person at the prompt.

    The builder has to run `PermissionRequest`: the plugin's validators are what keep the plan
    fixed and the loop's state out of the builder's hands, and a hook that cannot say no to a
    tool is not one of them. The plugin is a Claude Code plugin for the same reason.

    The person is only ever asked, and never said to. A loop meant to run for days is one that
    is left running with nobody at the prompt, and a turn said to the person waits for them to
    type it back however long that takes -- so the one thing this puts to them, the plan
    understanding quiz, goes the road a coding agent's own question goes and is answered with
    nothing where there is nobody to answer. `/afk` and a command line are then the same thing
    to it, which is what they are meant to be.
    """

    builder: Annotated[Agent, Moment.PERMISSION_REQUEST]
    reviewer: Agent
    human: Person


#: Every language the plugin will write a translated plan in, by name and by ISO code.
LANGUAGES = {
    "chinese": "zh",
    "korean": "ko",
    "japanese": "ja",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "portuguese": "pt",
    "russian": "ru",
    "arabic": "ar",
}

#: How many rounds `gen-plan` gives its convergence loop, which is the plugin's own maximum.
CONVERGING = 3

#: The headings the original command requires in every convergence review.
_REVIEW_HEADINGS = (
    "AGREE",
    "DISAGREE",
    "REQUIRED_CHANGES",
    "OPTIONAL_IMPROVEMENTS",
    "UNRESOLVED",
)

#: The original command's second convergence stop: two revisions with no implementation delta.
_NO_MATERIAL_ROUNDS = 2

#: How long a stopped backend is given to unwind before the bounded flow moves on.
_STOP_GRACE = 1.0

#: Where a draft goes when nobody said, as `validate-gen-idea-io.sh` resolves it.
IDEAS = ".humanize/ideas"

#: What the plan is called when nobody said, which is what the plugin's own examples use.
PLAN = "docs/plan.md"


class Relevance(BaseModel):
    """Whether a draft is about this repository at all, which `gen-plan` will not start without.

    One of the four questions this flow puts to an agent rather than sets it to work on. Each
    is a model like this one: the fields are the whole of what is being asked, the backend is
    held to them, and the flow reads a field rather than looking for a word at the start of a
    paragraph.
    """

    model_config = {"extra": "forbid"}

    relevant: bool = Field(
        description="Whether the draft is related to this repository. Be lenient: false only "
        "for a draft that is clearly about something else entirely."
    )
    why: str = Field(description="One or two sentences saying why.")


class Convergence(BaseModel):
    """One review round, retaining the original flow's public answer shape."""

    model_config = {"extra": "forbid"}

    converged: bool = Field(
        default=False,
        description="Your provisional convergence judgment; the flow verifies it from review "
        "headings before acting on it.",
    )
    review: str = Field(
        default="",
        description="The review under AGREE, DISAGREE, REQUIRED_CHANGES, "
        "OPTIONAL_IMPROVEMENTS and UNRESOLVED headings. Keep each item concise.",
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_structured(cls, value: Any) -> Any:
        """Accept the temporary list-shaped schema emitted by early fixed flow versions."""
        if not isinstance(value, dict) or "review" in value:
            return value
        names = (
            "agree",
            "disagree",
            "required_changes",
            "optional_improvements",
            "unresolved",
        )
        if not any(name in value for name in names):
            return value

        def section(name: str, items: Any) -> str:
            values = items if isinstance(items, list) else []
            body = "\n".join(f"- {item}" for item in values) or "- None"
            return f"{name.upper()}:\n{body}"

        review = "\n\n".join(section(name, value.get(name, [])) for name in names)
        return {"converged": bool(value.get("converged", False)), "review": review}

    def _sections(self) -> dict[str, list[str]]:
        """Reads the five review headings into blocker lists."""
        sections = {name: [] for name in _REVIEW_HEADINGS}
        current: str | None = None
        for line in self.review.splitlines():
            match = re.match(r"^\s*(?:[-*]\s*)?([A-Z_]+):\s*(.*)$", line)
            if match is not None and match.group(1) in sections:
                current = match.group(1)
                if match.group(2).strip() and match.group(2).strip().lower() != "none":
                    sections[current].append(match.group(2).strip())
            elif current is not None and line.strip():
                item = re.sub(r"^[-*]\s+", "", line.strip())
                if item.lower() != "none":
                    sections[current].append(item)
        return sections

    @property
    def settled(self) -> bool:
        """Whether the review contains no material disagreement or open decision."""
        sections = self._sections()
        if any(sections.values()):
            return not any(
                sections[name]
                for name in ("DISAGREE", "REQUIRED_CHANGES", "UNRESOLVED")
            )
        # Older flow tests used a compact `AGREE` review without colons. Preserve that
        # migration shape, but never let an empty/malformed review converge by boolean alone.
        return self.converged and bool(
            re.search(r"\bAGREE\b", self.review, re.IGNORECASE)
        )

    def rendered(self) -> str:
        """Renders the original plugin's five review headings for the planner."""
        if self.review.strip():
            return self.review.strip()
        return "\n\n".join(f"{name}:\n- None" for name in _REVIEW_HEADINGS)


class Compliance(BaseModel):
    """The two things a plan is checked for before a loop is started to build it."""

    model_config = {"extra": "forbid"}

    relevant: bool = Field(
        description="Whether the plan is about this repository. Lean towards true."
    )
    switches_branch: bool = Field(
        description="Whether the plan tells the implementer to switch, check out or create a "
        "git branch as part of the work. Lean towards false: `git checkout -- <file>` and "
        "'stay on the current branch' are not branch switches."
    )
    why: str = Field(
        description="What the plan is about, in a sentence -- or, where either check failed, "
        "the reason, quoting the instruction that requires the branch switch."
    )


class Question(BaseModel):
    """One of the plan understanding quiz's questions, with its four options."""

    model_config = {"extra": "forbid"}

    question: str = Field(description="The question itself.")
    options: list[str] = Field(
        description="Exactly four options, in order: A, B, C and D."
    )
    answer: Literal["A", "B", "C", "D"] = Field(description="Which one is correct.")


class Quiz(BaseModel):
    """The plan understanding quiz, which is advisory and never a gate."""

    model_config = {"extra": "forbid"}

    questions: list[Question] = Field(
        description="Exactly two questions, in the order they are to be asked."
    )
    summary: str = Field(
        description="Two or three sentences on what the plan does and how, for a reader who "
        "showed gaps in understanding. The technical approach, not just the goal."
    )


class Idea(BaseModel):
    """Every flag `gen-idea` takes, under the name the plugin gives it."""

    model_config = {"frozen": True}

    n: int = Field(
        default=6, ge=2, le=10, description="--n: how many directions explore the idea"
    )
    output: str = Field(
        default="",
        description="--output: where the draft goes, blank for .humanize/ideas",
    )


class Plan(BaseModel):
    """Every flag `gen-plan` takes, under the name the plugin gives it.

    `--input` is a field here where the three phases were one flow it was not: the draft is
    what `gen-idea` left behind, and naming it is how a plan is written from a draft somebody
    read and edited first.
    """

    model_config = {"frozen": True}

    input: str = Field(
        default="",
        description="--input: the draft to plan from, blank for the last one written",
    )
    output: str = Field(
        default="", description="--output: where the plan goes, blank for docs/plan.md"
    )
    mode: Literal["discussion", "direct"] = Field(
        default="discussion",
        description="--discussion or --direct: converge, or write it once",
    )
    auto_start_rlcr_if_converged: bool = Field(
        default=False,
        description="--auto-start-rlcr-if-converged: no review gate once converged",
    )
    alternative_plan_language: str = Field(
        default="",
        description="a translated plan too: zh, ko, ja, es, fr, de, pt, ru, ar",
    )
    turn_timeout: float = Field(
        default=3600,
        ge=0,
        description="seconds any one planning turn may take, zero for no per-turn limit",
    )
    total_timeout: float = Field(
        default=14400,
        ge=0,
        description="seconds the whole planning flow may take, zero for no overall limit",
    )
    turn_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description="how many times a failed or empty turn is retried",
    )


class Rlcr(BaseModel):
    """Every flag the loop takes, under the name the plugin gives it.

    What the plugin reads from `.humanize/config.json` is here too, since a config file and a
    flag are the same setting arrived at two ways -- and this is the one way.

    What the plugin says with a model name is said here by choosing an agent: `codex_model`,
    `codex_effort`, `bitlesson_model` and `provider_mode` are all "which model does this
    half", which is `/agents`. `--allow-empty-bitlesson-none` and
    `--require-bitlesson-entry-for-none` are one switch written twice.
    """

    model_config = {"frozen": True}

    plan_file: str = Field(
        default="",
        description="--plan-file: the plan to build, blank for docs/plan.md",
    )
    max: int = Field(
        default=42, ge=0, description="--max: rounds before the loop stops"
    )
    codex_timeout: int = Field(
        default=5400, ge=0, description="--codex-timeout: seconds one review may take"
    )
    full_review_round: int = Field(
        default=5,
        ge=2,
        description="--full-review-round: rounds between alignment checks",
    )
    base_branch: str = Field(
        default="", description="--base-branch: what the code review reads against"
    )
    track_plan_file: bool = Field(
        default=False,
        description="--track-plan-file: the plan is in git and stays clean",
    )
    push_every_round: bool = Field(
        default=False, description="--push-every-round: push after every round"
    )
    skip_impl: bool = Field(
        default=False,
        description="--skip-impl: no building, straight to the code review",
    )
    claude_answer_codex: bool = Field(
        default=False,
        description="--claude-answer-codex: the builder answers open questions",
    )
    agent_teams: bool = Field(
        default=False,
        description="--agent-teams: the builder leads a team instead of coding",
    )
    skip_quiz: bool = Field(
        default=False, description="--skip-quiz: do not check you have read the plan"
    )
    yolo: bool = Field(
        default=False,
        description="--yolo: --skip-quiz and --claude-answer-codex together",
    )
    privacy: bool = Field(
        default=False,
        description="--privacy: no methodology analysis when the loop exits",
    )
    require_bitlesson_entry_for_none: bool = Field(
        default=False,
        description="--require-bitlesson-entry-for-none: a round records a lesson",
    )

    @model_validator(mode="after")
    def _settles(self) -> Rlcr:
        """Turns the alias into what it aliases.

        Returns:
          The config, with `--yolo` spelled out as the two flags it is a name for.
        """
        if self.yolo:
            object.__setattr__(self, "skip_quiz", True)
            object.__setattr__(self, "claude_answer_codex", True)
        return self


def _language(said: str) -> tuple[str, str]:
    """The language a translated plan would be written in, and its code.

    Args:
      said: What the config asked for, by name or by code, in any case.

    Returns:
      The language and its code, or two empty strings -- for nothing asked for, for English,
      and for anything the plugin's table does not hold, which it warns about and disables.
    """
    wanted = said.strip().lower()
    if not wanted or wanted in ("english", "en"):
        return "", ""
    for named, code in LANGUAGES.items():
        if wanted in (named, code):
            return named.capitalize(), code
    print(
        f'Warning: unsupported alternative_plan_language "{said}". Supported values: '
        + ", ".join(f"{one.capitalize()} ({code})" for one, code in LANGUAGES.items())
        + ". Translation variant will not be generated."
    )
    return "", ""


def _slug(task: str) -> str:
    """A short name for an idea, as `validate-gen-idea-io.sh` makes one.

    Args:
      task: The idea.

    Returns:
      Its first few words, lowercased, joined with dashes.
    """
    words = re.findall(r"[a-z0-9]+", task.lower())[:6]
    return "-".join(words) or "idea"


def _stamp() -> str:
    """Now, as the plugin stamps a file name."""
    import datetime

    return datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _head(root: Path) -> str:
    """The branch the work is on, or "" outside a git repository.

    Args:
      root: The workspace.

    Returns:
      The branch.
    """
    status, branch = git("rev-parse", "--abbrev-ref", "HEAD", at=root)
    return "" if status else branch


def _base(root: Path, asked: str) -> str:
    """What the code review reads the work against, as the setup script resolves it.

    Args:
      root: The workspace.
      asked: What the config said, or "" to work it out.

    Returns:
      The branch: what was asked for, else the remote's default, else `main`, else `master`,
      and "" where this repository has none of them -- which is a run without a code review.
    """
    if asked:
        return asked
    status, said = git("symbolic-ref", "refs/remotes/origin/HEAD", at=root)
    if not status and said:
        remote = said.rsplit("/", 1)[-1]
        if not git("show-ref", "--verify", "--quiet", f"refs/heads/{remote}", at=root)[
            0
        ]:
            return remote
    for named in ("main", "master"):
        if not git("show-ref", "--verify", "--quiet", f"refs/heads/{named}", at=root)[
            0
        ]:
            return named
    return ""


def _section(held: str, *headings: str) -> str:
    """One section of a plan, by any of the headings it might be under.

    Args:
      held: The plan.
      headings: The words the heading might start with, lowercased.

    Returns:
      What is under the first one that is there, or "" if none of them is.
    """
    lines = held.splitlines()
    for at, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        named = line[3:].strip().lower()
        if not any(named.startswith(one) for one in headings):
            continue
        found: list[str] = []
        for under in lines[at + 1 :]:
            if under.startswith("## "):
                break
            found.append(under)
        return "\n".join(found).strip()
    return ""


def _asked(human: Person, question: str, options: list[str]) -> str:
    """Puts one multiple-choice question to whoever is at the prompt.

    Asked as a question with options rather than as a paragraph with a list in it: it is the
    road a coding agent's own question takes, so whatever is driving the flow shows it as one
    -- and the options themselves are what the person picks between, the letters being how
    the quiz was written down rather than something to make them read off a list.

    Args:
      human: The person, driven as an agent.
      question: What to ask.
      options: What they may answer, in order.

    Returns:
      The letter they picked, uppercased, or "" where nobody was there to pick one -- which
      is a command line, where the quiz is advisory and the run carries on.
    """
    listed = list(zip("ABCD", options, strict=False))
    said = human.asked(
        Asking(
            text=question,
            options=tuple(f"{letter}. {one}" for letter, one in listed),
        )
    )
    if not said:
        return ""
    # Whichever way they answered: the option itself, which is what an interface offers, or
    # the letter, which is what somebody reading the quiz as it was written would type.
    for letter, one in listed:
        if said.strip() in (one, f"{letter}. {one}"):
            return letter
    return said.strip()[:1].upper()


class _DeadlineError(TimeoutError):
    """A planning turn that did not finish inside its wall-clock budget."""

    def __init__(self, message: str, done: threading.Event) -> None:
        super().__init__(message)
        self.done = done


class _TurnError(RuntimeError):
    """A bounded planning stage which could not produce a usable answer."""

    def __init__(
        self,
        stage: str,
        why: str,
        *,
        timed_out: bool = False,
        done: threading.Event | None = None,
    ) -> None:
        super().__init__(f"{stage}: {why}")
        self.stage = stage
        self.timed_out = timed_out
        self.done = done


class _EmptyTurnError(ValueError):
    """A turn that landed without answering what the flow asked."""


def _within[T](owner: Agent, call: Callable[[], T], seconds: float, stage: str) -> T:
    """Runs one backend-neutral turn, stopping its agent when its deadline passes.

    ``Agent.stop`` is part of the flow-facing contract and ends the flow's wait whatever backend
    is behind it. A timed-out role is therefore not retried in this run; planner writes happen
    on staging files so a command which takes longer to unwind cannot corrupt the durable plan.
    """
    if seconds <= 0:
        return call()

    landed: list[tuple[bool, object]] = []
    done = threading.Event()

    def run() -> None:
        try:
            landed.append((True, call()))
        except BaseException as why:  # noqa: BLE001 -- carried back to the flow's thread
            landed.append((False, why))
        finally:
            done.set()

    worker = threading.Thread(
        target=run,
        name=f"humanize1-{owner.id}-{stage}",
        daemon=True,
    )
    worker.start()
    if not done.wait(seconds):
        owner.stop()
        worker.join(timeout=_STOP_GRACE)
        raise _DeadlineError(f"took longer than {seconds:g}s", done)

    succeeded, answer = landed[0]
    if succeeded:
        return cast("T", answer)
    raise cast("BaseException", answer)


def _turn_limit(config: Plan, began: float, stage: str) -> float:
    """The smaller of this turn's limit and what remains of the whole plan budget."""
    limits = [config.turn_timeout] if config.turn_timeout > 0 else []
    if config.total_timeout > 0:
        remaining = config.total_timeout - (time.monotonic() - began)
        if remaining <= 0:
            raise _TurnError(
                stage,
                f"the {config.total_timeout:g}s total planning budget was exhausted",
                timed_out=True,
            )
        limits.append(remaining)
    return min(limits) if limits else 0


def _take(
    owner: Agent,
    target: Agent | Session,
    prompt: str,
    config: Plan,
    began: float,
    stage: str,
    *,
    schema: type[BaseModel] | None = None,
) -> Any:
    """Takes one planning turn with bounded retries and a real wall-clock deadline."""
    attempts = config.turn_retries + 1
    for attempt in range(1, attempts + 1):
        try:
            limit = _turn_limit(config, began, stage)

            def call() -> Any:
                answer = (
                    target(prompt, suppress=True, schema=schema)
                    if schema is not None
                    else target(prompt, suppress=True)
                )
                if answer is None or not str(answer).strip():
                    raise _EmptyTurnError("the turn returned an empty answer")
                return answer

            return _within(owner, call, limit, stage)
        except Stopped:
            raise
        except _DeadlineError as why:
            raise _TurnError(stage, str(why), timed_out=True, done=why.done) from why
        except Unrecoverable as why:
            raise _TurnError(stage, str(why)) from why
        except (subprocess.CalledProcessError, ValueError) as why:
            if attempt == attempts:
                raise _TurnError(stage, str(why)) from why
            print(f"Warning: {stage} failed; retrying ({attempt} of {attempts}): {why}")
    raise AssertionError("a positive number of planning attempts took no turn")


def _candidate_text(plan: str) -> str:
    """The structured candidate, without the immutable draft appendix."""
    return plan.partition("\n--- Original Design Draft Start ---\n")[0].rstrip()


def _material_digest(plan: str) -> str:
    """A digest of implementation content, excluding deliberation-only changes."""
    candidate = _candidate_text(plan)
    endings = [
        candidate.find(heading)
        for heading in (
            "\n## Planner-Reviewer Deliberation",
            "\n## Claude-Codex Deliberation",
            "\n## Pending User Decisions",
        )
        if candidate.find(heading) >= 0
    ]
    material = candidate[: min(endings)] if endings else candidate
    return hashlib.sha256(material.encode()).hexdigest()


def _partial(where: Path, why: str) -> None:
    """Marks the last durable candidate partial when no agent can finish it."""
    held = where.read_text(encoding="utf-8")
    status = "- Final Status: `partially_converged`"
    held, changed = re.subn(r"(?m)^- Final Status:.*$", status, held, count=1)
    note = f"- Flow Note: {' '.join(why.split())}"
    if changed:
        held = held.replace(status, f"{status}\n{note}", 1)
    else:
        section = (
            "## Planner-Reviewer Deliberation\n\n### Convergence Status\n"
            f"{status}\n{note}\n\n"
        )
        marker = "## Pending User Decisions"
        held = (
            held.replace(marker, section + marker, 1)
            if marker in held
            else section + held
        )
    where.write_text(held, encoding="utf-8")


def _stage(where: Path) -> Path:
    """Copies a durable plan to the hidden file one writing turn is allowed to mutate."""
    staged = where.with_name(f".humanize-plan-{uuid.uuid4().hex}.tmp")
    shutil.copyfile(where, staged)
    return staged


def _abandon(staged: Path, why: _TurnError, owner: Agent) -> None:
    """Removes a finished failed attempt, retaining one a timed-out command may still hold."""
    if not why.timed_out or not owner.stopped or why.done is None:
        staged.unlink(missing_ok=True)
        return

    def remove_after_turn() -> None:
        why.done.wait()
        staged.unlink(missing_ok=True)

    threading.Thread(
        target=remove_after_turn,
        name=f"humanize1-cleanup-{staged.name}",
        daemon=True,
    ).start()


def _promote(staged: Path, where: Path) -> None:
    """Atomically makes a successfully written staging plan the durable candidate."""
    staged.replace(where)


def _idea(drafting: Session, task: str, config: Idea, root: Path) -> Path:
    """`gen-idea`: opens the idea from N directions at once and closes it to one.

    Args:
      drafting: The session the drafter opens the idea in.
      task: The idea, as it was given.
      config: How this run was set up.
      root: The workspace.

    Returns:
      The draft the builder wrote.

    Raises:
      ValueError: If the draft cannot be written where it was asked for, which is what the
        plugin's IO validation exits on before anything runs.
    """
    where = Path(config.output or f"{IDEAS}/{_slug(task)}-{_stamp()}.md")
    if not where.is_absolute():
        where = root / where
    if where.exists():
        raise ValueError(
            f"{where}: output file already exists - choose a different path"
        )
    where.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(where.parent, os.W_OK):
        raise ValueError(f"{where.parent}: no write permission to output directory")
    spoken(
        drafting,
        render(
            planning.GEN_IDEA,
            N=config.n,
            OUTPUT_FILE=where,
            TEMPLATE=planning.GEN_IDEA_TEMPLATE,
            IDEA_BODY=task,
        ),
    )
    return where


def _plan(
    agents: Planning,
    writing: Session,
    task: str,
    config: Plan,
    root: Path,
    draft: Path,
) -> Path:
    """`gen-plan`: the reviewer reads first, the builder writes, and the two converge.

    Args:
      agents: The agents the flow drives.
      writing: The session the planner holds for the whole of the planning.
      task: What was asked for.
      config: How this run was set up.
      root: The workspace.
      draft: What the plan is written from.

    Returns:
      The plan.

    Raises:
      ValueError: If the draft is not there, is empty, does not belong to this repository, or
        the plan cannot be written where it was asked for.
    """
    began = time.monotonic()
    if not draft.is_file():
        raise ValueError(f"{draft}: input file not found")
    held = draft.read_text(encoding="utf-8")
    if not held.strip():
        raise ValueError(f"{draft}: input file is empty")
    where = Path(config.output or PLAN)
    if not where.is_absolute():
        where = root / where
    if where.exists():
        raise ValueError(
            f"{where}: output file already exists - please choose another path"
        )
    # Made rather than demanded, as the phase before this one makes the directory it writes
    # its draft into: a plan is what this phase is for, and a repository with no `docs/` in
    # it yet is not a reason to refuse to write one.
    where.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(where.parent, os.W_OK):
        raise ValueError(f"{where.parent}: no write permission to output directory")

    try:
        read = cast(
            "Relevance",
            _take(
                agents.analyst,
                agents.analyst,
                render(planning.RELEVANCE, INPUT_FILE=draft, DRAFT_CONTENT=held),
                config,
                began,
                "draft relevance check",
                schema=Relevance,
            ),
        )
    except _TurnError as why:
        template = (
            planning.GEN_PLAN_TEMPLATE
            + "\n--- Original Design Draft Start ---\n\n"
            + held
            + "\n--- Original Design Draft End ---\n"
        )
        where.write_text(template, encoding="utf-8")
        _partial(where, str(why))
        print(
            f"Warning: {why}; returning the template and original draft as a partial plan."
        )
        return where
    if not read.relevant:
        raise ValueError(
            f"the draft does not appear to be related to this repository: {read.why}"
        )

    # The plan file starts as the template with the draft under it, which is what the plugin
    # copies into place before the builder writes a word: the draft is the human input, and
    # it stays in the file rather than being read once and paraphrased away.
    template = (
        planning.GEN_PLAN_TEMPLATE
        + "\n--- Original Design Draft Start ---\n\n"
        + held
        + "\n--- Original Design Draft End ---\n"
    )
    where.write_text(template, encoding="utf-8")
    draft_suffix = (
        "\n--- Original Design Draft Start ---\n\n"
        + held
        + "\n--- Original Design Draft End ---\n"
    )
    limitations: list[str] = []

    try:
        analysis = cast(
            "str",
            _take(
                agents.analyst,
                agents.analyst,
                render(
                    planning.GEN_PLAN_ANALYSIS,
                    INPUT_FILE=draft,
                    DRAFT_CONTENT=held,
                ),
                config,
                began,
                "independent planning analysis",
            ),
        )
    except _TurnError as why:
        limitations.append(str(why))
        analysis = (
            "CORE_RISKS:\n- Independent analysis was unavailable; the planner must identify "
            "risks directly.\n\nMISSING_REQUIREMENTS:\n- Determine from the draft and repository."
            "\n\nTECHNICAL_GAPS:\n- Determine from the draft and repository.\n\n"
            "ALTERNATIVE_DIRECTIONS:\n- Compare alternatives only where the draft leaves a "
            "choice.\n\nQUESTIONS_FOR_USER:\n- Preserve genuine open decisions in the plan."
            "\n\nCANDIDATE_CRITERIA:\n- Derive testable criteria from repository evidence."
        )
        print(f"Warning: {why}; continuing with planner-only candidate generation.")

    before_candidate = where.read_text(encoding="utf-8")
    staged = _stage(where)
    try:
        _take(
            agents.planner,
            writing,
            render(
                planning.GEN_PLAN_CANDIDATE,
                OUTPUT_FILE=staged,
                ANALYSIS=analysis,
            ),
            config,
            began,
            "candidate plan",
        )
    except _TurnError as why:
        _abandon(staged, why, agents.planner)
        _partial(where, str(why))
        print(
            f"Warning: {why}; returning the template and original draft as a partial plan."
        )
        return where
    candidate = staged.read_text(encoding="utf-8")
    if _candidate_text(candidate) == _candidate_text(before_candidate):
        staged.unlink(missing_ok=True)
        raise RuntimeError(
            "gen-plan's planner returned without writing the candidate plan"
        )
    if not candidate.endswith(draft_suffix):
        staged.unlink(missing_ok=True)
        raise RuntimeError("gen-plan's planner did not preserve the original draft")
    _promote(staged, where)

    converged = False
    prior = ""
    unchanged = 0
    material = _material_digest(candidate)
    if config.mode == "discussion" and not agents.analyst.stopped:
        for round_number in range(1, CONVERGING + 1):
            current = where.read_text(encoding="utf-8")
            try:
                round_ = cast(
                    "Convergence",
                    _take(
                        agents.analyst,
                        agents.analyst,
                        render(
                            planning.GEN_PLAN_CONVERGENCE,
                            OUTPUT_FILE=where,
                            TASK=task,
                            PRIOR=prior,
                            ROUND=round_number,
                            TOTAL_ROUNDS=CONVERGING,
                            PLAN_CONTENT=_candidate_text(current),
                        ),
                        config,
                        began,
                        f"reasonability review {round_number}",
                        schema=Convergence,
                    ),
                )
            except _TurnError as why:
                limitations.append(str(why))
                print(
                    f"Warning: {why}; finishing the last candidate as partially converged."
                )
                break
            review = round_.rendered()
            if round_.settled:
                converged = True
                break
            prior = f"What was still open after the last round:\n\n{review}\n"
            staged = _stage(where)
            try:
                _take(
                    agents.planner,
                    writing,
                    render(
                        planning.GEN_PLAN_REVISION,
                        OUTPUT_FILE=staged,
                        REVIEW=review,
                    ),
                    config,
                    began,
                    f"plan revision {round_number}",
                )
            except _TurnError as why:
                _abandon(staged, why, agents.planner)
                limitations.append(str(why))
                print(f"Warning: {why}; keeping the previous candidate.")
                break
            revised = staged.read_text(encoding="utf-8")
            if not revised.endswith(draft_suffix):
                staged.unlink(missing_ok=True)
                limitations.append(
                    f"plan revision {round_number}: the original draft was not preserved"
                )
                break
            _promote(staged, where)
            changed = _material_digest(revised)
            unchanged = unchanged + 1 if changed == material else 0
            material = changed
            if unchanged >= _NO_MATERIAL_ROUNDS:
                limitations.append(
                    "convergence stopped after two consecutive revisions made no material "
                    "plan changes"
                )
                break

    # `--auto-start-rlcr-if-converged` is the one thing that skips the person: it is only
    # ever satisfied in discussion mode, with the plan converged and nothing left to decide.
    reviewing = not (
        config.auto_start_rlcr_if_converged
        and converged
        and config.mode == "discussion"
    )
    status = "converged" if converged else "partially_converged"
    if agents.planner.stopped:
        _partial(where, limitations[-1] if limitations else "the planner timed out")
        return where
    staged = _stage(where)
    try:
        _take(
            agents.planner,
            writing,
            render(
                planning.GEN_PLAN_FINAL,
                OUTPUT_FILE=staged,
                CONVERGENCE_STATUS=status,
                DECISIONS=(
                    "\nPut every remaining `PENDING` decision to the person through the "
                    "user-question facility available to your backend, and record what they "
                    "decide in place of the `PENDING` status. If no person is available, keep "
                    "the item explicitly `PENDING` rather than waiting. Confirm every "
                    "quantitative metric the draft states too: whether it is a hard requirement "
                    "or a direction to move in, which changes how the acceptance criteria are "
                    "written.\n"
                    if reviewing
                    else ""
                ),
                PLANNING_NOTES=(
                    "\nPlanning limitations to record without expanding them into new scope:\n- "
                    + "\n- ".join(limitations)
                    + "\n"
                    if limitations
                    else ""
                ),
            ),
            config,
            began,
            "final plan consolidation",
        )
    except _TurnError as why:
        _abandon(staged, why, agents.planner)
        limitations.append(str(why))
        _partial(where, str(why))
        print(f"Warning: {why}; returning the last durable candidate.")
        return where
    finished = staged.read_text(encoding="utf-8")
    if not finished.endswith(draft_suffix):
        staged.unlink(missing_ok=True)
        _partial(where, "final consolidation did not preserve the original draft")
        return where
    finished = re.sub(
        r"(?m)^- Final Status:.*$",
        f"- Final Status: `{status}`",
        finished,
        count=1,
    )
    staged.write_text(finished, encoding="utf-8")
    _promote(staged, where)

    language, code = _language(config.alternative_plan_language)
    if language:
        variant = where.with_name(f"{where.stem}_{code}{where.suffix}")
        staged = variant.with_name(f".humanize-plan-{uuid.uuid4().hex}.tmp")
        try:
            _take(
                agents.planner,
                writing,
                render(
                    planning.GEN_PLAN_TRANSLATE,
                    OUTPUT_FILE=where,
                    LANGUAGE=language,
                    VARIANT_FILE=staged,
                ),
                config,
                began,
                f"{language} plan translation",
            )
        except _TurnError as why:
            _abandon(staged, why, agents.planner)
            print(
                f"Warning: {why}; the main plan is complete but no translation was kept."
            )
        else:
            staged.replace(variant)
    return where


def _rlcr(
    agents: Building,
    building: Session,
    config: Rlcr,
    root: Path,
    plan: Path | None,
    kept: dict[str, Any],
) -> None:
    """`start-rlcr-loop`: the plan is built under review until nothing is left to say.

    Args:
      agents: The agents the flow drives.
      building: The session the builder holds for the whole of the loop.
      config: How this run was set up.
      root: The workspace.
      plan: The plan to build, or None for a `--skip-impl` run that has none.
      kept: What the last run of this flow here left behind, and what this one leaves.

    Raises:
      ValueError: If the loop cannot start: not a git repository, no plan where one is
        needed, a plan that is not this repository's, or one that would move the branch.
    """
    if _head(root) == "":
        raise ValueError(
            "rlcr runs in a git repository: every review reads the work since the commit "
            "the plan was fixed in"
        )
    # What this run was set up with, checked before anything is set up or picked up -- and
    # what the loop that runs will actually be running on, either way: a fresh loop is set
    # up from this config, and a loop carried on is one whose own state says the same, since
    # `_again` refuses to carry a loop on into a run that was set up differently.
    if (
        config.agent_teams
        and os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") != "1"
    ):
        raise ValueError(
            "agent_teams requires the CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS environment "
            "variable to be set:\n\n  export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"
        )
    if config.push_every_round and not git("remote", at=root)[1]:
        raise ValueError(
            "push_every_round needs a remote to push to, and this repository has none"
        )
    # The loop the last run was in, where there is still one to carry on: a loop is set up
    # once, and setting another up beside it is what leaves a week of rounds orphaned.
    carrying = _again(agents.reviewer, config, root, plan, kept)
    running, told = (
        carrying if carrying is not None else _fresh(agents, config, root, plan, kept)
    )
    with (
        agents.builder.hooks.on(Moment.STOP, running),
        agents.builder.hooks.on(Moment.PERMISSION_REQUEST, guards.Guard(running, root)),
        agents.builder.hooks.on(
            Moment.USER_PROMPT_SUBMIT, guards.Prompted(running, root)
        ),
    ):
        spoken(building, told)


def _again(
    reviewer: Agent,
    config: Rlcr,
    root: Path,
    plan: Path | None,
    kept: dict[str, Any],
) -> tuple[Loop, str] | None:
    """The loop the last run of this flow left going, and what it last told the builder.

    The loop is read back off its own live state file and nothing in it is worked out again
    from the repository. The commit the plan was fixed in, the branch the work is on and the
    plan itself are what every round of the loop has been judged against, and a run that
    settled them afresh would move the anchor to wherever the repository has got to since --
    which is a loop carried on in name only.

    Which is also why this run's config is compared against it rather than laid over it: what
    the loop was set up with is what the rounds behind it were judged by. A run that says
    something else is a run asking for a different loop, and gets one.

    Args:
      reviewer: Who reads each round, which is this run's reviewer rather than the last
        run's: the loop is what carries on, and the agents are whoever was chosen this time.
      config: How this run was set up, which the loop has to have been set up the same way.
      root: The workspace, which is what the directory was written down against.
      plan: The plan this run was pointed at, or None for a run that named none.
      kept: What the last run left behind.

    Returns:
      The loop and what to start the builder on, or None where there is nothing here to
      carry on -- a first run, a loop that has ended, a directory that has gone, a state file
      this version of the flow cannot read, a phase the loop wrote no prompt for, a
      repository that has moved out from under it, or a run set up differently from it. Each
      of those is a fresh loop, said out loud where whoever expected the old one to go on can
      read why it is not.
    """
    said = str(kept.get("loop") or "")
    if not said:
        return None
    where = _under(root, said)
    running = Loop.picked_up(reviewer, where, root, kept=kept)
    if running is None:
        print(
            f"{where}: no live state file to carry on from -- that loop has ended, or was "
            "written by another version of this flow. Starting a fresh loop."
        )
        return None
    if moved := _moved(running):
        print(f"{where}: {moved}. Starting a fresh loop.")
        return None
    if differs := _differs(running, config, plan):
        print(f"{where}: {differs}. Starting a fresh loop.")
        return None
    # The builder is a session that has just been opened -- no backend reopens the one that
    # heard the round the first time -- so what it is told is what the loop wrote down for
    # the phase it is in, which is a prompt that says where everything it needs is.
    told = (
        running.prompt.read_text(encoding="utf-8") if running.prompt.is_file() else ""
    )
    if not told.strip():
        print(
            f"{running.prompt}: nothing was written down for where that loop is, so there "
            "is nothing to send a builder back in with. Starting a fresh loop."
        )
        return None
    # Who is reading the rounds from here on. The agents are this run's, so the state file
    # says the reviewer that is actually reading them rather than the one that read the last
    # run's: it is what `humanize monitor rlcr` shows of a loop, and it would otherwise name
    # a model nothing in this run is running.
    running.state.codex_model = reviewer.config.model
    running.state.codex_effort = reviewer.config.effort
    running.state_file.write_text(running.state.written(), encoding="utf-8")
    print(f"Carrying on the loop in {where}, {_where_it_is(running)}.")
    return running, told


def _where_it_is(running: Loop) -> str:
    """Where in the loop a run is picking it up, as a phrase to say out loud.

    Args:
      running: The loop as it was read back.

    Returns:
      The phase, or the round for a loop still building.
    """
    if running.analysing:
        return "in the methodology analysis it is exiting through"
    if running.finalizing:
        return "in the finalize round"
    return f"at round {running.state.current_round}"


def _moved(running: Loop) -> str:
    """What one loop is anchored to that has moved since, or "" for a repository that fits it.

    Two of the things a round is judged against live outside the loop's own directory: the
    branch the work is on, and the plan being built -- which has to be where it was, as it
    was, and in or out of git as the loop was told. Either of them having moved is a loop
    whose every turn its own guards would now refuse, and the plugin's own answer to a plan
    that has to change is to stop, change it and start again -- which has always meant a loop
    of its own rather than this one spending a run refusing itself.

    How much of the plan still matters depends on where the loop is. In the code review the
    plan is out of it and only its being there at all is read; while the implementation is
    going every round is judged against it, so where it is, what is in it and whether git
    holds it are all read.

    Args:
      running: The loop as it was read back.

    Returns:
      What has moved, in a clause, or "" for a loop the repository still fits.
    """
    state, root = running.state, running.root
    branch = _head(root)
    if state.start_branch and branch != state.start_branch:
        return f"that loop is building on {state.start_branch}, and this is on {branch}"
    plan, backup = root / state.plan_file, running.where / "plan.md"
    # A plan that is gone is the one thing no phase excuses. The loop's own prompt guard
    # reads the plan in every phase, so a run carried on without it is a run whose first
    # turn is refused before it starts -- which is a run that does nothing and says nothing.
    # Answered here instead: this loop cannot be carried on, and the setup that follows says
    # there is no plan to build, which is the truth said out loud.
    if not plan.is_file():
        return f"the plan that loop is building is not at {plan} any more"
    # Past the implementation phase the rest of the plan is out of it: the code review reads
    # the repository itself, and the gate that holds the plan still is skipped there too. So
    # is everything below, for that gate's own reason -- a plan that has changed, joined git
    # or left it since is nothing to throw a loop away over once no round is judged by it.
    if state.review_started:
        return ""
    if state.plan_file:
        tracked = git("ls-files", "--error-unmatch", state.plan_file, at=root)[0] == 0
        if tracked is not state.plan_tracked:
            return (
                f"{state.plan_file} is {'now' if tracked else 'no longer'} tracked in git, "
                "which is not how that loop was set up"
            )
    if not backup.is_file():
        return f"that loop's own copy of {state.plan_file} is not in {running.where} any more"
    if plan.read_bytes() != backup.read_bytes():
        return f"{plan} has changed since that loop was set up"
    return ""


def _differs(running: Loop, config: Rlcr, plan: Path | None) -> str:
    """What this run was set up with that the loop was not, or "" for a run that fits it.

    A loop keeps what it was set up with in its own state file, and every round behind it was
    run under exactly that: refused for the plan it was given, sent back at the round `max`
    called the last one, reviewed against the branch that was named. A run that says
    something else about any of it is not a run this loop carries on into -- the settings
    would have to be ignored, which is a `-c` nobody read, or taken up halfway, which is a
    loop whose rounds were not all run by the same rules.

    Three are not here. The agents are nobody's config field: whoever was chosen this run
    reads the rounds from here on. `skip_quiz` says whether a loop is set up with a quiz on
    the plan, which happens once, when it is set up -- a loop already running is past the
    question, and a run that answers it differently would be carrying on into nothing. And a
    run that says `skip_impl` of a loop already in its code review is carried on: the run
    asks for the implementation to be skipped and the loop has finished it, which is two ways
    of saying where the work starts, and the rounds from here are the same rounds either way.

    The other direction is not that. A loop set up review-only is one whose state says the
    BitLesson entry is not required -- it is set from `skip_impl` when a loop is set up and
    never again -- and a run that means to build the plan would spend itself on a loop that
    never will.

    Args:
      running: The loop as it was read back.
      config: How this run was set up.
      plan: The plan this run was pointed at, or None for a run that named none.

    Returns:
      What disagrees, in a clause, or "" for a loop this run would have set up the same way.
    """
    state, root = running.state, running.root
    if plan is not None and (named := _named(root, plan)) != state.plan_file:
        return f"that loop is building {state.plan_file}, and this run says {named}"
    if config.skip_impl and not state.review_started:
        return "that loop is building a plan, and this run says skip_impl"
    if not config.skip_impl and not state.bitlesson_required:
        return (
            "that loop was set up with skip_impl, and this run says it builds the plan"
        )
    if config.base_branch and config.base_branch != state.base_branch:
        return (
            f"that loop is reviewing against {state.base_branch or 'nothing'}, and this "
            f"run says {config.base_branch}"
        )
    settings: tuple[tuple[str, object, object], ...] = (
        ("max", state.max_iterations, config.max),
        ("codex_timeout", state.codex_timeout, config.codex_timeout),
        ("full_review_round", state.full_review_round, config.full_review_round),
        ("track_plan_file", state.plan_tracked, config.track_plan_file),
        ("push_every_round", state.push_every_round, config.push_every_round),
        ("agent_teams", state.agent_teams, config.agent_teams),
        (
            "claude_answer_codex",
            not state.ask_codex_question,
            config.claude_answer_codex,
        ),
        ("privacy", state.privacy_mode, config.privacy),
        (
            "require_bitlesson_entry_for_none",
            not state.bitlesson_allow_empty_none,
            config.require_bitlesson_entry_for_none,
        ),
    )
    for name, was, now in settings:
        if was != now:
            return (
                f"that loop was set up with {name} {_says(was)}, and this run says "
                f"{_says(now)}"
            )
    return ""


def _named(root: Path, plan: Path) -> str:
    """The plan, by the name a loop's state file calls it.

    Args:
      root: The workspace.
      plan: The plan.

    Returns:
      Where it is under the workspace, or the path itself for a plan kept outside one.
    """
    return str(plan.relative_to(root) if plan.is_relative_to(root) else plan)


def _says(value: object) -> str:
    """One setting, as a sentence about it says it.

    Args:
      value: What it is set to.

    Returns:
      A switch as on or off, which is what the flags these stand for are, and anything else
      as it stands.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def _fresh(
    agents: Building,
    config: Rlcr,
    root: Path,
    plan: Path | None,
    kept: dict[str, Any],
) -> tuple[Loop, str]:
    """Sets a loop up from nothing: the checks, the copy, the scaffolding and round zero.

    Args:
      agents: The agents the flow drives.
      config: How this run was set up.
      root: The workspace.
      plan: The plan to build, or None for a `--skip-impl` run that has none.
      kept: What this run leaves behind, which is where the new loop is written down.

    Returns:
      The loop and what to start the builder on.

    Raises:
      ValueError: If the loop cannot start: no plan where one is needed, a plan that is not
        this repository's, or one that would move the work to another branch.
    """
    held = ""
    if plan is not None:
        if not plan.is_file():
            raise ValueError(f"{plan}: no plan file to build")
        held = plan.read_text(encoding="utf-8")
        if len(held.splitlines()) < _ENOUGH:
            raise ValueError(f"{plan}: the plan file has almost nothing in it")

    # The plan is checked before anything is set up: a plan for another repository, or one
    # that would move the work to another branch, is one to say so about now.
    if plan is not None and not config.skip_impl:
        read = answered(
            agents.reviewer,
            render(prompts.PLAN_COMPLIANCE, PLAN_FILE=plan, PLAN_CONTENT=held),
            Compliance,
        )
        if not read.relevant:
            raise ValueError(f"the plan is not related to this repository: {read.why}")
        if read.switches_branch:
            raise ValueError(
                "the plan contains branch-switching instructions, which are incompatible "
                f"with RLCR: {read.why}"
            )

    if plan is not None and not (config.skip_quiz or config.skip_impl):
        _understood(agents, plan, held)

    stamp = loop.started()
    where = loop.directory(root, stamp)
    if plan is None:
        (where / "plan.md").write_text(
            "# Skip Implementation Mode\n\nThis RLCR loop was started with `skip_impl`, "
            "which skips the implementation phase and goes directly to code review.\n\n"
            "No implementation plan was provided - this is expected for skip-impl mode.\n",
            encoding="utf-8",
        )
        named = _named(root, where / "plan.md")
    else:
        shutil.copyfile(plan, where / "plan.md")
        named = _named(root, plan)

    base = _base(root, config.base_branch)
    commit = git("rev-parse", base, at=root)[1] if base else ""
    state = State(
        current_round=0,
        max_iterations=config.max,
        codex_model=agents.reviewer.config.model,
        codex_effort=agents.reviewer.config.effort,
        codex_timeout=config.codex_timeout,
        push_every_round=config.push_every_round,
        full_review_round=config.full_review_round,
        plan_file=named,
        plan_tracked=config.track_plan_file,
        start_branch=_head(root),
        base_branch=base,
        base_commit=commit,
        review_started=config.skip_impl,
        ask_codex_question=not config.claude_answer_codex,
        agent_teams=config.agent_teams,
        privacy_mode=config.privacy,
        # Skip-impl does not use the BitLesson-aware summary template, so enforcing it
        # would block a review-only run on a section nothing asked it to write.
        bitlesson_required=not config.skip_impl,
        bitlesson_allow_empty_none=not config.require_bitlesson_entry_for_none,
        mainline_stall_count=0,
        started_at=_utc(),
    )
    running = Loop(agents.reviewer, where, root, state, kept=kept)
    _set_up(running, config, plan, held)
    if config.skip_impl:
        (where / loop.REVIEW_STARTED).write_text(
            "build_finish_round=0\n", encoding="utf-8"
        )

    told = _round_zero(running, config, held)
    running.prompt.write_text(told, encoding="utf-8")
    # Written down only now, with the whole of the loop on disk behind it: a directory named
    # before it holds a state file and a prompt is a directory the next run cannot pick up
    # anyway, and would be one it read past to find that out.
    kept.update(loop=str(where.relative_to(root)), rounds=state.current_round)
    return running, told


#: How few lines a plan may have before it is not a plan, as the setup script counts them.
_ENOUGH = 5


def _utc() -> str:
    """Now, as the state file records it."""
    import datetime

    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _understood(agents: Building, plan: Path, held: str) -> None:
    """The plan understanding quiz, which is advisory and never a gate.

    Two questions about how the plan will be built, put to whoever is at the prompt. Getting
    one wrong is not refused: what it earns is the summary of what the plan actually does,
    and the choice to go on or to stop and read it.

    Every one of the three is asked as a question with options rather than said to the person
    as a turn, and that is what makes this a quiz a run nobody is at can pass through. A turn
    said to the person waits for them to type, and waits however long that takes -- which for
    a run left going overnight, or one told with `/afk` that nobody is here, is a loop meant
    to run for days stopped on its first minute by a multiple-choice question. A question is
    answered with nothing instead, and nothing is what this reads as go on: the quiz is
    advisory, and somebody who is away has not asked for the loop to stop.

    Args:
      agents: The agents the flow drives.
      plan: The plan.
      held: What it says.

    Raises:
      ValueError: If the person read the summary and chose to stop and review the plan, which
        is the one answer that ends the run and has to be given for it to.
    """
    # Advisory, so a turn that failed or would not answer in the shape asked for is a quiz
    # that is not run rather than one that is asked for again: the plugin warns and goes on.
    quiz = agents.reviewer(
        render(prompts.PLAN_UNDERSTANDING_QUIZ, PLAN_FILE=plan, PLAN_CONTENT=held),
        suppress=True,
        schema=Quiz,
    )
    if quiz is None or not quiz.questions:
        print("Plan understanding quiz unavailable, continuing without it.")
        return
    right = 0
    asked = 0
    for question in quiz.questions:
        picked = _asked(agents.human, question.question, question.options)
        if not picked:
            return  # nobody is at the prompt, so there is nobody to quiz
        asked += 1
        right += picked == question.answer
    if asked and right == asked:
        print("Your understanding of the plan looks solid. Proceeding with setup.")
        return
    going = _asked(
        agents.human,
        f"{quiz.summary}\n\nThe answers were "
        + ", ".join(
            f"Q{at + 1}: {question.answer}"
            for at, question in enumerate(quiz.questions)
        )
        + ".\n\nWould you like to proceed with the RLCR loop anyway, or stop and review "
        "the plan more carefully first?",
        ["Proceed with RLCR loop", "Stop and review the plan first"],
    )
    # Asked the way the questions were, so that a run nobody is at answers with nothing and
    # goes on: the quiz is advisory, and a person who is away has not asked for it to stop.
    if going == "B":
        raise ValueError(
            "stopping. Please review the plan file and run the flow again when ready"
        )


def _set_up(running: Loop, config: Rlcr, plan: Path | None, held: str) -> None:
    """Writes everything a loop starts with: the tracker, the contract, the lessons, the state.

    Args:
      running: The loop.
      config: How this run was set up.
      plan: The plan, or None for a review-only run.
      held: What the plan says.
    """
    lessons = running.root / running.state.bitlesson_file
    if not lessons.exists():
        lessons.parent.mkdir(parents=True, exist_ok=True)
        lessons.write_text(prompts.BITLESSON, encoding="utf-8")
    goal = _section(held, "goal", "objective", "overview")
    criteria = _section(held, "acceptance", "criteria", "requirements")
    if config.skip_impl and plan is not None:
        tracker = render(
            prompts.GOAL_TRACKER_SKIP_IMPL_ANCHORED,
            PLAN_GOAL_CONTENT=goal
            or f"Preserve the original plan scope from {running.state.plan_file} while "
            "resolving code review findings on the current branch.",
            PLAN_AC_CONTENT=criteria
            or f"- The current branch remains aligned with the original plan at "
            f"{running.state.plan_file}.\n- All blocking `[P0-9]` code review findings are "
            "resolved without widening scope beyond the original plan.\n- Non-blocking "
            "follow-up items are explicitly queued and do not block completion.",
            PLAN_FILE=running.state.plan_file,
        )
    elif config.skip_impl:
        tracker = prompts.GOAL_TRACKER_SKIP_IMPL
    else:
        tracker = render(
            prompts.GOAL_TRACKER,
            GOAL_SECTION=goal
            or "[To be extracted from plan by the builder in Round 0]\n\nSource plan: "
            + running.state.plan_file,
            AC_SECTION=criteria
            or "[To be defined by the builder in Round 0 based on the plan]",
        )
    running.tracker.write_text(tracker, encoding="utf-8")
    running.summary.write_text(
        render(prompts.SUMMARY_TEMPLATE, ROUND=0), encoding="utf-8"
    )
    if config.skip_impl:
        running.contract.write_text(
            render(
                prompts.ROUND_CONTRACT_SKIP_IMPL_ANCHORED,
                PLAN_FILE=running.state.plan_file,
            )
            if plan is not None
            else prompts.ROUND_CONTRACT_SKIP_IMPL,
            encoding="utf-8",
        )
    running.state_file.write_text(running.state.written(), encoding="utf-8")


def _round_zero(running: Loop, config: Rlcr, held: str) -> str:
    """The prompt the builder starts on, as the setup script writes it.

    Args:
      running: The loop.
      config: How this run was set up.
      held: What the plan says, which round 0 is given in full.

    Returns:
      The prompt.
    """
    if config.skip_impl:
        return render(
            prompts.ROUND_0_SKIP_IMPL,
            BASE_BRANCH=running.state.base_branch,
            START_BRANCH=running.state.start_branch,
            PLAN_FILE=running.state.plan_file,
            GOAL_TRACKER_FILE=running.tracker,
            ROUND_CONTRACT_FILE=running.contract,
            SUMMARY_FILE=running.summary,
            ANCHOR=render(
                prompts.ROUND_0_SKIP_IMPL_ANCHORED, PLAN_FILE=running.state.plan_file
            )
            if held
            else prompts.ROUND_0_SKIP_IMPL_UNANCHORED,
        )
    teams = ""
    if config.agent_teams:
        teams = (
            "\n" + prompts.AGENT_TEAMS_INSTRUCTIONS + "\n" + prompts.AGENT_TEAMS_CORE
        )
    told = render(
        prompts.ROUND_0,
        GOAL_TRACKER_FILE=running.tracker,
        ROUND_CONTRACT_FILE=running.contract,
        SUMMARY_FILE=running.summary,
        TASK_LANES=prompts.TASK_LANES,
        PLAN_CONTENT=held,
        BITLESSON_SELECTION=render(
            prompts.BITLESSON_SELECTION,
            BITLESSON_FILE=running.root / running.state.bitlesson_file,
        ),
        AGENT_TEAMS=teams,
    )
    if config.push_every_round:
        told += prompts.PUSH_EVERY_ROUND_NOTE
    return told


def _last(root: Path) -> Path:
    """The draft `gen-idea` wrote last, for a `gen-plan` that was not told which one.

    Args:
      root: The workspace.

    Returns:
      The most recently written draft under `.humanize/ideas`.

    Raises:
      ValueError: If there is none, which is a plan asked for before there was anything to
        plan from.
    """
    written = [one for one in (root / IDEAS).glob("*.md") if one.is_file()]
    if not written:
        raise ValueError(
            f"no draft to plan from under {IDEAS}: run gen-idea first, or set input to a "
            "draft you already have"
        )
    return max(written, key=lambda one: one.stat().st_mtime)


def _under(root: Path, said: str) -> Path:
    """One path a config named, against the workspace where it was named relatively."""
    where = Path(said)
    return where if where.is_absolute() else root / where


@flow(name="gen-idea")
def gen_idea(agents: Drafting, task: str, config: Idea | None = None) -> None:
    """Opens a loose idea into a repo-grounded draft.

    Args:
      agents: The drafter.
      task: The idea, as it was given.
      config: How the run was set up, or None for the plugin's own defaults.

    Raises:
      ValueError: If there is no idea to open, or the draft cannot be written where it was
        asked for. Said before the first turn rather than found hours into one.
    """
    if not task.strip():
        raise ValueError("gen-idea opens an idea, and this run was given none")
    _idea(agents.drafter.new(), task, config or Idea(), Path.cwd())


@flow(name="gen-plan")
def gen_plan(agents: Planning, task: str, config: Plan | None = None) -> None:
    """Turns a draft into a plan the writing and the reading side have converged on.

    Args:
      agents: The planner, and the analyst that reads what it writes.
      task: What was asked for, which the convergence rounds are judged against.
      config: How the run was set up, or None for the plugin's own defaults.

    Raises:
      ValueError: If there is no draft to plan from, or it is not this repository's, or the
        plan cannot be written where it was asked for.
    """
    setting = config or Plan()
    root = Path.cwd()
    draft = _under(root, setting.input) if setting.input else _last(root)
    # One session for the whole of the planning: the side that writes remembers how it got
    # there, and the next phase starts from the file rather than from the conversation.
    _plan(agents, agents.planner.new(), task, setting, root, draft)


@flow(name="rlcr", resumable=True)
def rlcr(
    agents: Building,
    task: str,  # noqa: ARG001
    config: Rlcr | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Builds the plan under review until nothing is left to say.

    Args:
      agents: The builder, the reviewer that reads its work, and whoever is at the prompt.
      task: What was asked for. The plan is what the loop runs on, so this is not put to an
        agent -- it is what the run is called wherever it is watched.
      config: How the run was set up, or None for the plugin's own defaults.
      state: What the last run of this flow here left behind -- the loop it was working in,
        which this run carries on in where it is still there to carry on, in whichever of
        its phases it stopped in. A first run is handed nothing and starts one; so is a run
        whose loop has ended or gone, and one set up differently from the loop it was handed.

    Raises:
      ValueError: If the loop cannot start: outside a git repository, no plan where one is
        needed, a plan that is not this repository's, or one that would move the branch.
    """
    setting = config or Rlcr()
    root = Path.cwd()
    # `--skip-impl` reviews the branch as it stands, so a run that named no plan has none
    # rather than the one that happens to be at `docs/plan.md`: naming it is what anchors a
    # review-only run to a plan. Every other run builds a plan, and blank means the usual one.
    plan = (
        _under(root, setting.plan_file)
        if setting.plan_file
        else None
        if setting.skip_impl
        else _under(root, PLAN)
    )
    # A run of a flow that is not being written down is handed nothing to write down in --
    # one called from a test, one called by a flow that opened no cycle -- and is a loop
    # that starts fresh and is not picked up, rather than a run that refuses to happen.
    _rlcr(
        agents,
        agents.builder.new(),
        setting,
        root,
        plan,
        state if state is not None else {},
    )
