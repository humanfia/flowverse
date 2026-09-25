from __future__ import annotations

import asyncio
import datetime
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, cast

from hmz.flows import (
    Agent,
    AgentCollection,
    Budget,
    EnvCollection,
    EnvError,
    EnvFileNotFound,
    FilesEnvMixin,
    FlowParams,
    HarnessError,
    HarnessNotInstalled,
    HarnessRefused,
    HarnessUnrecoverable,
    LocalEnv,
    ModelUnavailable,
    ShellEnvMixin,
    flow,
)

from . import blocks, prompts
from .prompts import render

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import PurePosixPath

    from hmz.flows import FlowContext, FlowState

__all__ = [
    "ALLOWED",
    "COMPLETE",
    "MAX_LINES",
    "PERMANENT",
    "STOP",
    "Here",
    "Loop",
    "State",
    "Todos",
    "TurnTimedOut",
    "git",
    "issues",
    "move",
    "read",
    "remove",
    "timed",
    "utc",
    "verdict",
    "write",
]


class Here(LocalEnv, ShellEnvMixin, FilesEnvMixin): ...


COMPLETE = "COMPLETE"
STOP = "STOP"

MAX_LINES = 2000
_CODE = frozenset({
    "py", "js", "ts", "tsx", "jsx", "java", "c", "cpp", "cc", "cxx", "h", "hpp",
    "cs", "go", "rs", "rb", "php", "swift", "kt", "kts", "scala", "sh", "bash", "zsh",
})  # fmt: skip
_DOCS = frozenset(["md", "rst", "txt", "adoc", "asciidoc"])

ADVANCED, STALLED, REGRESSED, UNKNOWN = "advanced", "stalled", "regressed", "unknown"
NORMAL, REPLAN_REQUIRED = "normal", "replan_required"

_REPLAN_AT = 2
_STOP_AT = 3

_GIT = 30

_TRIES = 3
_PAUSE = 5.0

PERMANENT = (
    HarnessNotInstalled,
    HarnessRefused,
    ModelUnavailable,
    HarnessUnrecoverable,
)

LOOPS = ".humanize/rlcr"
BITLESSON = ".humanize/bitlesson.md"

REVIEW_STARTED = ".review-phase-started"
EXIT_REASON = ".methodology-exit-reason"

BUILDING = "state.md"
FINALIZING = "finalize-state.md"
ANALYSING = "methodology-analysis-state.md"

_OURS = re.compile(r"^\?\? \.humanize[-/]")

_VERDICT = re.compile(
    r"Mainline Progress Verdict:\s*(ADVANCED|STALLED|REGRESSED)(?:[^A-Za-z]|$)",
    re.IGNORECASE,
)
_VERDICTS = re.compile(r"ADVANCED|STALLED|REGRESSED", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\[To be [a-z]")

_FINDING = re.compile(r"\[P[0-9]\]")

_SCANNED = 50
_RECENT = 3
_HEADING = 40

_DELTA = re.compile(r"^##\s+BitLesson Delta\s*$", re.MULTILINE)
_ACTION = re.compile(r"^[\s-]*Action:\s*([A-Za-z]+)\s*$", re.MULTILINE)
_LESSONS = re.compile(r"^[\s-]*Lesson ID\(s\):\s*(.*)$", re.MULTILINE)
_NOTES = re.compile(r"^[\s-]*Notes:\s*(.*)$", re.MULTILINE)
_UNWRITTEN = re.compile(r"^(\[.*\]|<.*>)$")
_LESSON_ID = re.compile(r"^Lesson ID:\s*(\S+)\s*$", re.MULTILINE)

ALLOWED = ("complete", "cancel", "maxiter", "stop", "unexpected")


async def git(env: Here, *args: str) -> tuple[int, str]:
    try:
        status, out, _ = await env.exec(["git", *args], timeout=_GIT)
    except EnvError:
        return 124, ""
    return status, out.strip()


async def read(env: Here, path: PurePosixPath | str) -> str | None:
    try:
        held = (await env.read(str(path))).decode("utf-8", errors="replace")
    except EnvFileNotFound:
        return None
    return held.replace("\r\n", "\n").replace("\r", "\n")


async def write(env: Here, path: PurePosixPath | str, text: str) -> None:
    await env.write(str(path), text.encode("utf-8"))


async def remove(env: Here, path: PurePosixPath | str) -> None:
    try:
        await env.exec(["rm", "-f", "--", str(path)], timeout=_GIT)
    except EnvError:
        return


async def move(env: Here, was: PurePosixPath | str, to: PurePosixPath | str) -> str:
    try:
        status, _, err = await env.exec(
            ["mv", "-f", "--", str(was), str(to)], timeout=_GIT
        )
    except EnvError as why:
        return str(why) or "mv failed"
    return "" if status == 0 else err.strip() or f"mv exited {status}"


class TurnTimedOut(Exception):
    pass


async def timed[T](turn: Callable[[Budget | None], Awaitable[T]], seconds: float) -> T:
    budget = (
        Budget(duration=datetime.timedelta(seconds=seconds), graceful=False)
        if seconds > 0
        else None
    )
    deadline = time.monotonic() + seconds
    try:
        async with asyncio.timeout(seconds or None):
            return await turn(budget)
    except TimeoutError as why:
        if budget is None or time.monotonic() < deadline:
            raise
        raise TurnTimedOut(f"took longer than {seconds:g}s") from why


class _Reviewing(AgentCollection):
    reviewer: Agent


class _Reviewed(EnvCollection):
    workspace: Here


class _Timed(FlowParams):
    seconds: int = 0


@flow(
    agents=_Reviewing,
    envs=_Reviewed,
    params=_Timed,
    name="rlcr-review",
    hidden=True,
)
async def review_once(
    task: str,
    *,
    agents: _Reviewing,
    envs: _Reviewed,
    params: _Timed,
    ctx: FlowContext,  # noqa: ARG001
) -> str:
    reviewer = agents["reviewer"]
    session = await reviewer.spawn(env=envs["workspace"])
    return await timed(
        lambda budget: reviewer.run(task, session=session, budget=budget),
        params.seconds,
    )


def verdict(said: str) -> str:
    lines = [line for line in said.splitlines() if _VERDICT.search(line)]
    if not lines:
        return UNKNOWN
    found = _VERDICTS.findall(lines[-1])
    return found[0].lower() if len(found) == 1 else UNKNOWN


def issues(said: str) -> str:
    lines = said.splitlines()
    tail = lines[-_SCANNED:]
    for at, line in enumerate(tail):
        if _FINDING.search(line[:10]):
            found = "\n".join(tail[at:])
            return f"## Code Review Issues\n\n{found}\n"
    return ""


@dataclass
class State:
    current_round: int = 0
    max_iterations: int = 42
    codex_model: str = ""
    codex_effort: str = ""
    codex_timeout: int = 5400
    push_every_round: bool = False
    full_review_round: int = 5
    plan_file: str = ""
    plan_tracked: bool = False
    start_branch: str = ""
    base_branch: str = ""
    base_commit: str = ""
    review_started: bool = False
    ask_codex_question: bool = True
    session_id: str = ""
    agent_teams: bool = False
    privacy_mode: bool = False
    bitlesson_required: bool = True
    bitlesson_file: str = BITLESSON
    bitlesson_allow_empty_none: bool = True
    mainline_stall_count: int = 0
    last_mainline_verdict: str = UNKNOWN
    drift_status: str = NORMAL
    started_at: str = ""

    def written(self) -> str:
        said = [f"{name}: {_yaml(value)}" for name, value in asdict(self).items()]
        return "---\n" + "\n".join(said) + "\n---\n"

    @classmethod
    def read(cls, held: str | None) -> State | None:
        lines = (held or "").splitlines()
        if not lines or lines[0].strip() != "---":
            return None
        said: dict[str, str] = {}
        for line in lines[1:]:
            if line.strip() == "---":
                break
            name, sep, value = line.partition(":")
            if not sep:
                return None
            said[name.strip()] = value.strip()
        kept: dict[str, Any] = {}
        for name, was in asdict(cls()).items():
            found = _read(said.pop(name), was) if name in said else None
            if found is None:
                return None
            kept[name] = found
        return None if said else cls(**kept)


def _yaml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _read(said: str, was: object) -> str | int | bool | None:
    if isinstance(was, bool):
        return said == "true" if said in ("true", "false") else None
    if isinstance(was, int):
        try:
            return int(said)
        except ValueError:
            return None
    return said


class Todos:
    def __init__(self) -> None:
        self.tasks: dict[str, tuple[str, str, str]] = {}
        self.made = 0
        self.listed: list[tuple[str, str]] = []
        self.touched = False
        self.told: tuple[int, list[str]] = (-1, [])

    def seen(self, tool: str, called: Mapping[str, Any]) -> None:
        if tool not in ("TaskCreate", "TaskUpdate", "TodoWrite", "update_plan"):
            return
        self.touched = True
        if tool == "TaskCreate":
            self.made += 1
            self.tasks[str(self.made)] = (
                "pending",
                str(called.get("subject") or ""),
                str(called.get("description") or ""),
            )
        elif tool == "TaskUpdate":
            named = str(called.get("taskId") or "")
            if not named:
                return
            status, subject, about = self.tasks.get(named, ("pending", "", ""))
            self.tasks[named] = (
                str(called.get("status") or status),
                str(called.get("subject") or subject),
                str(called.get("description") or about),
            )
        elif tool in ("TodoWrite", "update_plan"):
            items: Any = called.get("todos" if tool == "TodoWrite" else "plan")
            if not isinstance(items, list):
                return
            self.listed = [
                (
                    str(one.get("status") or ""),
                    str(one.get("content") or one.get("step") or ""),
                )
                for one in cast("list[Any]", items)
                if isinstance(one, dict)
            ]

    def left(self) -> list[str]:
        found: list[str] = []
        for named, (status, subject, about) in self.tasks.items():
            if status in ("completed", "deleted"):
                continue
            lane = _lane(subject, about)
            if lane == "queued":
                continue
            said = subject or about or f"Task {named}"
            found.append(f"  - [{status}] [{lane}] (Task #{named}) {said}")
        for status, said in self.listed:
            if status == "completed":
                continue
            lane = _lane(said)
            if lane == "queued":
                continue
            found.append(f"  - [{status}] [{lane}] {said}")
        return found


@dataclass
class Loop:
    reviewer: Agent
    env: Here
    where: PurePosixPath
    state: State
    kept: FlowState | None = None
    over: str = ""
    finalizing: bool = False
    analysing: bool = False
    exit_reason: str = ""
    continuing: str | None = None
    todos: Todos = field(default_factory=Todos)
    failing: int = 0
    _status: str | None = None

    @property
    def root(self) -> PurePosixPath:
        return self.env.workdir

    @classmethod
    async def picked_up(
        cls,
        reviewer: Agent,
        env: Here,
        where: PurePosixPath,
        kept: FlowState | None = None,
    ) -> Loop | None:
        for at, finalizing, analysing in (
            (BUILDING, False, False),
            (FINALIZING, True, False),
            (ANALYSING, False, True),
        ):
            state = State.read(await read(env, where / at))
            if state is None:
                continue
            exiting = await read(env, where / EXIT_REASON) if analysing else ""
            return cls(
                reviewer,
                env,
                where,
                state,
                kept=kept,
                finalizing=finalizing,
                analysing=analysing,
                exit_reason=(exiting or "").strip(),
            )
        return None

    async def stopped(self) -> str | None:
        gates: tuple[Callable[[], Awaitable[str | None]], ...] = (
            self._schema,
            self._branch,
            self._plan_integrity,
            self._todos,
            self._git_status,
            self._large_files,
            self._analysis_phase,
            self._git_clean,
            self._unpushed,
            self._summary_written,
            self._contract_written,
            self._bitlesson_delta,
            self._goal_tracker_started,
            self._max_iterations,
            self._finalize_done,
        )
        for gate in gates:
            refused = await gate()
            if self.over:
                return None
            if refused is not None:
                return refused
        said = await self._review()
        if self.over:
            if said:
                print(said)
            return None
        return said

    async def _schema(self) -> str | None:
        held = await read(self.env, self.state_file)
        if held is None:
            await self._ends("unexpected")
            return None
        for name in ("current_round", "max_iterations"):
            found = re.search(rf"^{name}:\s*(\S+)\s*$", held, re.MULTILINE)
            if found is None:
                await self._ends("unexpected")
                return None
            if re.fullmatch(r"[+-]?\d+", found.group(1)):
                setattr(self.state, name, int(found.group(1)))
        return None

    async def _branch(self) -> str | None:
        status, branch = await git(self.env, "rev-parse", "--abbrev-ref", "HEAD")
        if status or not branch:
            return (
                "Git operation failed or timed out.\n\nCannot verify branch "
                "consistency. Please check git status manually and try again."
            )
        if self.state.start_branch and branch != self.state.start_branch:
            return render(
                blocks.BRANCH_CHANGED,
                START_BRANCH=self.state.start_branch,
                CURRENT_BRANCH=branch,
            )
        return None

    async def _plan_integrity(self) -> str | None:
        if self.state.review_started:
            return None
        backup = self.where / "plan.md"
        plan = self.root / self.state.plan_file
        kept = await read(self.env, backup)
        if kept is None:
            return (
                "Plan file backup not found in loop directory.\n\n"
                f"This backup is required for plan integrity verification: {backup}"
            )
        held = await read(self.env, plan)
        if held is None:
            return render(
                blocks.PLAN_FILE_DELETED,
                PLAN_FILE=self.state.plan_file,
                BACKUP_PATH=backup,
            )
        if self.state.plan_tracked:
            _, dirty = await git(
                self.env, "status", "--porcelain", self.state.plan_file
            )
            if dirty:
                return render(
                    blocks.PLAN_FILE_UNCOMMITTED,
                    PLAN_FILE=self.state.plan_file,
                    PLAN_GIT_STATUS=dirty,
                )
        if held != kept:
            return render(
                blocks.PLAN_FILE_MODIFIED,
                PLAN_FILE=self.state.plan_file,
                BACKUP_PATH=backup,
            )
        return None

    async def _todos(self) -> str | None:
        todos = self.todos
        left = todos.left()
        told = (self.state.current_round, left)
        if not left or (todos.told == told and not todos.touched):
            return None
        todos.told, todos.touched = told, False
        return render(blocks.INCOMPLETE_TODOS, INCOMPLETE_LIST="\n".join(left))

    async def _git_status(self) -> str | None:
        self._status = None
        status, _ = await git(self.env, "rev-parse", "--git-dir")
        if status:
            return None
        failed, everything = await git(self.env, "status", "--porcelain")
        if failed:
            return render(blocks.GIT_STATUS_FAILED, GIT_STATUS_EXIT=failed)
        self._status = everything
        return None

    async def _analysis_phase(self) -> str | None:
        if not self.analysing:
            return None
        done = self.where / "methodology-analysis-done.md"
        report = self.where / "methodology-analysis-report.md"
        if not (await self._written(done) and await self._written(report)):
            return (
                "# Methodology Analysis Incomplete\n\nPlease complete the "
                "methodology analysis before exiting.\n\nYou need to:\n"
                f"1. Write the analysis report to {report}\n"
                f"2. Write a completion note to {done}"
            )
        if self._left():
            return render(
                blocks.GIT_NOT_CLEAN,
                GIT_ISSUES="uncommitted changes after methodology analysis",
                SPECIAL_NOTES="",
            )
        await self._ends(self.exit_reason or "unexpected")
        self.analysing = False
        return None

    async def _git_clean(self) -> str | None:
        if self._status is None:
            return None
        tracked, held = await git(self.env, "ls-files", "--", ".humanize")
        if not tracked and held:
            return blocks.GIT_TRACKED_HUMANIZE
        rows = self._status.splitlines()
        left = [row for row in rows if not _OURS.match(row)]
        if not left:
            return None
        notes = ""
        untracked = [row for row in rows if row.startswith("??")]
        if any(_OURS.match(row) for row in untracked):
            notes += blocks.GIT_NOT_CLEAN_HUMANIZE_LOCAL
        if any(not _OURS.match(row) for row in untracked):
            notes += blocks.GIT_NOT_CLEAN_UNTRACKED
        return render(
            blocks.GIT_NOT_CLEAN, GIT_ISSUES="uncommitted changes", SPECIAL_NOTES=notes
        )

    async def _unpushed(self) -> str | None:
        if not self.state.push_every_round:
            return None
        _, said = await git(self.env, "status", "-sb")
        ahead = re.search(r"ahead (\d+)", said)
        if ahead is None:
            return None
        _, branch = await git(self.env, "rev-parse", "--abbrev-ref", "HEAD")
        return render(
            blocks.UNPUSHED_COMMITS,
            AHEAD_COUNT=ahead.group(1),
            CURRENT_BRANCH=branch or "unknown",
        )

    async def _large_files(self) -> str | None:
        if self._status is None:
            return None
        found: list[str] = []
        for row in self._status.splitlines():
            named = row[3:].split(" -> ")[-1]
            path = self.root / named
            kind = path.suffix.lstrip(".").lower()
            about = (
                "code" if kind in _CODE else "documentation" if kind in _DOCS else ""
            )
            if not about:
                continue
            try:
                held = await read(self.env, path)
            except EnvError:
                continue
            if held is None:
                continue
            lines = len(held.splitlines())
            if lines > MAX_LINES:
                found.append(f"\n- `{path}`: {lines} lines ({about} file)")
        if not found:
            return None
        return render(
            blocks.LARGE_FILES, MAX_LINES=MAX_LINES, LARGE_FILES="".join(found)
        )

    async def _summary_written(self) -> str | None:
        if await self._written(self.summary):
            return None
        return render(blocks.WORK_SUMMARY_MISSING, SUMMARY_FILE=self.summary)

    async def _contract_written(self) -> str | None:
        if self.finalizing or await read(self.env, self.contract) is not None:
            return None
        return render(blocks.ROUND_CONTRACT_MISSING, ROUND_CONTRACT_FILE=self.contract)

    async def _bitlesson_delta(self) -> str | None:
        if self.finalizing or not self.state.bitlesson_required:
            return None
        return await self._delta(await read(self.env, self.summary) or "")

    async def _goal_tracker_started(self) -> str | None:
        if self.finalizing or self.state.review_started or self.state.current_round:
            return None
        held = await read(self.env, self.tracker)
        if held is None:
            return None
        missing = [
            f"\n- **{about}**: Still contains placeholder text"
            for heading, about in (
                ("### Ultimate Goal", "Ultimate Goal"),
                ("### Acceptance Criteria", "Acceptance Criteria"),
                ("#### Active Tasks", "Active Tasks"),
            )
            if _PLACEHOLDER.search(_section(held, heading))
        ]
        if not missing:
            return None
        return render(
            blocks.GOAL_TRACKER_NOT_INITIALIZED,
            GOAL_TRACKER_FILE=self.tracker,
            MISSING_ITEMS="".join(missing),
        )

    async def _max_iterations(self) -> str | None:
        if self.finalizing or self.state.review_started:
            return None
        if self.state.current_round + 1 <= self.state.max_iterations:
            return None
        return await self._analyse(
            "maxiter",
            f"Reached max iterations ({self.state.max_iterations}) without completion",
        )

    async def _finalize_done(self) -> str | None:
        if not self.finalizing:
            return None
        return await self._analyse(
            "complete", "All acceptance criteria met and code review passed"
        )

    async def _review(self) -> str | None:
        if (
            self.state.review_started
            and await read(self.env, self.where / REVIEW_STARTED) is None
        ):
            return (
                "Review phase state inconsistency detected.\n\nThe state file "
                "indicates review_started=true, but no review phase marker exists.\nThis "
                "can happen if the state file was manually edited.\n\n**To fix:**\nReset "
                "the state by stopping the flow and starting it again."
            )
        aligning = (
            self.state.current_round % self.state.full_review_round
            == self.state.full_review_round - 1
        )
        asked = await self._review_prompt(aligning=aligning)
        await write(self.env, self.review_prompt, asked)

        said = ""
        if not self.state.review_started:
            before = await read(self.env, self.result) or ""
            said, failed = await self._reviewed(asked, "review")
            if failed:
                return render(
                    blocks.REVIEW_FAILED,
                    FAILURE_REASON=failed,
                    ROUND_NUMBER=self.state.current_round,
                    BASE_BRANCH=self.state.base_branch,
                )
            written = await read(self.env, self.result) or ""
            if not written.strip() or written == before:
                await write(self.env, self.result, said)
            said = await read(self.env, self.result) or ""
            if not said.strip():
                return render(
                    blocks.REVIEW_FAILED,
                    FAILURE_REASON="the review result file is empty",
                    ROUND_NUMBER=self.state.current_round,
                    BASE_BRANCH=self.state.base_branch,
                )
            if (drifted := await self._drift(said)) is not None:
                return drifted
            if _last(said) == COMPLETE:
                return await self._complete()
        if self.state.review_started:
            return await self._code_review()
        if _last(said) == STOP:
            return await self._analyse(
                "stop",
                f"Circuit breaker triggered - stagnation detected at round "
                f"{self.state.current_round}",
            )
        return await self._next_round(said, aligning=aligning)

    async def _reviewed(self, asked: str, what: str) -> tuple[str, str]:
        seconds = self.state.codex_timeout
        for attempt in range(1, _TRIES + 1):
            try:
                said = await review_once(
                    asked,
                    agents={"reviewer": self.reviewer},
                    envs={"workspace": self.env},
                    params=_Timed(seconds=seconds),
                )
            except TurnTimedOut:
                return "", f"the {what} took longer than the {seconds}s it was given"
            except PERMANENT:
                raise
            except HarnessError as why:
                if attempt < _TRIES:
                    await asyncio.sleep(_PAUSE * attempt)
                    continue
                self.failing += 1
                if self.failing >= _TRIES:
                    raise
                return "", f"the {what} failed: {why}"
            self.failing = 0
            return said, ""
        raise AssertionError("a positive number of attempts reviewed nothing")

    async def _drift(self, said: str) -> str | None:
        last = _last(said)
        found = verdict(said)
        if last != STOP and found == UNKNOWN:
            return render(
                blocks.MAINLINE_VERDICT_MISSING,
                REVIEW_RESULT_FILE=self.result,
                REVIEW_PROMPT_FILE=self.review_prompt,
            )
        if found == ADVANCED:
            self.state.mainline_stall_count = 0
            self.state.last_mainline_verdict = ADVANCED
            self.state.drift_status = NORMAL
        elif found in (STALLED, REGRESSED):
            self.state.mainline_stall_count += 1
            self.state.last_mainline_verdict = found
            self.state.drift_status = (
                REPLAN_REQUIRED
                if self.state.mainline_stall_count >= _REPLAN_AT
                else NORMAL
            )
        if last == COMPLETE:
            self.state.mainline_stall_count = 0
            self.state.last_mainline_verdict = ADVANCED
            self.state.drift_status = NORMAL
        elif last != STOP and self.state.mainline_stall_count >= _STOP_AT:
            await self._write_state()
            await self._ends("stop")
            return render(
                blocks.MAINLINE_DRIFT_STOP,
                STALL_COUNT=self.state.mainline_stall_count,
                LAST_VERDICT=self.state.last_mainline_verdict,
                PLAN_FILE=self.state.plan_file,
            )
        await self._write_state()
        return None

    async def _complete(self) -> str | None:
        if self.state.current_round >= self.state.max_iterations:
            return await self._analyse(
                "maxiter",
                f"Review confirmed COMPLETE but at max iterations "
                f"({self.state.max_iterations})",
            )
        if not self.state.base_branch:
            return await self._finalize("No base_branch configured for code review")
        self.state.review_started = True
        self.state.mainline_stall_count = 0
        self.state.last_mainline_verdict = ADVANCED
        self.state.drift_status = NORMAL
        await self._write_state()
        await write(
            self.env,
            self.where / REVIEW_STARTED,
            f"build_finish_round={self.state.current_round}\n",
        )
        return await self._code_review()

    async def _code_review(self) -> str | None:
        at = self.state.current_round + 1
        base = self.state.base_commit or self.state.base_branch
        asked = render(
            prompts.CODE_REVIEW,
            REVIEW_ROUND=at,
            BASE_BRANCH=self.state.base_branch,
            BASE_COMMIT=self.state.base_commit or "N/A",
            REVIEW_BASE=base,
            REVIEW_BASE_TYPE="commit" if self.state.base_commit else "branch",
            TIMESTAMP=utc(),
        )
        await write(self.env, self.where / f"round-{at}-review-prompt.md", asked)
        said, failed = await self._reviewed(asked, "code review")
        if not failed and not said.strip():
            failed = "the code review answered nothing"
        if failed:
            return render(
                blocks.REVIEW_FAILED,
                FAILURE_REASON=failed,
                ROUND_NUMBER=at,
                BASE_BRANCH=self.state.base_branch,
            )
        found = issues(said)
        if not found:
            return await self._finalize("")
        await write(self.env, self.where / f"round-{at}-review-result.md", said)
        self.state.current_round = at
        await self._write_state()
        await self._scaffold(at)
        asked = render(
            prompts.REVIEW_PHASE,
            REVIEW_CONTENT=found,
            SUMMARY_FILE=self.summary,
            PLAN_FILE=self.state.plan_file,
            GOAL_TRACKER_FILE=self.tracker,
            ROUND_CONTRACT_FILE=self.contract,
            CURRENT_ROUND=at,
        )
        if self.state.bitlesson_required and "BitLesson" not in asked:
            asked += render(
                prompts.REVIEW_PHASE_BITLESSON, BITLESSON_FILE=self._bitlesson
            )
        asked += prompts.ROUND_ROUTING_NOTE
        await write(self.env, self.prompt, asked)
        return asked

    async def _finalize(self, skipped: str) -> str:
        await self._rename(FINALIZING)
        self.finalizing = True
        asked = render(
            prompts.FINALIZE_SKIPPED if skipped else prompts.FINALIZE,
            REVIEW_SKIP_REASON=skipped,
            FINALIZE_SUMMARY_FILE=self.summary,
            PLAN_FILE=self.state.plan_file,
            GOAL_TRACKER_FILE=self.tracker,
            BASE_BRANCH=self.state.base_branch,
            START_BRANCH=self.state.start_branch,
        )
        await write(self.env, self.prompt, asked)
        return asked

    async def _next_round(self, said: str, *, aligning: bool) -> str:
        at = self.state.current_round + 1
        self.state.current_round = at
        await self._write_state()
        await self._scaffold(at)
        replanning = self.state.drift_status == REPLAN_REQUIRED
        asked = render(
            prompts.DRIFT_REPLAN if replanning else prompts.NEXT_ROUND,
            PLAN_FILE=self.state.plan_file,
            REVIEW_CONTENT=said,
            GOAL_TRACKER_FILE=self.tracker,
            BITLESSON_FILE=self._bitlesson,
            ROUND_CONTRACT_FILE=self.contract,
            CURRENT_ROUND=at,
            STALL_COUNT=self.state.mainline_stall_count,
            LAST_MAINLINE_VERDICT=self.state.last_mainline_verdict,
        )
        if replanning and self.state.bitlesson_required and "BitLesson" not in asked:
            asked += render(
                prompts.REVIEW_PHASE_BITLESSON, BITLESSON_FILE=self._bitlesson
            )
        if self.state.agent_teams:
            asked = _injected(asked, prompts.AGENT_TEAMS_ENFORCEMENT)
        if self.state.ask_codex_question and _asks_a_question(said):
            asked = asked.replace(
                "<!-- REVIEWER's REVIEW RESULT  END  -->\n---",
                "<!-- REVIEWER's REVIEW RESULT  END  -->\n---\n\n"
                + prompts.OPEN_QUESTION_NOTICE,
                1,
            )
        if aligning:
            asked += prompts.POST_ALIGNMENT_ACTION_ITEMS
        asked += render(prompts.NEXT_ROUND_FOOTER, NEXT_SUMMARY_FILE=self.summary)
        asked += prompts.ROUND_ROUTING_NOTE
        if self.state.push_every_round:
            asked += prompts.PUSH_EVERY_ROUND_NOTE
        asked += prompts.GOAL_TRACKER_UPDATE_REQUEST
        if self.state.agent_teams and not self.state.review_started:
            asked += (
                "\n" + prompts.AGENT_TEAMS_CONTINUE + "\n" + prompts.AGENT_TEAMS_CORE
            )
        await write(self.env, self.prompt, asked)
        return asked

    async def _review_prompt(self, *, aligning: bool) -> str:
        at = self.state.current_round
        history = await self._commits()
        recent = (
            "".join(
                f"- @{self.where}/round-{r}-summary.md\n"
                f"- @{self.where}/round-{r}-review-result.md\n"
                for r in range(at - 1, max(at - 1 - _RECENT, -1), -1)
            )
            or "(first round, no prior history)"
        )
        section = render(
            prompts.COMMIT_HISTORY_SECTION,
            COMMIT_HISTORY=history,
            RECENT_ROUND_FILES=recent,
        )
        return render(
            prompts.FULL_ALIGNMENT_REVIEW if aligning else prompts.REGULAR_REVIEW,
            CURRENT_ROUND=at,
            PLAN_FILE=self.state.plan_file,
            PROMPT_FILE=self.prompt,
            SUMMARY_CONTENT=await read(self.env, self.summary) or "",
            GOAL_TRACKER_FILE=self.tracker,
            DOCS_PATH="docs",
            GOAL_TRACKER_UPDATE_SECTION=render(
                prompts.GOAL_TRACKER_UPDATE_SECTION, GOAL_TRACKER_FILE=self.tracker
            ),
            COMMIT_HISTORY_SECTION=section,
            COMPLETED_ITERATIONS=at + 1,
            LOOP_DIR=self.where,
            PREV_ROUND=max(at - 1, 0),
            PREV_PREV_ROUND=max(at - 2, 0),
            REVIEW_RESULT_FILE=self.result,
        )

    async def _commits(self) -> str:
        base = self.state.base_commit
        if base:
            status, _ = await git(self.env, "merge-base", "--is-ancestor", base, "HEAD")
            if status == 0:
                _, said = await git(
                    self.env,
                    "log",
                    "--oneline",
                    "--no-decorate",
                    "--reverse",
                    f"{base}..HEAD",
                )
                return "\n".join(said.splitlines()[-80:]) or "(no commits yet)"
        _, said = await git(
            self.env, "log", "--oneline", "--no-decorate", "--reverse", "-30"
        )
        if not said:
            return "(no commits yet)"
        return f"(base commit unavailable, showing recent branch commits)\n{said}"

    async def _delta(self, summary: str) -> str | None:
        found = _DELTA.search(summary)
        if found is None:
            return blocks.BITLESSON_DELTA_MISSING
        block = summary[found.end() :].split("\n## ", maxsplit=1)[0]
        action = _ACTION.search(block)
        named = action.group(1).lower() if action else ""
        if named not in ("none", "add", "update"):
            return blocks.BITLESSON_DELTA_INVALID
        lessons = _LESSONS.search(block)
        said = (lessons.group(1) if lessons else "").strip()
        kept = self._bitlesson
        held = await read(self.env, kept)
        known = _LESSON_ID.findall(held) if held is not None else []
        if named == "none":
            if said and said.upper() != "NONE":
                return render(blocks.BITLESSON_DELTA_INCONSISTENT, BITLESSON_FILE=kept)
            if not known and not self.state.bitlesson_allow_empty_none:
                return render(blocks.BITLESSON_DELTA_EMPTY_KB, BITLESSON_FILE=kept)
            return None
        if not said or said.upper() == "NONE":
            return render(blocks.BITLESSON_DELTA_MISSING_IDS, ACTION=named)
        notes = _NOTES.search(block)
        wrote = (notes.group(1) if notes else "").strip()
        if not wrote or _UNWRITTEN.match(wrote):
            return render(blocks.BITLESSON_DELTA_MISSING_NOTES, ACTION=named)
        if held is None:
            return render(blocks.BITLESSON_FILE_MISSING, ACTION=named)
        wanted = [one.strip() for one in said.split(",") if one.strip()]
        if any(one not in known for one in wanted):
            return render(blocks.BITLESSON_DELTA_INCONSISTENT, BITLESSON_FILE=kept)
        return None

    @property
    def state_file(self) -> PurePosixPath:
        if self.analysing:
            return self.where / ANALYSING
        return self.where / (FINALIZING if self.finalizing else BUILDING)

    @property
    def _bitlesson(self) -> PurePosixPath:
        return self.root / self.state.bitlesson_file

    @property
    def summary(self) -> PurePosixPath:
        if self.finalizing:
            return self.where / "finalize-summary.md"
        return self.where / f"round-{self.state.current_round}-summary.md"

    @property
    def contract(self) -> PurePosixPath:
        return self.where / f"round-{self.state.current_round}-contract.md"

    @property
    def prompt(self) -> PurePosixPath:
        if self.analysing:
            return self.where / "methodology-analysis-prompt.md"
        if self.finalizing:
            return self.where / "finalize-prompt.md"
        return self.where / f"round-{self.state.current_round}-prompt.md"

    @property
    def review_prompt(self) -> PurePosixPath:
        return self.where / f"round-{self.state.current_round}-review-prompt.md"

    @property
    def result(self) -> PurePosixPath:
        return self.where / f"round-{self.state.current_round}-review-result.md"

    @property
    def tracker(self) -> PurePosixPath:
        return self.where / "goal-tracker.md"

    async def _written(self, path: PurePosixPath) -> bool:
        return bool((await read(self.env, path) or "").strip())

    async def _scaffold(self, at: int) -> None:
        summary = self.where / f"round-{at}-summary.md"
        if await read(self.env, summary) is None:
            await write(
                self.env, summary, render(prompts.ROUND_SUMMARY_TEMPLATE, ROUND=at)
            )

    async def _write_state(self) -> None:
        await write(self.env, self.state_file, self.state.written())
        if self.kept is not None:
            self.kept["rounds"] = self.state.current_round

    async def _rename(self, to: str) -> None:
        was = self.state_file
        if await read(self.env, was) is None:
            return
        if failed := await move(self.env, was, self.where / to):
            raise RuntimeError(f"could not rename {was} to {to}: {failed}")

    def _left(self) -> str:
        if self._status is None:
            return ""
        return "\n".join(
            row for row in self._status.splitlines() if not _OURS.match(row)
        )

    async def _analyse(self, reason: str, about: str) -> str | None:
        done = self.where / "methodology-analysis-done.md"
        if (
            self.state.privacy_mode
            or await read(self.env, self.where / ANALYSING) is not None
            or await self._written(done)
        ):
            await self._ends(reason)
            return None
        await self._rename(ANALYSING)
        self.analysing, self.exit_reason = True, reason
        await write(self.env, self.where / EXIT_REASON, reason)
        await write(self.env, done, "")
        asked = render(
            prompts.METHODOLOGY_ANALYSIS,
            EXIT_REASON=reason,
            EXIT_REASON_DESCRIPTION=about,
            CURRENT_ROUND=self.state.current_round,
            MAX_ITERATIONS=self.state.max_iterations,
            LOOP_DIR=self.where,
        )
        await write(self.env, self.prompt, asked)
        return asked

    async def _ends(self, reason: str) -> None:
        self.over = reason if reason in ALLOWED else "unexpected"
        await self._rename(f"{self.over}-state.md")
        await remove(self.env, self.where / EXIT_REASON)


def _last(said: str) -> str:
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _section(held: str, heading: str) -> str:
    found: list[str] = []
    taking = False
    for line in held.splitlines():
        if line.startswith(heading):
            taking = True
            continue
        if taking and line.startswith("##"):
            break
        if taking:
            found.append(line)
    return "\n".join(found)


def _injected(asked: str, enforcement: str) -> str:
    heading = "## Original Implementation Plan"
    if heading in asked:
        return asked.replace(heading, f"\n{enforcement}\n\n{heading}", 1)
    return f"{asked}\n{enforcement}\n"


def _asks_a_question(said: str) -> bool:
    return any(
        len(line) < _HEADING and "Open Question" in line for line in said.splitlines()
    )


def utc() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_LANE = re.compile(r"^\s*\[(mainline|blocking|queued)\](?:\s|$)", re.IGNORECASE)


def _lane(*parts: str) -> str:
    for part in parts:
        found = _LANE.match(part or "")
        if found:
            return found.group(1).lower()
    return "blocking"


def directory(root: PurePosixPath, stamp: str) -> PurePosixPath:
    return root / LOOPS / stamp


def started() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
