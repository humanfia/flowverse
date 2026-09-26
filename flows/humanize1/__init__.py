"""RLCR (humanize 1) -- PolyArch/humanize as three flows, each set up before it starts."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from _humanize1 import guards, loop, planning, prompts
from _humanize1.loop import (
    PERMANENT,
    Here,
    Loop,
    State,
    TurnTimedOut,
    git,
    move,
    read,
    remove,
    timed,
    utc,
    write,
)
from _humanize1.prompts import render
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hmz.flows import (
    Agent,
    AgentCollection,
    EnvCollection,
    FlowParams,
    HarnessError,
    Outworlder,
    PermissionRequestHookAgentMixin,
    SessionError,
    flow,
)

if TYPE_CHECKING:
    from hmz.flows import FlowContext, FlowState, Session


class Builder(Agent, PermissionRequestHookAgentMixin): ...


class Drafting(AgentCollection):
    drafter: Agent


class Planning(AgentCollection):
    planner: Agent
    analyst: Agent


class Building(AgentCollection):
    builder: Builder
    reviewer: Agent
    human: Outworlder


class Where(EnvCollection):
    workspace: Here


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

CONVERGING = 3

_REVIEW_HEADINGS = (
    "AGREE",
    "DISAGREE",
    "REQUIRED_CHANGES",
    "OPTIONAL_IMPROVEMENTS",
    "UNRESOLVED",
)

_NO_MATERIAL_ROUNDS = 2

_ANSWERING = 3

IDEAS = ".humanize/ideas"

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
        sections = self._sections()
        if any(sections.values()):
            return not any(
                sections[name]
                for name in ("DISAGREE", "REQUIRED_CHANGES", "UNRESOLVED")
            )
        return self.converged and bool(
            re.search(r"\bAGREE\b", self.review, re.IGNORECASE)
        )

    def rendered(self) -> str:
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


class Choice(BaseModel):
    """What the person picked for one of the quiz's questions."""

    model_config = {"extra": "forbid"}

    choice: Literal["", "A", "B", "C", "D"] = Field(
        default="",
        description="The letter of the option picked, or blank to answer none of them.",
    )


class Idea(FlowParams):
    """Every flag `gen-idea` takes, under the name the plugin gives it."""

    model_config = ConfigDict(frozen=True)

    n: int = Field(
        default=6, ge=2, le=10, description="--n: how many directions explore the idea"
    )
    output: str = Field(
        default="",
        description="--output: where the draft goes, blank for .humanize/ideas",
    )


class Plan(FlowParams):
    """Every flag `gen-plan` takes, under the name the plugin gives it.

    `--input` is a field here where the three phases were one flow it was not: the draft is
    what `gen-idea` left behind, and naming it is how a plan is written from a draft somebody
    read and edited first.
    """

    model_config = ConfigDict(frozen=True)

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


class Rlcr(FlowParams):
    """Every flag the loop takes, under the name the plugin gives it.

    What the plugin reads from `.humanize/config.json` is here too, since a config file and a
    flag are the same setting arrived at two ways -- and this is the one way.

    What the plugin says with a model name is said here by choosing an agent: `codex_model`,
    `codex_effort`, `bitlesson_model` and `provider_mode` are all "which model does this
    half", which is `/agents`. `--allow-empty-bitlesson-none` and
    `--require-bitlesson-entry-for-none` are one switch written twice.
    """

    model_config = ConfigDict(frozen=True)

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
    skip_code_review: bool = Field(
        default=False,
        description=(
            "--skip-code-review: finish after implementation RLCR; do not start the "
            "final repository-wide code-review phase"
        ),
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
        if self.yolo:
            object.__setattr__(self, "skip_quiz", True)
            object.__setattr__(self, "claude_answer_codex", True)
        return self


_SHELL = 30

_ENOUGH = 5


def _language(said: str) -> tuple[str, str]:
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
    words = re.findall(r"[a-z0-9]+", task.lower())[:6]
    return "-".join(words) or "idea"


def _stamp() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _under(root: PurePosixPath, said: str) -> PurePosixPath:
    where = PurePosixPath(said)
    return where if where.is_absolute() else root / where


def _named(root: PurePosixPath, plan: PurePosixPath) -> str:
    return str(plan.relative_to(root) if plan.is_relative_to(root) else plan)


def _says(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


async def _head(env: Here) -> str:
    status, branch = await git(env, "rev-parse", "--abbrev-ref", "HEAD")
    return "" if status else branch


async def _base(env: Here, asked: str) -> str:
    if asked:
        return asked
    status, said = await git(env, "symbolic-ref", "refs/remotes/origin/HEAD")
    if not status and said:
        remote = said.rsplit("/", 1)[-1]
        if not (
            await git(env, "show-ref", "--verify", "--quiet", f"refs/heads/{remote}")
        )[0]:
            return remote
    for named in ("main", "master"):
        if not (
            await git(env, "show-ref", "--verify", "--quiet", f"refs/heads/{named}")
        )[0]:
            return named
    return ""


async def _review_base(env: Here, config: Rlcr) -> str:
    return "" if config.skip_code_review else await _base(env, config.base_branch)


async def _writable(env: Here, directory: PurePosixPath) -> None:
    status, _, err = await env.exec(
        ["mkdir", "-p", "--", str(directory)], timeout=_SHELL
    )
    if status:
        raise ValueError(f"{directory}: cannot create output directory: {err.strip()}")
    status, _, _ = await env.exec(["test", "-w", str(directory)], timeout=_SHELL)
    if status:
        raise ValueError(f"{directory}: no write permission to output directory")


async def _last(env: Here) -> PurePosixPath:
    status, said, _ = await env.exec(["ls", "-t", "--", IDEAS], timeout=_SHELL)
    written = [one for one in said.splitlines() if one.endswith(".md")]
    if status or not written:
        raise ValueError(
            f"no draft to plan from under {IDEAS}: run gen-idea first, or set input to a "
            "draft you already have"
        )
    return env.workdir / IDEAS / written[0]


def _section(held: str, *headings: str) -> str:
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


def _undecided(held: str) -> list[str]:
    found: list[str] = []
    named = ""
    for line in _section(held, "pending user decisions").splitlines():
        said = line.strip()
        if match := re.match(r"-\s*(DEC-\d+)", said):
            named = match.group(1)
        elif named and said.startswith("- Decision Status:") and "PENDING" in said:
            found.append(named)
            named = ""
    return found


async def _asked(
    human: Outworlder, session: Session, question: str, options: list[str]
) -> str:
    listed = "\n".join(
        f"{letter}. {one}" for letter, one in zip("ABCD", options, strict=False)
    )
    said = await human.run(
        f"{question}\n\n{listed}", session=session, output_schema=Choice
    )
    return said.choice


async def _answered[T: BaseModel](
    agent: Agent, env: Here, prompt: str, schema: type[T]
) -> T:
    for attempt in range(1, _ANSWERING + 1):
        try:
            session = await agent.spawn(env=env)
            return await agent.run(prompt, session=session, output_schema=schema)
        except PERMANENT:
            raise
        except HarnessError as why:
            if attempt == _ANSWERING:
                raise
            print(f"Warning: {why}; asking again.")
    raise AssertionError("a positive number of attempts asked nothing")


class _TurnError(RuntimeError):
    def __init__(self, stage: str, why: str, *, timed_out: bool = False) -> None:
        super().__init__(f"{stage}: {why}")
        self.stage = stage
        self.timed_out = timed_out


class _EmptyTurnError(ValueError):
    pass


@dataclass
class _Turns:
    agents: Planning
    env: Here
    config: Plan
    writing: Session | None = None
    began: float = field(default_factory=time.monotonic)
    stopped: set[str] = field(default_factory=set[str])


def _turn_limit(config: Plan, began: float, stage: str) -> float:
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


async def _take(
    turns: _Turns,
    role: Literal["planner", "analyst"],
    prompt: str,
    stage: str,
    *,
    schema: type[BaseModel] | None = None,
) -> Any:
    if role in turns.stopped:
        raise _TurnError(
            stage, f"the {role} was stopped after a timeout", timed_out=True
        )
    agent = turns.agents[role]
    attempts = turns.config.turn_retries + 1
    for attempt in range(1, attempts + 1):
        limit = _turn_limit(turns.config, turns.began, stage)
        try:
            if role == "analyst":
                session = await agent.spawn(env=turns.env)
            elif turns.writing is None:
                session = turns.writing = await agent.spawn(env=turns.env)
            else:
                session = turns.writing
            answer = await timed(
                lambda budget, session=session: (
                    agent.run(prompt, session=session, budget=budget)
                    if schema is None
                    else agent.run(
                        prompt, session=session, output_schema=schema, budget=budget
                    )
                ),
                limit,
            )
            if role == "analyst" and not str(answer).strip():
                raise _EmptyTurnError("the turn returned an empty answer")
        except TurnTimedOut as why:
            turns.stopped.add(role)
            raise _TurnError(stage, str(why), timed_out=True) from why
        except PERMANENT as why:
            raise _TurnError(stage, str(why)) from why
        except (HarnessError, _EmptyTurnError) as why:
            if attempt == attempts:
                raise _TurnError(stage, str(why)) from why
            print(f"Warning: {stage} failed; retrying ({attempt} of {attempts}): {why}")
        else:
            return answer
    raise AssertionError("a positive number of planning attempts took no turn")


def _candidate_text(plan: str) -> str:
    return plan.partition("\n--- Original Design Draft Start ---\n")[0].rstrip()


def _material_digest(plan: str) -> str:
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


async def _partial(env: Here, where: PurePosixPath, why: str) -> None:
    held = await read(env, where) or ""
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
    await write(env, where, held)


async def _stage(env: Here, where: PurePosixPath) -> PurePosixPath:
    staged = where.with_name(f".humanize-plan-{uuid.uuid4().hex}.tmp")
    await write(env, staged, await read(env, where) or "")
    return staged


async def _promote(env: Here, staged: PurePosixPath, where: PurePosixPath) -> None:
    if failed := await move(env, staged, where):
        raise RuntimeError(f"could not move {staged} to {where}: {failed}")


async def _idea(drafter: Agent, env: Here, task: str, config: Idea) -> PurePosixPath:
    where = _under(env.workdir, config.output or f"{IDEAS}/{_slug(task)}-{_stamp()}.md")
    if await read(env, where) is not None:
        raise ValueError(
            f"{where}: output file already exists - choose a different path"
        )
    await _writable(env, where.parent)
    session = await drafter.spawn(env=env)
    asked = render(
        planning.GEN_IDEA,
        N=config.n,
        OUTPUT_FILE=where,
        TEMPLATE=planning.GEN_IDEA_TEMPLATE,
        IDEA_BODY=task,
    )
    for _ in range(_ANSWERING):
        said = await drafter.run(asked, session=session)
        if await read(env, where) is not None:
            return where
        if said.strip():
            raise ValueError(
                f"{where}: the drafter wrote no draft, saying: {said.strip()}"
            )
    raise ValueError(f"{where}: the drafter answered nothing and wrote no draft")


async def _plan(turns: _Turns, task: str, draft: PurePosixPath) -> PurePosixPath:
    env, config = turns.env, turns.config
    held = await read(env, draft)
    if held is None:
        raise ValueError(f"{draft}: input file not found")
    if not held.strip():
        raise ValueError(f"{draft}: input file is empty")
    where = _under(env.workdir, config.output or PLAN)
    if await read(env, where) is not None:
        raise ValueError(
            f"{where}: output file already exists - please choose another path"
        )
    await _writable(env, where.parent)

    draft_suffix = (
        "\n--- Original Design Draft Start ---\n\n"
        + held
        + "\n--- Original Design Draft End ---\n"
    )
    template = planning.GEN_PLAN_TEMPLATE + draft_suffix
    try:
        relevance: Relevance = await _take(
            turns,
            "analyst",
            render(planning.RELEVANCE, INPUT_FILE=draft, DRAFT_CONTENT=held),
            "draft relevance check",
            schema=Relevance,
        )
    except _TurnError as why:
        await write(env, where, template)
        await _partial(env, where, str(why))
        print(
            f"Warning: {why}; returning the template and original draft as a partial plan."
        )
        return where
    if not relevance.relevant:
        raise ValueError(
            f"the draft does not appear to be related to this repository: {relevance.why}"
        )

    await write(env, where, template)
    limitations: list[str] = []

    try:
        analysis: str = await _take(
            turns,
            "analyst",
            render(planning.GEN_PLAN_ANALYSIS, INPUT_FILE=draft, DRAFT_CONTENT=held),
            "independent planning analysis",
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

    staged = await _stage(env, where)
    try:
        await _take(
            turns,
            "planner",
            render(planning.GEN_PLAN_CANDIDATE, OUTPUT_FILE=staged, ANALYSIS=analysis),
            "candidate plan",
        )
    except _TurnError as why:
        await remove(env, staged)
        await _partial(env, where, str(why))
        print(
            f"Warning: {why}; returning the template and original draft as a partial plan."
        )
        return where
    candidate = await read(env, staged) or ""
    if _candidate_text(candidate) == _candidate_text(template):
        await remove(env, staged)
        raise RuntimeError(
            "gen-plan's planner returned without writing the candidate plan"
        )
    if not candidate.endswith(draft_suffix):
        await remove(env, staged)
        raise RuntimeError("gen-plan's planner did not preserve the original draft")
    await _promote(env, staged, where)

    converged = False
    prior = ""
    unchanged = 0
    material = _material_digest(candidate)
    if config.mode == "discussion" and "analyst" not in turns.stopped:
        for round_number in range(1, CONVERGING + 1):
            current = await read(env, where) or ""
            try:
                round_: Convergence = await _take(
                    turns,
                    "analyst",
                    render(
                        planning.GEN_PLAN_CONVERGENCE,
                        OUTPUT_FILE=where,
                        TASK=task,
                        PRIOR=prior,
                        ROUND=round_number,
                        TOTAL_ROUNDS=CONVERGING,
                        PLAN_CONTENT=_candidate_text(current),
                    ),
                    f"reasonability review {round_number}",
                    schema=Convergence,
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
            staged = await _stage(env, where)
            try:
                await _take(
                    turns,
                    "planner",
                    render(
                        planning.GEN_PLAN_REVISION, OUTPUT_FILE=staged, REVIEW=review
                    ),
                    f"plan revision {round_number}",
                )
            except _TurnError as why:
                await remove(env, staged)
                limitations.append(str(why))
                print(f"Warning: {why}; keeping the previous candidate.")
                break
            revised = await read(env, staged) or ""
            if not revised.endswith(draft_suffix):
                await remove(env, staged)
                limitations.append(
                    f"plan revision {round_number}: the original draft was not preserved"
                )
                break
            await _promote(env, staged, where)
            changed = _material_digest(revised)
            unchanged = unchanged + 1 if changed == material else 0
            material = changed
            if unchanged >= _NO_MATERIAL_ROUNDS:
                limitations.append(
                    "convergence stopped after two consecutive revisions made no material "
                    "plan changes"
                )
                break

    reviewing = not (
        config.auto_start_rlcr_if_converged
        and converged
        and config.mode == "discussion"
    )
    status = "converged" if converged else "partially_converged"
    if "planner" in turns.stopped:
        await _partial(
            env, where, limitations[-1] if limitations else "the planner timed out"
        )
        return where
    staged = await _stage(env, where)
    try:
        await _take(
            turns,
            "planner",
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
            "final plan consolidation",
        )
    except _TurnError as why:
        await remove(env, staged)
        await _partial(env, where, str(why))
        print(f"Warning: {why}; returning the last durable candidate.")
        return where
    finished = await read(env, staged) or ""
    if not finished.endswith(draft_suffix):
        await remove(env, staged)
        await _partial(
            env, where, "final consolidation did not preserve the original draft"
        )
        return where
    finished = re.sub(
        r"(?m)^- Final Status:.*$",
        f"- Final Status: `{status}`",
        finished,
        count=1,
    )
    await write(env, staged, finished)
    await _promote(env, staged, where)

    if undecided := _undecided(finished):
        raise ValueError(
            f"{where}: `PENDING` still stands on {', '.join(undecided)} under "
            "`## Pending User Decisions`, and a loop handed a plan nobody finished "
            "deciding builds none of it. The plan is written, every position with it -- "
            "answer each `Decision Status` in the file, or run gen-plan again with "
            "somebody at the prompt."
        )

    language, code = _language(config.alternative_plan_language)
    if language:
        variant = where.with_name(f"{where.stem}_{code}{where.suffix}")
        staged = variant.with_name(f".humanize-plan-{uuid.uuid4().hex}.tmp")
        try:
            await _take(
                turns,
                "planner",
                render(
                    planning.GEN_PLAN_TRANSLATE,
                    OUTPUT_FILE=where,
                    LANGUAGE=language,
                    VARIANT_FILE=staged,
                ),
                f"{language} plan translation",
            )
        except _TurnError as why:
            await remove(env, staged)
            print(
                f"Warning: {why}; the main plan is complete but no translation was kept."
            )
        else:
            if failed := await move(env, staged, variant):
                print(
                    f"Warning: {failed}; the main plan is complete but no translation "
                    "was kept."
                )
    return where


async def _again(
    reviewer: Agent,
    env: Here,
    config: Rlcr,
    plan: PurePosixPath | None,
    kept: FlowState | None,
) -> tuple[Loop, str] | None:
    said = str(kept["loop"] or "") if kept is not None and "loop" in kept else ""
    if not said:
        return None
    where = _under(env.workdir, said)
    running = await Loop.picked_up(reviewer, env, where, kept)
    if running is None:
        print(
            f"{where}: no live state file to carry on from -- that loop has ended, or was "
            "written by another version of this flow. Starting a fresh loop."
        )
        return None
    if moved := await _moved(running):
        print(f"{where}: {moved}. Starting a fresh loop.")
        return None
    if differs := _differs(running, config, plan):
        print(f"{where}: {differs}. Starting a fresh loop.")
        return None
    told = await read(env, running.prompt) or ""
    if not told.strip():
        print(
            f"{running.prompt}: nothing was written down for where that loop is, so there "
            "is nothing to send a builder back in with. Starting a fresh loop."
        )
        return None
    running.state.codex_model = reviewer.model
    running.state.codex_effort = reviewer.effort
    await write(env, running.state_file, running.state.written())
    print(f"Carrying on the loop in {where}, {_where_it_is(running)}.")
    return running, told


async def _built(builder: Builder, session: Session, asking: str) -> None:
    for attempt in range(1, loop._TRIES + 1):
        try:
            await builder.run(asking, session=session)
        except (*PERMANENT, SessionError):
            raise
        except HarnessError as why:
            if attempt == loop._TRIES:
                raise
            print(f"Warning: the builder's turn failed; taking it again: {why}")
            await asyncio.sleep(loop._PAUSE * attempt)
        else:
            return


def _where_it_is(running: Loop) -> str:
    if running.analysing:
        return "in the methodology analysis it is exiting through"
    if running.finalizing:
        return "in the finalize round"
    return f"at round {running.state.current_round}"


async def _moved(running: Loop) -> str:
    state, env, root = running.state, running.env, running.root
    branch = await _head(env)
    if state.start_branch and branch != state.start_branch:
        return f"that loop is building on {state.start_branch}, and this is on {branch}"
    plan, backup = root / state.plan_file, running.where / "plan.md"
    held = await read(env, plan)
    if held is None:
        return f"the plan that loop is building is not at {plan} any more"
    if state.review_started:
        return ""
    if state.plan_file and (
        refused := await guards.tracking(
            env, state.plan_file, tracked=state.plan_tracked
        )
    ):
        return refused
    kept = await read(env, backup)
    if kept is None:
        return f"that loop's own copy of {state.plan_file} is not in {running.where} any more"
    if held != kept:
        return f"{plan} has changed since that loop was set up"
    return ""


def _differs(running: Loop, config: Rlcr, plan: PurePosixPath | None) -> str:
    state, root = running.state, running.root
    if plan is not None and (named := _named(root, plan)) != state.plan_file:
        return f"that loop is building {state.plan_file}, and this run says {named}"
    if config.skip_impl and not state.review_started:
        return "that loop is building a plan, and this run says skip_impl"
    if not config.skip_impl and not state.bitlesson_required:
        return (
            "that loop was set up with skip_impl, and this run says it builds the plan"
        )
    if (
        config.base_branch
        and not config.skip_code_review
        and config.base_branch != state.base_branch
    ):
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


async def _fresh(
    agents: Building,
    env: Here,
    config: Rlcr,
    plan: PurePosixPath | None,
    kept: FlowState | None,
) -> tuple[Loop, str]:
    root = env.workdir
    reviewer = agents["reviewer"]
    held = ""
    if plan is not None:
        said = await read(env, plan)
        if said is None:
            raise ValueError(f"{plan}: no plan file to build")
        held = said
        if len(held.splitlines()) < _ENOUGH:
            raise ValueError(f"{plan}: the plan file has almost nothing in it")
        if refused := await guards.tracking(
            env, _named(root, plan), tracked=config.track_plan_file
        ):
            raise ValueError(refused)

    base = await _review_base(env, config)
    commit = ""
    if base:
        status, commit = await git(
            env, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"
        )
        if status:
            raise ValueError(f"base_branch {base!r} names no commit in this repository")

    if plan is not None and not config.skip_impl:
        checked = await _answered(
            reviewer,
            env,
            render(prompts.PLAN_COMPLIANCE, PLAN_FILE=plan, PLAN_CONTENT=held),
            Compliance,
        )
        if not checked.relevant:
            raise ValueError(
                f"the plan is not related to this repository: {checked.why}"
            )
        if checked.switches_branch:
            raise ValueError(
                "the plan contains branch-switching instructions, which are incompatible "
                f"with RLCR: {checked.why}"
            )

    if (
        plan is not None
        and not (config.skip_quiz or config.skip_impl)
        and not agents["human"].away
    ):
        await _understood(agents, env, plan, held)

    where = loop.directory(root, loop.started())
    if plan is None:
        await write(
            env,
            where / "plan.md",
            "# Skip Implementation Mode\n\nThis RLCR loop was started with `skip_impl`, "
            "which skips the implementation phase and goes directly to code review.\n\n"
            "No implementation plan was provided - this is expected for skip-impl mode.\n",
        )
        named = _named(root, where / "plan.md")
    else:
        await write(env, where / "plan.md", held)
        named = _named(root, plan)

    state = State(
        current_round=0,
        max_iterations=config.max,
        codex_model=reviewer.model,
        codex_effort=reviewer.effort,
        codex_timeout=config.codex_timeout,
        push_every_round=config.push_every_round,
        full_review_round=config.full_review_round,
        plan_file=named,
        plan_tracked=config.track_plan_file,
        start_branch=await _head(env),
        base_branch=base,
        base_commit=commit,
        review_started=config.skip_impl,
        ask_codex_question=not config.claude_answer_codex,
        agent_teams=config.agent_teams,
        privacy_mode=config.privacy,
        bitlesson_required=not config.skip_impl,
        bitlesson_allow_empty_none=not config.require_bitlesson_entry_for_none,
        mainline_stall_count=0,
        started_at=utc(),
    )
    running = Loop(reviewer, env, where, state, kept=kept)
    await _set_up(running, config, plan, held)
    if config.skip_impl:
        await write(env, where / loop.REVIEW_STARTED, "build_finish_round=0\n")

    told = _round_zero(running, config, held)
    await write(env, running.prompt, told)
    if kept is not None:
        kept["loop"] = _named(root, where)
        kept["rounds"] = state.current_round
    return running, told


async def _understood(
    agents: Building, env: Here, plan: PurePosixPath, held: str
) -> None:
    reviewer, human = agents["reviewer"], agents["human"]
    try:
        quiz = await reviewer.run(
            render(prompts.PLAN_UNDERSTANDING_QUIZ, PLAN_FILE=plan, PLAN_CONTENT=held),
            session=await reviewer.spawn(env=env),
            output_schema=Quiz,
        )
    except HarnessError:
        quiz = None
    if quiz is None or not quiz.questions:
        print("Plan understanding quiz unavailable, continuing without it.")
        return
    person = await human.spawn(env=env)
    right = 0
    asked = 0
    for question in quiz.questions:
        picked = await _asked(human, person, question.question, question.options)
        if not picked:
            return
        asked += 1
        right += picked == question.answer
    if asked and right == asked:
        print("Your understanding of the plan looks solid. Proceeding with setup.")
        return
    going = await _asked(
        human,
        person,
        f"{quiz.summary}\n\nThe answers were "
        + ", ".join(
            f"Q{at + 1}: {question.answer}"
            for at, question in enumerate(quiz.questions)
        )
        + ".\n\nWould you like to proceed with the RLCR loop anyway, or stop and review "
        "the plan more carefully first?",
        ["Proceed with RLCR loop", "Stop and review the plan first"],
    )
    if going == "B":
        raise ValueError(
            "stopping. Please review the plan file and run the flow again when ready"
        )


async def _set_up(
    running: Loop, config: Rlcr, plan: PurePosixPath | None, held: str
) -> None:
    env = running.env
    lessons = running.root / running.state.bitlesson_file
    if await read(env, lessons) is None:
        await write(env, lessons, prompts.BITLESSON)
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
    await write(env, running.tracker, tracker)
    await write(env, running.summary, render(prompts.SUMMARY_TEMPLATE, ROUND=0))
    if config.skip_impl:
        await write(
            env,
            running.contract,
            render(
                prompts.ROUND_CONTRACT_SKIP_IMPL_ANCHORED,
                PLAN_FILE=running.state.plan_file,
            )
            if plan is not None
            else prompts.ROUND_CONTRACT_SKIP_IMPL,
        )
    await write(env, running.state_file, running.state.written())


def _round_zero(running: Loop, config: Rlcr, held: str) -> str:
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


@flow(
    agents=Drafting,
    envs=Where,
    params=Idea,
    name="gen-idea",
    description="Opens a loose idea into a repo-grounded draft.",
)
async def gen_idea(
    task: str,
    *,
    agents: Drafting,
    envs: Where,
    params: Idea,
    ctx: FlowContext,  # noqa: ARG001
) -> str:
    if not task.strip():
        raise ValueError("gen-idea opens an idea, and this run was given none")
    return str(await _idea(agents["drafter"], envs["workspace"], task, params))


@flow(
    agents=Planning,
    envs=Where,
    params=Plan,
    name="gen-plan",
    description="Turns a draft into a plan the writing and the reading side have converged on.",
)
async def gen_plan(
    task: str,
    *,
    agents: Planning,
    envs: Where,
    params: Plan,
    ctx: FlowContext,  # noqa: ARG001
) -> str:
    env = envs["workspace"]
    draft = _under(env.workdir, params.input) if params.input else await _last(env)
    return str(await _plan(_Turns(agents, env, params), task, draft))


@flow(
    agents=Building,
    envs=Where,
    params=Rlcr,
    name="rlcr",
    description="Builds the plan under review until nothing is left to say.",
    resumable=True,
)
async def rlcr(
    task: str,  # noqa: ARG001
    *,
    agents: Building,
    envs: Where,
    params: Rlcr,
    ctx: FlowContext,
) -> str:
    env = envs["workspace"]
    root = env.workdir
    plan = (
        _under(root, params.plan_file)
        if params.plan_file
        else None
        if params.skip_impl
        else _under(root, PLAN)
    )
    if await _head(env) == "":
        raise ValueError(
            "rlcr runs in a git repository: every review reads the work since the commit "
            "the plan was fixed in"
        )
    if (
        params.agent_teams
        and os.environ.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") != "1"
    ):
        raise ValueError(
            "agent_teams requires the CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS environment "
            "variable to be set:\n\n  export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"
        )
    if params.push_every_round and not (await git(env, "remote"))[1]:
        raise ValueError(
            "push_every_round needs a remote to push to, and this repository has none"
        )
    carrying = await _again(agents["reviewer"], env, params, plan, ctx.state)
    running, told = (
        carrying
        if carrying is not None
        else await _fresh(agents, env, params, plan, ctx.state)
    )
    builder = agents["builder"]
    guard = guards.Guard(running)
    builder.on_permission_request(guard)
    builder.on_pre_tool_use(guard.watching)
    builder.on_user_prompt_submit(guards.Prompted(running))
    session = await builder.spawn(env=env)
    asking: str | None = told
    while asking is not None:
        await _built(builder, session, asking)
        asking = running.continuing = await running.stopped()
    return running.over


__all__ = [
    "Builder",
    "Building",
    "Choice",
    "Compliance",
    "Convergence",
    "Drafting",
    "Here",
    "Idea",
    "Plan",
    "Planning",
    "Quiz",
    "Relevance",
    "Rlcr",
    "Where",
    "gen_idea",
    "gen_plan",
    "rlcr",
]
