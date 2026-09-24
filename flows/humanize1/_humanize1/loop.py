from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from hmz.flows import Verdict

from . import blocks, prompts
from .prompts import render

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from hmz.flows import Agent, Occasion, Profile, Session
    from pydantic import BaseModel

__all__ = [
    "ALLOWED",
    "COMPLETE",
    "MAX_LINES",
    "STOP",
    "Loop",
    "State",
    "answered",
    "git",
    "issues",
    "spoken",
    "verdict",
]

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


def spoken(agent: Agent | Session, prompt: str) -> tuple[str, float]:
    began = time.monotonic()
    while True:
        said = agent(prompt, suppress=True)
        if said:
            return said, time.monotonic() - began
        time.sleep(5)


def answered[T: BaseModel](agent: Agent | Session, prompt: str, schema: type[T]) -> T:
    while True:
        said = agent(prompt, suppress=True, schema=schema)
        if said is not None:
            return said
        time.sleep(5)


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


def git(*args: str, at: Path) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", "-C", str(at), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT,
        )
    except (OSError, subprocess.SubprocessError):
        return 124, ""
    return done.returncode, done.stdout.strip()


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
    def read(cls, at: Path) -> State | None:
        try:
            held = at.read_text(encoding="utf-8")
        except OSError:
            return None
        lines = held.splitlines()
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


@dataclass
class Loop:
    reviewer: Agent
    where: Path
    root: Path
    state: State
    said: list[str] = field(default_factory=list[str])
    over: str = ""
    finalizing: bool = False
    analysing: bool = False
    exit_reason: str = ""
    kept: dict[str, Any] | None = None
    _status: str | None = None

    @classmethod
    def picked_up(
        cls,
        reviewer: Agent,
        where: Path,
        root: Path,
        kept: dict[str, Any] | None = None,
    ) -> Loop | None:
        for at, finalizing, analysing in (
            (BUILDING, False, False),
            (FINALIZING, True, False),
            (ANALYSING, False, True),
        ):
            state = State.read(where / at)
            if state is None:
                continue
            return cls(
                reviewer,
                where,
                root,
                state,
                kept=kept,
                finalizing=finalizing,
                analysing=analysing,
                exit_reason=_exiting(where) if analysing else "",
            )
        return None

    def __call__(self, occasion: Occasion) -> Verdict | None:
        gates: tuple[Callable[[Occasion], Verdict | None], ...] = (
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
            refused = gate(occasion)
            if self.over:
                return None
            if refused is not None:
                return refused
        return self._review(occasion)

    def _schema(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        try:
            held = self.state_file.read_text(encoding="utf-8")
        except OSError:
            self._ends("unexpected")
            return None
        for name, kind in (("current_round", int), ("max_iterations", int)):
            found = re.search(rf"^{name}:\s*(\S+)\s*$", held, re.MULTILINE)
            if found is None:
                self._ends("unexpected")
                return None
            with contextlib.suppress(ValueError):
                setattr(self.state, name, kind(found.group(1)))
        return None

    def _branch(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        status, branch = git("rev-parse", "--abbrev-ref", "HEAD", at=self.root)
        if status or not branch:
            return Verdict(
                refused=True,
                because="Git operation failed or timed out.\n\nCannot verify branch "
                "consistency. Please check git status manually and try again.",
            )
        if self.state.start_branch and branch != self.state.start_branch:
            return self._blocks(
                blocks.BRANCH_CHANGED,
                START_BRANCH=self.state.start_branch,
                CURRENT_BRANCH=branch,
            )
        return None

    def _plan_integrity(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if self.state.review_started:
            return None
        backup = self.where / "plan.md"
        plan = self.root / self.state.plan_file
        if not backup.is_file():
            return Verdict(
                refused=True,
                because="Plan file backup not found in loop directory.\n\n"
                f"This backup is required for plan integrity verification: {backup}",
            )
        if not plan.is_file():
            return self._blocks(
                blocks.PLAN_FILE_DELETED,
                PLAN_FILE=self.state.plan_file,
                BACKUP_PATH=backup,
            )
        if self.state.plan_tracked:
            _, dirty = git("status", "--porcelain", self.state.plan_file, at=self.root)
            if dirty:
                return self._blocks(
                    blocks.PLAN_FILE_UNCOMMITTED,
                    PLAN_FILE=self.state.plan_file,
                    PLAN_GIT_STATUS=dirty,
                )
        if plan.read_bytes() != backup.read_bytes():
            return self._blocks(
                blocks.PLAN_FILE_MODIFIED,
                PLAN_FILE=self.state.plan_file,
                BACKUP_PATH=backup,
            )
        return None

    def _todos(self, occasion: Occasion) -> Verdict | None:
        left = _open_tasks(self.reviewer, occasion)
        if not left:
            return None
        return self._blocks(blocks.INCOMPLETE_TODOS, INCOMPLETE_LIST="\n".join(left))

    def _git_status(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        self._status = None
        status, _ = git("rev-parse", "--git-dir", at=self.root)
        if status:
            return None
        failed, everything = git("status", "--porcelain", at=self.root)
        if failed:
            return self._blocks(blocks.GIT_STATUS_FAILED, GIT_STATUS_EXIT=failed)
        self._status = everything
        return None

    def _analysis_phase(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if not self.analysing:
            return None
        done = self.where / "methodology-analysis-done.md"
        report = self.where / "methodology-analysis-report.md"
        if not (_written(done) and _written(report)):
            return Verdict(
                refused=True,
                because="# Methodology Analysis Incomplete\n\nPlease complete the "
                "methodology analysis before exiting.\n\nYou need to:\n"
                f"1. Write the analysis report to {report}\n"
                f"2. Write a completion note to {done}",
            )
        if self._left():
            return self._blocks(
                blocks.GIT_NOT_CLEAN,
                GIT_ISSUES="uncommitted changes after methodology analysis",
                SPECIAL_NOTES="",
            )
        self._ends(self.exit_reason or "unexpected")
        self.analysing = False
        return None

    def _git_clean(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if self._status is None:
            return None
        tracked, held = git("ls-files", "--", ".humanize", at=self.root)
        if not tracked and held:
            return self._blocks(blocks.GIT_TRACKED_HUMANIZE)
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
        return self._blocks(
            blocks.GIT_NOT_CLEAN, GIT_ISSUES="uncommitted changes", SPECIAL_NOTES=notes
        )

    def _unpushed(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if not self.state.push_every_round:
            return None
        _, said = git("status", "-sb", at=self.root)
        ahead = re.search(r"ahead (\d+)", said)
        if ahead is None:
            return None
        _, branch = git("rev-parse", "--abbrev-ref", "HEAD", at=self.root)
        return self._blocks(
            blocks.UNPUSHED_COMMITS,
            AHEAD_COUNT=ahead.group(1),
            CURRENT_BRANCH=branch or "unknown",
        )

    def _large_files(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if self._status is None:
            return None
        found: list[str] = []
        for row in self._status.splitlines():
            named = row[3:].split(" -> ")[-1]
            path = self.root / named
            if not path.is_file():
                continue
            kind = path.suffix.lstrip(".").lower()
            about = (
                "code" if kind in _CODE else "documentation" if kind in _DOCS else ""
            )
            if not about:
                continue
            with contextlib.suppress(OSError, UnicodeDecodeError):
                lines = len(path.read_text(encoding="utf-8").splitlines())
                if lines > MAX_LINES:
                    found.append(f"\n- `{path}`: {lines} lines ({about} file)")
        if not found:
            return None
        return self._blocks(
            blocks.LARGE_FILES, MAX_LINES=MAX_LINES, LARGE_FILES="".join(found)
        )

    def _summary_written(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if _written(self.summary):
            return None
        return self._blocks(blocks.WORK_SUMMARY_MISSING, SUMMARY_FILE=self.summary)

    def _contract_written(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if self.finalizing or self.contract.is_file():
            return None
        return self._blocks(
            blocks.ROUND_CONTRACT_MISSING, ROUND_CONTRACT_FILE=self.contract
        )

    def _bitlesson_delta(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if self.finalizing or not self.state.bitlesson_required:
            return None
        return self._delta(self.summary.read_text(encoding="utf-8"))

    def _goal_tracker_started(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        tracker = self.tracker
        if (
            self.finalizing
            or self.state.review_started
            or self.state.current_round
            or not tracker.is_file()
        ):
            return None
        held = tracker.read_text(encoding="utf-8")
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
        return self._blocks(
            blocks.GOAL_TRACKER_NOT_INITIALIZED,
            GOAL_TRACKER_FILE=tracker,
            MISSING_ITEMS="".join(missing),
        )

    def _max_iterations(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if self.finalizing or self.state.review_started:
            return None
        if self.state.current_round + 1 <= self.state.max_iterations:
            return None
        return self._analyse(
            "maxiter",
            f"Reached max iterations ({self.state.max_iterations}) without completion",
        )

    def _finalize_done(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if not self.finalizing:
            return None
        return self._analyse(
            "complete", "All acceptance criteria met and code review passed"
        )

    def _review(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        if self.state.review_started and not (self.where / REVIEW_STARTED).is_file():
            return Verdict(
                refused=True,
                because="Review phase state inconsistency detected.\n\nThe state file "
                "indicates review_started=true, but no review phase marker exists.\nThis "
                "can happen if the state file was manually edited.\n\n**To fix:**\nReset "
                "the state by stopping the flow and starting it again.",
            )
        aligning = (
            self.state.current_round % self.state.full_review_round
            == self.state.full_review_round - 1
        )
        asked = self._review_prompt(aligning=aligning)
        self.review_prompt.write_text(asked, encoding="utf-8")

        said = ""
        if not self.state.review_started:
            before = (
                self.result.read_text(encoding="utf-8") if self.result.is_file() else ""
            )
            said, took = spoken(self.reviewer, asked)
            if took > self.state.codex_timeout:
                return self._blocks(
                    blocks.REVIEW_FAILED,
                    FAILURE_REASON=f"the review took {took:.0f}s, over the "
                    f"{self.state.codex_timeout}s it was given",
                    ROUND_NUMBER=self.state.current_round,
                    BASE_BRANCH=self.state.base_branch,
                )
            written = (
                self.result.read_text(encoding="utf-8") if self.result.is_file() else ""
            )
            if not written.strip() or written == before:
                self.result.write_text(said, encoding="utf-8")
            said = self.result.read_text(encoding="utf-8")
            if not said.strip():
                return self._blocks(
                    blocks.REVIEW_FAILED,
                    FAILURE_REASON="the review result file is empty",
                    ROUND_NUMBER=self.state.current_round,
                    BASE_BRANCH=self.state.base_branch,
                )
            self.said.append(said)
            if (drifted := self._drift(said)) is not None:
                return drifted
            if _last(said) == COMPLETE:
                return self._complete()
        if self.state.review_started:
            return self._code_review()
        if _last(said) == STOP:
            return self._analyse(
                "stop",
                f"Circuit breaker triggered - stagnation detected at round "
                f"{self.state.current_round}",
            )
        return self._next_round(said, aligning=aligning)

    def _drift(self, said: str) -> Verdict | None:
        last = _last(said)
        found = verdict(said)
        if last != STOP and found == UNKNOWN:
            return self._blocks(
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
            self._write_state()
            self._ends("stop")
            return self._blocks(
                blocks.MAINLINE_DRIFT_STOP,
                STALL_COUNT=self.state.mainline_stall_count,
                LAST_VERDICT=self.state.last_mainline_verdict,
                PLAN_FILE=self.state.plan_file,
            )
        self._write_state()
        return None

    def _complete(self) -> Verdict | None:
        if self.state.current_round >= self.state.max_iterations:
            return self._analyse(
                "maxiter",
                f"Review confirmed COMPLETE but at max iterations "
                f"({self.state.max_iterations})",
            )
        if not self.state.base_branch:
            return self._finalize("No base_branch configured for code review")
        self.state.review_started = True
        self.state.mainline_stall_count = 0
        self.state.last_mainline_verdict = ADVANCED
        self.state.drift_status = NORMAL
        self._write_state()
        (self.where / REVIEW_STARTED).write_text(
            f"build_finish_round={self.state.current_round}\n", encoding="utf-8"
        )
        return self._code_review()

    def _code_review(self) -> Verdict | None:
        at = self.state.current_round + 1
        base = self.state.base_commit or self.state.base_branch
        asked = render(
            prompts.CODE_REVIEW,
            REVIEW_ROUND=at,
            BASE_BRANCH=self.state.base_branch,
            BASE_COMMIT=self.state.base_commit or "N/A",
            REVIEW_BASE=base,
            REVIEW_BASE_TYPE="commit" if self.state.base_commit else "branch",
            TIMESTAMP=_stamp(),
        )
        (self.where / f"round-{at}-review-prompt.md").write_text(
            asked, encoding="utf-8"
        )
        said, took = spoken(self.reviewer, asked)
        if took > self.state.codex_timeout:
            return self._blocks(
                blocks.REVIEW_FAILED,
                FAILURE_REASON=f"the code review took {took:.0f}s, over the "
                f"{self.state.codex_timeout}s it was given",
                ROUND_NUMBER=at,
                BASE_BRANCH=self.state.base_branch,
            )
        found = issues(said)
        if not found:
            return self._finalize("")
        (self.where / f"round-{at}-review-result.md").write_text(said, encoding="utf-8")
        self.said.append(said)
        self.state.current_round = at
        self._write_state()
        self._scaffold(at)
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
        self.prompt.write_text(asked, encoding="utf-8")
        return Verdict(refused=True, because=asked)

    def _finalize(self, skipped: str) -> Verdict:
        self._rename(FINALIZING)
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
        self.prompt.write_text(asked, encoding="utf-8")
        return Verdict(refused=True, because=asked)

    def _next_round(self, said: str, *, aligning: bool) -> Verdict:
        at = self.state.current_round + 1
        self.state.current_round = at
        self._write_state()
        self._scaffold(at)
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
        self.prompt.write_text(asked, encoding="utf-8")
        return Verdict(refused=True, because=asked)

    def _review_prompt(self, *, aligning: bool) -> str:
        at = self.state.current_round
        history = self._commits()
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
            SUMMARY_CONTENT=self.summary.read_text(encoding="utf-8"),
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

    def _commits(self) -> str:
        base = self.state.base_commit
        if base:
            status, _ = git("merge-base", "--is-ancestor", base, "HEAD", at=self.root)
            if status == 0:
                _, said = git(
                    "log",
                    "--oneline",
                    "--no-decorate",
                    "--reverse",
                    f"{base}..HEAD",
                    at=self.root,
                )
                return "\n".join(said.splitlines()[-80:]) or "(no commits yet)"
        _, said = git(
            "log", "--oneline", "--no-decorate", "--reverse", "-30", at=self.root
        )
        if not said:
            return "(no commits yet)"
        return f"(base commit unavailable, showing recent branch commits)\n{said}"

    def _delta(self, summary: str) -> Verdict | None:
        found = _DELTA.search(summary)
        if found is None:
            return self._blocks(blocks.BITLESSON_DELTA_MISSING)
        block = summary[found.end() :].split("\n## ", maxsplit=1)[0]
        action = _ACTION.search(block)
        named = action.group(1).lower() if action else ""
        if named not in ("none", "add", "update"):
            return self._blocks(blocks.BITLESSON_DELTA_INVALID)
        lessons = _LESSONS.search(block)
        said = (lessons.group(1) if lessons else "").strip()
        kept = self.root / self.state.bitlesson_file
        known = (
            _LESSON_ID.findall(kept.read_text(encoding="utf-8"))
            if kept.is_file()
            else []
        )
        if named == "none":
            if said and said.upper() != "NONE":
                return self._blocks(
                    blocks.BITLESSON_DELTA_INCONSISTENT, BITLESSON_FILE=kept
                )
            if not known and not self.state.bitlesson_allow_empty_none:
                return self._blocks(
                    blocks.BITLESSON_DELTA_EMPTY_KB, BITLESSON_FILE=kept
                )
            return None
        if not said or said.upper() == "NONE":
            return self._blocks(blocks.BITLESSON_DELTA_MISSING_IDS, ACTION=named)
        notes = _NOTES.search(block)
        wrote = (notes.group(1) if notes else "").strip()
        if not wrote or _UNWRITTEN.match(wrote):
            return self._blocks(blocks.BITLESSON_DELTA_MISSING_NOTES, ACTION=named)
        if not kept.is_file():
            return self._blocks(blocks.BITLESSON_FILE_MISSING, ACTION=named)
        wanted = [one.strip() for one in said.split(",") if one.strip()]
        if any(one not in known for one in wanted):
            return self._blocks(
                blocks.BITLESSON_DELTA_INCONSISTENT, BITLESSON_FILE=kept
            )
        return None

    @property
    def state_file(self) -> Path:
        if self.analysing:
            return self.where / ANALYSING
        return self.where / (FINALIZING if self.finalizing else BUILDING)

    @property
    def _bitlesson(self) -> Path:
        return self.root / self.state.bitlesson_file

    @property
    def summary(self) -> Path:
        if self.finalizing:
            return self.where / "finalize-summary.md"
        return self.where / f"round-{self.state.current_round}-summary.md"

    @property
    def contract(self) -> Path:
        return self.where / f"round-{self.state.current_round}-contract.md"

    @property
    def prompt(self) -> Path:
        if self.analysing:
            return self.where / "methodology-analysis-prompt.md"
        if self.finalizing:
            return self.where / "finalize-prompt.md"
        return self.where / f"round-{self.state.current_round}-prompt.md"

    @property
    def review_prompt(self) -> Path:
        return self.where / f"round-{self.state.current_round}-review-prompt.md"

    @property
    def result(self) -> Path:
        return self.where / f"round-{self.state.current_round}-review-result.md"

    @property
    def tracker(self) -> Path:
        return self.where / "goal-tracker.md"

    def _scaffold(self, at: int) -> None:
        summary = self.where / f"round-{at}-summary.md"
        if not summary.exists():
            summary.write_text(
                render(prompts.ROUND_SUMMARY_TEMPLATE, ROUND=at), encoding="utf-8"
            )

    def _write_state(self) -> None:
        self.state_file.write_text(self.state.written(), encoding="utf-8")
        if self.kept is not None:
            self.kept["rounds"] = self.state.current_round

    def _rename(self, to: str) -> None:
        was = self.state_file
        if was.exists():
            shutil.move(str(was), str(self.where / to))

    def _left(self) -> str:
        if self._status is None:
            return ""
        return "\n".join(
            row for row in self._status.splitlines() if not _OURS.match(row)
        )

    def _analyse(self, reason: str, about: str) -> Verdict | None:
        done = self.where / "methodology-analysis-done.md"
        if (
            self.state.privacy_mode
            or (self.where / ANALYSING).exists()
            or _written(done)
        ):
            self._ends(reason)
            return None
        self._rename(ANALYSING)
        self.analysing, self.exit_reason = True, reason
        (self.where / EXIT_REASON).write_text(reason, encoding="utf-8")
        done.touch()
        asked = render(
            prompts.METHODOLOGY_ANALYSIS,
            EXIT_REASON=reason,
            EXIT_REASON_DESCRIPTION=about,
            CURRENT_ROUND=self.state.current_round,
            MAX_ITERATIONS=self.state.max_iterations,
            LOOP_DIR=self.where,
        )
        self.prompt.write_text(asked, encoding="utf-8")
        return Verdict(refused=True, because=asked)

    def _ends(self, reason: str) -> None:
        self.over = reason if reason in ALLOWED else "unexpected"
        self._rename(f"{self.over}-state.md")
        with contextlib.suppress(OSError):
            (self.where / EXIT_REASON).unlink(missing_ok=True)

    @staticmethod
    def _blocks(template: str, **fields: object) -> Verdict:
        return Verdict(refused=True, because=render(template, **fields))


def _written(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def _exiting(where: Path) -> str:
    try:
        return (where / EXIT_REASON).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


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


def _stamp() -> str:
    import datetime

    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_tasks(agent: Agent, occasion: Occasion) -> list[str]:
    from hmz.flows import backends

    profile = backends.named(agent.backend)
    if profile is None or not occasion.session:
        return []
    found: list[str] = []
    tasks = profile.directory() / "tasks" / occasion.session
    for path in sorted(tasks.glob("*.json")) if tasks.is_dir() else []:
        task = _json(path)
        if not isinstance(task, dict):
            continue
        held = cast("dict[str, Any]", task)
        status = str(held.get("status") or "pending")
        if status in ("completed", "deleted"):
            continue
        subject = str(held.get("subject") or "")
        about = str(held.get("description") or "")
        lane = _lane(subject, about)
        if lane == "queued":
            continue
        said = subject or about or f"Task {path.stem}"
        found.append(f"  - [{status}] [{lane}] (Task #{path.stem}) {said}")
    for todo in _todo_writes(profile, occasion.session):
        status = str(todo.get("status") or "")
        said = str(todo.get("content") or "")
        if status == "completed":
            continue
        lane = _lane(said)
        if lane == "queued":
            continue
        found.append(f"  - [{status}] [{lane}] {said}")
    return found


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _todo_writes(profile: Profile, session: str) -> list[dict[str, Any]]:
    latest: list[dict[str, Any]] = []
    for pattern in profile.logs:
        for path in sorted(profile.directory().glob(pattern.format(ident=session))):
            try:
                held = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line in held.splitlines():
                if not line.strip():
                    continue
                try:
                    entry: Any = json.loads(line)
                except ValueError:
                    continue
                for name, called in _tool_calls(entry):
                    todos = called.get("todos")
                    if name == "TodoWrite" and isinstance(todos, list):
                        latest = [
                            cast("dict[str, Any]", one)
                            for one in cast("list[Any]", todos)
                            if isinstance(one, dict)
                        ]
    return latest


def _tool_calls(entry: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    if not isinstance(entry, dict):
        return
    said = cast("dict[str, Any]", entry)
    kind = said.get("type")
    content: Any = []
    if kind == "assistant":
        message = said.get("message")
        content = (
            cast("dict[str, Any]", message).get("content", [])
            if isinstance(message, dict)
            else []
        )
    elif kind == "message":
        content = said.get("content", [])
    if isinstance(content, list):
        for raw in cast("list[Any]", content):
            if not isinstance(raw, dict):
                continue
            block = cast("dict[str, Any]", raw)
            if block.get("type") != "tool_use":
                continue
            named = str(block.get("name") or "")
            with_it: Any = block.get("input") or {}
            if named and isinstance(with_it, dict):
                yield named, cast("dict[str, Any]", with_it)
    if kind == "tool_use":
        named = str(said.get("name") or said.get("tool_name") or "")
        with_it = said.get("input") or said.get("tool_input") or {}
        if named and isinstance(with_it, dict):
            yield named, cast("dict[str, Any]", with_it)


_LANE = re.compile(r"^\s*\[(mainline|blocking|queued)\](?:\s|$)", re.IGNORECASE)


def _lane(*parts: str) -> str:
    for part in parts:
        found = _LANE.match(part or "")
        if found:
            return found.group(1).lower()
    return "blocking"


def directory(root: Path, stamp: str) -> Path:
    where = root / LOOPS / stamp
    where.mkdir(parents=True, exist_ok=True)
    return where


def started() -> str:
    import datetime

    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")


def sanitized(root: Path) -> str:
    said = re.sub(r"[^a-zA-Z0-9._-]", "-", str(root))
    return re.sub(r"-{2,}", "-", said)


def cache(root: Path, stamp: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    where = base / "humanize" / sanitized(root) / stamp
    where.mkdir(parents=True, exist_ok=True)
    return where
