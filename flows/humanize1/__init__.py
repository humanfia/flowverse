"""RLCR (humanize 1) -- PolyArch/humanize as three flows, each set up before it starts."""

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

from hmz.flows import Agent, Moment, Person, Session, Stopped, Unrecoverable, flow
from hmz.flows import Question as Asking

if TYPE_CHECKING:
    from collections.abc import Callable


class Drafting(NamedTuple):
    drafter: Agent


class Planning(NamedTuple):
    planner: Agent
    analyst: Agent


class Building(NamedTuple):
    builder: Annotated[Agent, Moment.PERMISSION_REQUEST]
    reviewer: Agent
    human: Person


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

_STOP_GRACE = 1.0

IDEAS = ".humanize/ideas"

PLAN = "docs/plan.md"


class Relevance(BaseModel):
    model_config = {"extra": "forbid"}

    relevant: bool = Field(
        description="Whether the draft is related to this repository. Be lenient: false only "
        "for a draft that is clearly about something else entirely."
    )
    why: str = Field(description="One or two sentences saying why.")


class Convergence(BaseModel):
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
    model_config = {"extra": "forbid"}

    question: str = Field(description="The question itself.")
    options: list[str] = Field(
        description="Exactly four options, in order: A, B, C and D."
    )
    answer: Literal["A", "B", "C", "D"] = Field(description="Which one is correct.")


class Quiz(BaseModel):
    model_config = {"extra": "forbid"}

    questions: list[Question] = Field(
        description="Exactly two questions, in the order they are to be asked."
    )
    summary: str = Field(
        description="Two or three sentences on what the plan does and how, for a reader who "
        "showed gaps in understanding. The technical approach, not just the goal."
    )


class Idea(BaseModel):
    model_config = {"frozen": True}

    n: int = Field(
        default=6, ge=2, le=10, description="--n: how many directions explore the idea"
    )
    output: str = Field(
        default="",
        description="--output: where the draft goes, blank for .humanize/ideas",
    )


class Plan(BaseModel):
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
    import datetime

    return datetime.datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def _head(root: Path) -> str:
    status, branch = git("rev-parse", "--abbrev-ref", "HEAD", at=root)
    return "" if status else branch


def _base(root: Path, asked: str) -> str:
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


def _review_base(root: Path, config: Rlcr) -> str:
    return "" if config.skip_code_review else _base(root, config.base_branch)


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


def _asked(human: Person, question: str, options: list[str]) -> str:
    listed = list(zip("ABCD", options, strict=False))
    said = human.asked(
        Asking(
            text=question,
            options=tuple(f"{letter}. {one}" for letter, one in listed),
        )
    )
    if not said:
        return ""
    for letter, one in listed:
        if said.strip() in (one, f"{letter}. {one}"):
            return letter
    return said.strip()[:1].upper()


class _DeadlineError(TimeoutError):
    def __init__(self, message: str, done: threading.Event) -> None:
        super().__init__(message)
        self.done = done


class _TurnError(RuntimeError):
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
    pass


def _within[T](owner: Agent, call: Callable[[], T], seconds: float, stage: str) -> T:
    if seconds <= 0:
        return call()

    landed: list[tuple[bool, object]] = []
    done = threading.Event()

    def run() -> None:
        try:
            landed.append((True, call()))
        except BaseException as why:  # noqa: BLE001
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


def _partial(where: Path, why: str) -> None:
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
    staged = where.with_name(f".humanize-plan-{uuid.uuid4().hex}.tmp")
    shutil.copyfile(where, staged)
    return staged


def _abandon(staged: Path, why: _TurnError, owner: Agent) -> None:
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
    staged.replace(where)


def _idea(drafting: Session, task: str, config: Idea, root: Path) -> Path:
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
    if _head(root) == "":
        raise ValueError(
            "rlcr runs in a git repository: every review reads the work since the commit "
            "the plan was fixed in"
        )
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
    told = (
        running.prompt.read_text(encoding="utf-8") if running.prompt.is_file() else ""
    )
    if not told.strip():
        print(
            f"{running.prompt}: nothing was written down for where that loop is, so there "
            "is nothing to send a builder back in with. Starting a fresh loop."
        )
        return None
    running.state.codex_model = reviewer.config.model
    running.state.codex_effort = reviewer.config.effort
    running.state_file.write_text(running.state.written(), encoding="utf-8")
    print(f"Carrying on the loop in {where}, {_where_it_is(running)}.")
    return running, told


def _where_it_is(running: Loop) -> str:
    if running.analysing:
        return "in the methodology analysis it is exiting through"
    if running.finalizing:
        return "in the finalize round"
    return f"at round {running.state.current_round}"


def _moved(running: Loop) -> str:
    state, root = running.state, running.root
    branch = _head(root)
    if state.start_branch and branch != state.start_branch:
        return f"that loop is building on {state.start_branch}, and this is on {branch}"
    plan, backup = root / state.plan_file, running.where / "plan.md"
    if not plan.is_file():
        return f"the plan that loop is building is not at {plan} any more"
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
    return str(plan.relative_to(root) if plan.is_relative_to(root) else plan)


def _says(value: object) -> str:
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
    held = ""
    if plan is not None:
        if not plan.is_file():
            raise ValueError(f"{plan}: no plan file to build")
        held = plan.read_text(encoding="utf-8")
        if len(held.splitlines()) < _ENOUGH:
            raise ValueError(f"{plan}: the plan file has almost nothing in it")

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

    base = _review_base(root, config)
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
    kept.update(loop=str(where.relative_to(root)), rounds=state.current_round)
    return running, told


_ENOUGH = 5


def _utc() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _understood(agents: Building, plan: Path, held: str) -> None:
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
            return
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
    if going == "B":
        raise ValueError(
            "stopping. Please review the plan file and run the flow again when ready"
        )


def _set_up(running: Loop, config: Rlcr, plan: Path | None, held: str) -> None:
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
    written = [one for one in (root / IDEAS).glob("*.md") if one.is_file()]
    if not written:
        raise ValueError(
            f"no draft to plan from under {IDEAS}: run gen-idea first, or set input to a "
            "draft you already have"
        )
    return max(written, key=lambda one: one.stat().st_mtime)


def _under(root: Path, said: str) -> Path:
    where = Path(said)
    return where if where.is_absolute() else root / where


@flow(name="gen-idea", about="Opens a loose idea into a repo-grounded draft.")
def gen_idea(agents: Drafting, task: str, config: Idea | None = None) -> None:
    if not task.strip():
        raise ValueError("gen-idea opens an idea, and this run was given none")
    _idea(agents.drafter.new(), task, config or Idea(), Path.cwd())


@flow(
    name="gen-plan",
    about="Turns a draft into a plan the writing and the reading side have converged on.",
)
def gen_plan(agents: Planning, task: str, config: Plan | None = None) -> None:
    setting = config or Plan()
    root = Path.cwd()
    draft = _under(root, setting.input) if setting.input else _last(root)
    _plan(agents, agents.planner.new(), task, setting, root, draft)


@flow(
    name="rlcr",
    about="Builds the plan under review until nothing is left to say.",
    resumable=True,
)
def rlcr(
    agents: Building,
    task: str,  # noqa: ARG001
    config: Rlcr | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    setting = config or Rlcr()
    root = Path.cwd()
    plan = (
        _under(root, setting.plan_file)
        if setting.plan_file
        else None
        if setting.skip_impl
        else _under(root, PLAN)
    )
    _rlcr(
        agents,
        agents.builder.new(),
        setting,
        root,
        plan,
        state if state is not None else {},
    )
