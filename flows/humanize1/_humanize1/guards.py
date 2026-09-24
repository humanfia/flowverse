from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hmz.flows import Verdict

from . import blocks
from .prompts import render

if TYPE_CHECKING:
    from pathlib import Path

    from hmz.flows import Occasion

    from .loop import Loop

__all__ = ["Guard", "Prompted"]

_ROUND = re.compile(r"round-(\d+)-(summary|prompt|contract|todos)\.md$", re.IGNORECASE)

_REDIRECT = re.compile(r">>?\s*(\S+)")

_INPLACE = re.compile(
    r"(^|[\s|;&(])(tee|dd|truncate|cp|mv|install|rsync)\b"
    r"|(^|[\s|;&(])(sed|perl|awk)\b[^|;&]*\s-i\b"
)

_PUSH = re.compile(r"\bgit\s+push\b")


class Guard:
    def __init__(self, loop: Loop, root: Path) -> None:
        self._loop = loop
        self._root = root

    def __call__(self, occasion: Occasion) -> Verdict | None:
        called = occasion.input
        tool = occasion.tool
        if tool == "Bash":
            return self._bash(str(called.get("command") or ""))
        named = str(called.get("file_path") or called.get("path") or "")
        if not named:
            return None
        if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
            return self._writes(named, str(called.get("old_string") or ""))
        if tool == "Read":
            return self._reads(named)
        return None

    def _writes(self, named: str, old: str) -> Verdict | None:
        where = self._at(named)
        base = where.name
        if base.endswith("todos.md") and _ROUND.search(base):
            return self._refuse(blocks.TODOS_FILE_ACCESS)
        if base in ("state.md", "finalize-state.md", "methodology-analysis-state.md"):
            return self._refuse(blocks.STATE_FILE_MODIFICATION)
        if base == "plan.md" and self._ours(where):
            return self._refuse(blocks.PLAN_BACKUP_PROTECTED)
        if where == (self._root / self._loop.state.plan_file).resolve():
            return self._refuse(
                blocks.PLAN_FILE_MODIFIED,
                PLAN_FILE=self._loop.state.plan_file,
                BACKUP_PATH=self._loop.where / "plan.md",
            )
        if base == "goal-tracker.md":
            return self._tracker(where, old)
        found = _ROUND.search(base)
        if found is None:
            return None
        at, kind = int(found.group(1)), found.group(2).lower()
        if kind == "prompt":
            return self._refuse(blocks.PROMPT_FILE_WRITE)
        if not self._ours(where):
            return self._refuse(
                blocks.WRONG_CONTRACT_LOCATION
                if kind == "contract"
                else blocks.WRONG_SUMMARY_LOCATION,
                CORRECT_PATH=self._loop.where / base,
            )
        if at != self._loop.state.current_round:
            return self._refuse(
                blocks.WRONG_ROUND_NUMBER,
                ACTION="write",
                CLAUDE_ROUND=at,
                FILE_TYPE=kind,
                CURRENT_ROUND=self._loop.state.current_round,
                CORRECT_PATH=self._loop.where
                / f"round-{self._loop.state.current_round}-{kind}.md",
            )
        return None

    def _tracker(self, where: Path, old: str) -> Verdict | None:
        if where != (self._loop.tracker).resolve():
            return self._refuse(
                blocks.GOAL_TRACKER_MODIFICATION,
                CURRENT_ROUND=self._loop.state.current_round,
                CORRECT_PATH=self._loop.tracker,
            )
        if self._loop.state.current_round <= 0:
            return None
        held = _read(where)
        immutable = held.split("## MUTABLE SECTION")[0]
        if not old or (old.strip() and old.strip() in immutable):
            return self._refuse(
                blocks.GOAL_TRACKER_MODIFICATION,
                CURRENT_ROUND=self._loop.state.current_round,
                CORRECT_PATH=self._loop.tracker,
            )
        return None

    def _reads(self, named: str) -> Verdict | None:
        where = self._at(named)
        base = where.name
        if base.endswith("todos.md") and _ROUND.search(base):
            return self._refuse(blocks.TODOS_FILE_ACCESS)
        if base == "goal-tracker.md" and self._elsewhere(where):
            return self._refuse(
                blocks.GOAL_TRACKER_MODIFICATION,
                CURRENT_ROUND=self._loop.state.current_round,
                CORRECT_PATH=self._loop.tracker,
            )
        found = _ROUND.search(base)
        if found is None or not self._elsewhere(where):
            return None
        at, kind = int(found.group(1)), found.group(2).lower()
        if at == self._loop.state.current_round:
            return None
        return self._refuse(
            blocks.WRONG_ROUND_NUMBER,
            ACTION="read",
            CLAUDE_ROUND=at,
            FILE_TYPE=kind,
            CURRENT_ROUND=self._loop.state.current_round,
            CORRECT_PATH=self._loop.where
            / f"round-{self._loop.state.current_round}-{kind}.md",
        )

    def _bash(self, command: str) -> Verdict | None:
        if not command.strip():
            return None
        if _adds_everything(command) and (self._root / ".humanize").exists():
            return self._refuse(blocks.GIT_ADD_HUMANIZE)
        if _PUSH.search(command) and not self._loop.state.push_every_round:
            return self._refuse(blocks.GIT_PUSH)
        for word in _touched(command):
            base = word.rsplit("/", 1)[-1]
            if base in (
                "state.md",
                "finalize-state.md",
                "methodology-analysis-state.md",
            ):
                return self._refuse(blocks.STATE_FILE_MODIFICATION)
            if base == "plan.md" and ".humanize/rlcr/" in word:
                return self._refuse(blocks.PLAN_BACKUP_PROTECTED)
            if base == "goal-tracker.md":
                return self._refuse(
                    blocks.GOAL_TRACKER_BASH_WRITE, CORRECT_PATH=self._loop.tracker
                )
            found = _ROUND.search(base)
            if found is None:
                continue
            kind = found.group(2).lower()
            if kind == "todos":
                return self._refuse(blocks.TODOS_FILE_ACCESS)
            if kind == "prompt":
                return self._refuse(blocks.PROMPT_FILE_WRITE)
            return self._refuse(
                blocks.ROUND_CONTRACT_BASH_WRITE
                if kind == "contract"
                else blocks.SUMMARY_BASH_WRITE,
                CORRECT_PATH=self._loop.where
                / f"round-{self._loop.state.current_round}-{kind}.md",
            )
        return None

    def _at(self, named: str) -> Path:
        from pathlib import Path as _Path

        where = _Path(named)
        if not where.is_absolute():
            where = self._root / where
        try:
            return where.resolve()
        except OSError:
            return where

    def _ours(self, where: Path) -> bool:
        try:
            return where.is_relative_to(self._loop.where.resolve())
        except OSError:
            return False

    def _elsewhere(self, where: Path) -> bool:
        return ".humanize" in where.parts and not self._ours(where)

    @staticmethod
    def _refuse(template: str, **fields: object) -> Verdict:
        return Verdict(refused=True, because=render(template, **fields))


class Prompted:
    def __init__(self, loop: Loop, root: Path) -> None:
        self._loop = loop
        self._root = root

    def __call__(self, occasion: Occasion) -> Verdict | None:  # noqa: ARG002
        from .loop import git

        status, branch = git("rev-parse", "--abbrev-ref", "HEAD", at=self._root)
        if status or not branch:
            return None
        state = self._loop.state
        if state.start_branch and branch != state.start_branch:
            return Verdict(
                refused=True,
                because=render(
                    blocks.BRANCH_CHANGED,
                    START_BRANCH=state.start_branch,
                    CURRENT_BRANCH=branch,
                ),
            )
        if not state.plan_file:
            return None
        tracked, _ = git("ls-files", "--error-unmatch", state.plan_file, at=self._root)
        if not state.plan_tracked:
            if tracked == 0:
                return Verdict(
                    refused=True,
                    because="Plan file is now tracked in git but the loop was started "
                    f"without track_plan_file.\n\nFile: {state.plan_file}\n\nThe plan "
                    "file must remain gitignored during this RLCR loop.",
                )
            return None
        if tracked != 0:
            return Verdict(
                refused=True,
                because="Plan file is no longer tracked in git.\n\nFile: "
                f"{state.plan_file}\n\nThis RLCR loop was started with track_plan_file, "
                "but the plan file has been removed from git tracking.",
            )
        _, dirty = git("status", "--porcelain", state.plan_file, at=self._root)
        if dirty:
            return Verdict(
                refused=True,
                because=render(
                    blocks.PLAN_FILE_UNCOMMITTED,
                    PLAN_FILE=state.plan_file,
                    PLAN_GIT_STATUS=dirty,
                ),
            )
        return None


def _touched(command: str) -> list[str]:
    found = list(_REDIRECT.findall(command))
    if _INPLACE.search(command):
        found.extend(
            word.strip("'\"")
            for word in command.split()
            if not word.startswith("-") and ("/" in word or word.endswith(".md"))
        )
    return found


def _adds_everything(command: str) -> bool:
    return any(_adds(part.split()) for part in re.split(r"[|;&]+", command))


def _adds(words: list[str]) -> bool:
    if "git" not in words:
        return False
    at = words.index("git")
    if words[at + 1 : at + 2] != ["add"]:
        return False
    return any(
        word in ("-A", "--all", ".") or word.removeprefix("./").startswith(".humanize")
        for word in words[at + 2 :]
    )


def _read(where: Path) -> str:
    try:
        return where.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
