from __future__ import annotations

import os
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from hmz.flows import (
    PermissionRequestHookResult,
    PreToolUseHookResult,
    UserPromptSubmitHookResult,
)

from . import blocks
from .loop import git, read
from .prompts import render

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hmz.flows import (
        PermissionRequestHookParams,
        PreToolUseHookParams,
        UserPromptSubmitHookParams,
    )

    from .loop import Here, Loop

__all__ = ["Guard", "Prompted", "tracking"]

_ROUND = re.compile(r"round-(\d+)-(summary|prompt|contract|todos)\.md$", re.IGNORECASE)

_REDIRECT = re.compile(r">>?\s*(\S+)")

_INPLACE = re.compile(
    r"(^|[\s|;&(])(tee|dd|truncate|cp|mv|install|rsync)\b"
    r"|(^|[\s|;&(])(sed|perl|awk)\b[^|;&]*\s-i\b"
)

_PUSH = re.compile(r"\bgit\s+push\b")


class Guard:
    def __init__(self, loop: Loop) -> None:
        self._loop = loop
        self._root = loop.root

    async def __call__(
        self, params: PermissionRequestHookParams
    ) -> PermissionRequestHookResult:
        refused = await self.refused(params.tool, params.input)
        if refused is None:
            return PermissionRequestHookResult()
        return PermissionRequestHookResult(allow=False, reason=refused)

    async def watching(self, params: PreToolUseHookParams) -> PreToolUseHookResult:
        self._loop.todos.seen(params.tool, params.input)
        refused = await self.refused(params.tool, params.input)
        if refused is None:
            return PreToolUseHookResult()
        return PreToolUseHookResult(block=True, reason=refused)

    async def refused(self, tool: str, called: Mapping[str, Any]) -> str | None:
        if tool in ("Bash", "commandExecution"):
            command: Any = called.get("command") or ""
            if isinstance(command, list):
                command = " ".join(str(one) for one in command)
            return self._bash(str(command))
        named = str(called.get("file_path") or called.get("path") or "")
        if not named:
            return None
        if tool in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
            return await self._writes(named, str(called.get("old_string") or ""))
        if tool == "Read":
            return self._reads(named)
        return None

    async def _writes(self, named: str, old: str) -> str | None:
        where = self._at(named)
        base = where.name
        if base.endswith("todos.md") and _ROUND.search(base):
            return blocks.TODOS_FILE_ACCESS
        if base in ("state.md", "finalize-state.md", "methodology-analysis-state.md"):
            return blocks.STATE_FILE_MODIFICATION
        if base == "plan.md" and self._ours(where):
            return blocks.PLAN_BACKUP_PROTECTED
        if where == self._at(str(self._root / self._loop.state.plan_file)):
            return render(
                blocks.PLAN_FILE_MODIFIED,
                PLAN_FILE=self._loop.state.plan_file,
                BACKUP_PATH=self._loop.where / "plan.md",
            )
        if base == "goal-tracker.md":
            return await self._tracker(where, old)
        found = _ROUND.search(base)
        if found is None:
            return None
        at, kind = int(found.group(1)), found.group(2).lower()
        if kind == "prompt":
            return blocks.PROMPT_FILE_WRITE
        if not self._ours(where):
            return render(
                blocks.WRONG_CONTRACT_LOCATION
                if kind == "contract"
                else blocks.WRONG_SUMMARY_LOCATION,
                CORRECT_PATH=self._loop.where / base,
            )
        if at != self._loop.state.current_round:
            return render(
                blocks.WRONG_ROUND_NUMBER,
                ACTION="write",
                CLAUDE_ROUND=at,
                FILE_TYPE=kind,
                CURRENT_ROUND=self._loop.state.current_round,
                CORRECT_PATH=self._loop.where
                / f"round-{self._loop.state.current_round}-{kind}.md",
            )
        return None

    async def _tracker(self, where: PurePosixPath, old: str) -> str | None:
        if where != self._at(str(self._loop.tracker)):
            return render(
                blocks.GOAL_TRACKER_MODIFICATION,
                CURRENT_ROUND=self._loop.state.current_round,
                CORRECT_PATH=self._loop.tracker,
            )
        if self._loop.state.current_round <= 0:
            return None
        held = await read(self._loop.env, self._loop.tracker) or ""
        immutable = held.split("## MUTABLE SECTION")[0]
        if not old or (old.strip() and old.strip() in immutable):
            return render(
                blocks.GOAL_TRACKER_MODIFICATION,
                CURRENT_ROUND=self._loop.state.current_round,
                CORRECT_PATH=self._loop.tracker,
            )
        return None

    def _reads(self, named: str) -> str | None:
        where = self._at(named)
        base = where.name
        if base.endswith("todos.md") and _ROUND.search(base):
            return blocks.TODOS_FILE_ACCESS
        if base == "goal-tracker.md" and self._elsewhere(where):
            return render(
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
        return render(
            blocks.WRONG_ROUND_NUMBER,
            ACTION="read",
            CLAUDE_ROUND=at,
            FILE_TYPE=kind,
            CURRENT_ROUND=self._loop.state.current_round,
            CORRECT_PATH=self._loop.where
            / f"round-{self._loop.state.current_round}-{kind}.md",
        )

    def _bash(self, command: str) -> str | None:
        if not command.strip():
            return None
        if _adds_everything(command):
            return blocks.GIT_ADD_HUMANIZE
        if _PUSH.search(command) and not self._loop.state.push_every_round:
            return blocks.GIT_PUSH
        for word in _touched(command):
            base = word.rsplit("/", 1)[-1]
            if base in (
                "state.md",
                "finalize-state.md",
                "methodology-analysis-state.md",
            ):
                return blocks.STATE_FILE_MODIFICATION
            if base == "plan.md" and ".humanize/rlcr/" in word:
                return blocks.PLAN_BACKUP_PROTECTED
            if base == "goal-tracker.md":
                return render(
                    blocks.GOAL_TRACKER_BASH_WRITE, CORRECT_PATH=self._loop.tracker
                )
            found = _ROUND.search(base)
            if found is None:
                continue
            kind = found.group(2).lower()
            if kind == "todos":
                return blocks.TODOS_FILE_ACCESS
            if kind == "prompt":
                return blocks.PROMPT_FILE_WRITE
            return render(
                blocks.ROUND_CONTRACT_BASH_WRITE
                if kind == "contract"
                else blocks.SUMMARY_BASH_WRITE,
                CORRECT_PATH=self._loop.where
                / f"round-{self._loop.state.current_round}-{kind}.md",
            )
        return None

    def _at(self, named: str) -> PurePosixPath:
        where = PurePosixPath(named)
        if not where.is_absolute():
            where = self._root / where
        return PurePosixPath(os.path.realpath(where))

    def _ours(self, where: PurePosixPath) -> bool:
        return where.is_relative_to(self._at(str(self._loop.where)))

    def _elsewhere(self, where: PurePosixPath) -> bool:
        return ".humanize" in where.parts and not self._ours(where)


class Prompted:
    def __init__(self, loop: Loop) -> None:
        self._loop = loop

    async def __call__(
        self, params: UserPromptSubmitHookParams
    ) -> UserPromptSubmitHookResult:
        if params.prompt == self._loop.continuing:
            return UserPromptSubmitHookResult()
        refused = await self.refused()
        if refused is None:
            return UserPromptSubmitHookResult()
        return UserPromptSubmitHookResult(block=True, reason=refused)

    async def refused(self) -> str | None:
        env = self._loop.env
        status, branch = await git(env, "rev-parse", "--abbrev-ref", "HEAD")
        if status or not branch:
            return None
        state = self._loop.state
        if state.start_branch and branch != state.start_branch:
            return render(
                blocks.BRANCH_CHANGED,
                START_BRANCH=state.start_branch,
                CURRENT_BRANCH=branch,
            )
        if not state.plan_file:
            return None
        return await tracking(env, state.plan_file, tracked=state.plan_tracked)


async def tracking(env: Here, plan_file: str, *, tracked: bool) -> str | None:
    status, _ = await git(env, "ls-files", "--error-unmatch", plan_file)
    if not tracked:
        if status == 0:
            return (
                "Plan file is now tracked in git but the loop was started "
                f"without track_plan_file.\n\nFile: {plan_file}\n\nThe plan "
                "file must remain gitignored during this RLCR loop."
            )
        return None
    if status != 0:
        return (
            "Plan file is no longer tracked in git.\n\nFile: "
            f"{plan_file}\n\nThis RLCR loop was started with track_plan_file, "
            "but the plan file has been removed from git tracking."
        )
    _, dirty = await git(env, "status", "--porcelain", plan_file)
    if dirty:
        return render(
            blocks.PLAN_FILE_UNCOMMITTED,
            PLAN_FILE=plan_file,
            PLAN_GIT_STATUS=dirty,
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
