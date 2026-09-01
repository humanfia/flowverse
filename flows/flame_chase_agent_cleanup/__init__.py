"""Two agents flame-chase a repository task while a cleaner distills the workspace and erases its history.

    hmz exec -f flame_chase_agent_cleanup -a claude -a codex -a claude 'improve the project under solution/'

Two coding agents take turns on the task, each turn a fresh session opened on the
working directory and handed the task verbatim, so every turn reads the repository
rather than any history. On the first run the flow records a manifest of the task's
own files under ~/.flame_chase_agent_cleanup/, keyed by a hash of the working directory's
absolute path; a manifest already there is kept, not remade. Every cleanup_turns
completed coding-agent turns (default 5) a cleaning epoch runs between turns: the
whole tree is saved aside as a revert point, a fresh cleaner session shrinks
the configured work_paths to their essence and distills NEXT.md, and the flow
measures what survived -- every entry not in the manifest counts stray unless it
sits under a configured work path, whose contents are the cleaner's judgment alone
(no rule can tell new task work from junk), with a real NEXT.md at the root the one
sanctioned flow output, NEXT.md against next_lines, comment lines in supported
sources under work_paths
against comment_lines -- handing what is over back to the same session up to repairs
times before truncating NEXT.md and deleting strays itself (a comment overage is
only printed, never mechanically stripped). A repair turn that never lands is
retried a bounded few times and never counted as a repair, and the budget is re-read
after every cleaner turn, so an epoch issues no agent turn past it. A configured
check_command then runs for at most an hour as the leader of its own process group,
killed and reaped whole once the verdict is in; a nonzero exit or a timeout restores
the tree from the revert point, and an empty check_command skips the check step
entirely. Last the commit history is erased -- the .git entry removed whether
directory, gitfile, or symlink, git re-initialized, everything force-added past any
ignore rules and committed once, allow-empty, under a neutral author given on the
git command line; if a git step fails the tree is restored, old history included,
from the revert point. Nothing a successful epoch deletes is archived; the only
subprocesses are git and the check, all else standard library. The run ends when
output tokens summed across the two chasers and the cleaner reach budget million
(10.0 by default); resumable state keeps completed turns,
tokens spent and epochs run, and clears when the budget ends the run.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from hmz.flows import Agent, flow
from pydantic import BaseModel, Field, field_validator

NOTES = "NEXT.md"
C_SUFFIXES = {
    ".cu",
    ".cuh",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".hh",
    ".cxx",
    ".hxx",
    ".hip",
}
AUTHOR_NAME = "cleaner"
AUTHOR_EMAIL = "cleaner@flame.chase"
DELIVERY_TRIES = 3


def _validate_work_paths(value: tuple[str, ...]) -> tuple[str, ...]:
    paths = tuple(Path(raw) for raw in value)
    for path in paths:
        if (
            not path.parts
            or path.is_absolute()
            or path == Path(".")
            or ".." in path.parts
            or ".git" in path.parts
        ):
            raise ValueError("work_paths must be relative paths below the repository")
    if len(set(paths)) != len(paths):
        raise ValueError("work_paths must not contain duplicates")
    for index, path in enumerate(paths):
        for other in paths[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ValueError("work_paths must not overlap")
    return tuple(path.as_posix() for path in paths)


class Agents(NamedTuple):
    """The three seats: two chasers taking turns, one cleaner arriving fresh."""

    first_chaser: Agent
    second_chaser: Agent
    cleaner: Agent


class Config(BaseModel):
    """What the flow can be set up with; unset, it runs on the defaults."""

    model_config = {"extra": "forbid"}

    budget: float = Field(
        default=10.0,
        ge=0,
        description=(
            "millions of output tokens, summed across all agents in this workspace,"
            " that end the run"
        ),
    )
    cleanup_turns: int = Field(
        default=5,
        ge=0,
        description="completed coding-agent turns between cleaning epochs; 0 never cleans",
    )
    work_paths: tuple[str, ...] = Field(
        default=("solution",),
        min_length=1,
        description="relative, non-overlapping files or directories where agents may "
        "create or revise task work",
    )
    next_lines: int = Field(
        default=10,
        ge=1,
        description="the most lines NEXT.md may hold",
    )
    comment_lines: int = Field(
        default=30,
        ge=0,
        description=(
            "cap on total comment lines across supported sources under work_paths;"
            " an overage is printed, never mechanically stripped"
        ),
    )
    repairs: int = Field(
        default=2,
        ge=0,
        description=(
            "times an over-measure is handed back to the same cleaner session before"
            " the flow cuts mechanically"
        ),
    )
    check_command: str = Field(
        default="",
        description=(
            "correctness check run in the working directory after a cleaning, held to"
            " an hour; empty skips the check and the revert"
        ),
    )

    @field_validator("work_paths")
    @classmethod
    def validate_work_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_work_paths(value)


class Cleaned(BaseModel):
    """The cleaner's account of an epoch; every field required, as a shape must be."""

    deleted: list[str] = Field(description="what was deleted, item by item, briefly")
    kept: list[str] = Field(
        description="what was kept as essence, item by item, briefly"
    )
    check_ran: bool = Field(description="whether the correctness check was run")
    check_passed: bool = Field(description="whether the correctness check passed")


class Measure(NamedTuple):
    """What the flow counts for itself after a cleaning."""

    strays: list[str]
    notes_lines: int
    comment_count: int


# -- deterministic tree work: manifest, measures, revert point, git ---------------


def _tree_files(root: Path) -> list[str]:
    """Every entry under root as a sorted relative posix path: regular files, file
    symlinks, and directory symlinks (listed, never entered). Anything named .git is
    left out at every level, whether directory, gitfile, or symlink."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        dirnames[:] = [name for name in dirnames if name != ".git"]
        linked = [name for name in dirnames if (base / name).is_symlink()]
        dirnames[:] = [name for name in dirnames if name not in linked]
        for name in linked + filenames:
            rel = (base / name).relative_to(root)
            if ".git" in rel.parts:
                continue
            found.append(rel.as_posix())
    return sorted(found)


def _manifest_path(root: Path) -> Path:
    """The manifest's place under home, keyed by a hash of root's absolute path."""
    key = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
    return Path.home() / ".flame_chase_agent_cleanup" / key / "manifest.txt"


def _ensure_manifest(root: Path) -> set[str]:
    """Record the task-provided files on the first run; keep a manifest already there."""
    path = _manifest_path(root)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(_tree_files(root)) + "\n", encoding="utf-8")
    kept = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in kept if line.strip()}


def _py_comment_lines(text: str) -> int:
    """Lines carrying a '#' comment, string literals tracked."""
    count = 0
    quote = ""
    commented = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            if commented:
                count += 1
            commented = False
            if len(quote) == 1:
                quote = ""
            i += 1
            continue
        if quote:
            if ch == "\\":
                i += 2
                continue
            if text.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if ch in "\"'":
            run_ = text[i : i + 3]
            quote = run_ if run_ == ch * 3 else ch
            i += len(quote)
            continue
        if ch == "#":
            commented = True
            nl = text.find("\n", i)
            if nl < 0:
                break
            i = nl
            continue
        i += 1
    if commented:
        count += 1
    return count


def _c_comment_lines(text: str) -> int:
    """Lines touched by '//' or '/* ... */', string and character literals tracked."""
    count = 0
    quote = ""
    in_block = False
    commented = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            if commented:
                count += 1
            commented = in_block
            quote = ""
            i += 1
            continue
        if in_block:
            commented = True
            if text.startswith("*/", i):
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if text.startswith("//", i):
            commented = True
            nl = text.find("\n", i)
            if nl < 0:
                break
            i = nl
            continue
        if text.startswith("/*", i):
            commented = True
            in_block = True
            i += 2
            continue
        i += 1
    if commented:
        count += 1
    return count


def _path_comment_lines(base: Path) -> int:
    """Comment lines under one work path; symlinks are never followed."""
    if base.is_symlink() or not base.is_dir():
        if base.is_file() and not base.is_symlink():
            suffix = base.suffix.lower()
            if suffix == ".py" or suffix in C_SUFFIXES:
                try:
                    text = base.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return 0
                return (
                    _py_comment_lines(text)
                    if suffix == ".py"
                    else _c_comment_lines(text)
                )
        return 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            name
            for name in dirnames
            if name != ".git" and not (Path(dirpath) / name).is_symlink()
        ]
        for name in filenames:
            path = Path(dirpath) / name
            suffix = path.suffix.lower()
            if suffix != ".py" and suffix not in C_SUFFIXES:
                continue
            if path.is_symlink():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            total += (
                _py_comment_lines(text) if suffix == ".py" else _c_comment_lines(text)
            )
    return total


def _work_comment_lines(root: Path, work_paths: tuple[str, ...]) -> int:
    """Total comment lines in supported sources under configured work paths."""
    return sum(_path_comment_lines(_safe_work_path(root, path)) for path in work_paths)


def _safe_work_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    current = root
    for part in path.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"work path {relative} crosses symlink {current}")
    return root / path


def _under_work_path(relative: str, work_paths: tuple[str, ...]) -> bool:
    return any(
        relative == path or relative.startswith(path + "/") for path in work_paths
    )


def _stray_files(
    root: Path, manifest: set[str], work_paths: tuple[str, ...]
) -> list[str]:
    """Entries not in the manifest or configured work paths.

    A task may legitimately add files under its work paths, and no rule can tell
    those from junk, so their contents remain the cleaner's judgment alone. A real
    root NEXT.md is the other sanctioned flow output; a symlink counts as stray.
    """
    notes_is_link = (root / NOTES).is_symlink()
    strays = []
    for rel in _tree_files(root):
        if rel in manifest:
            continue
        if rel == NOTES and not notes_is_link:
            continue
        if _under_work_path(rel, work_paths):
            continue
        strays.append(rel)
    return strays


def _notes_lines(root: Path) -> int:
    """How many lines NEXT.md holds; 0 when absent or a symlink (never followed)."""
    path = root / NOTES
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _measure(root: Path, manifest: set[str], work_paths: tuple[str, ...]) -> Measure:
    """Everything the flow measures deterministically after a cleaning."""
    return Measure(
        _stray_files(root, manifest, work_paths),
        _notes_lines(root),
        _work_comment_lines(root, work_paths),
    )


def _truncate_notes(root: Path, limit: int) -> None:
    """Cut NEXT.md to its cap mechanically; a symlink is never followed or written."""
    path = root / NOTES
    if path.is_symlink() or not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > limit:
            path.write_text("\n".join(lines[:limit]) + "\n", encoding="utf-8")
    except OSError:
        print(f"could not truncate {NOTES}")


def _delete_strays(root: Path, strays: list[str]) -> None:
    """Unlink stray entries mechanically -- files and symlinks only, links never
    followed; directories they leave empty are left alone."""
    for rel in strays:
        path = root / rel
        try:
            if path.is_dir() and not path.is_symlink():
                continue
            path.unlink(missing_ok=True)
        except OSError:
            print(f"could not delete stray {rel}")


def _save_tree(root: Path) -> Path:
    """Copy the whole working tree aside, outside itself, as the revert point."""
    keep = Path(tempfile.mkdtemp(prefix="flame_chase_agent_cleanup_revert_"))
    saved = keep / "tree"
    shutil.copytree(root, saved, symlinks=True, ignore_dangling_symlinks=True)
    return saved


def _restore_tree(root: Path, saved: Path) -> None:
    """Put the working tree back exactly as the revert point holds it."""
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    for child in saved.iterdir():
        target = root / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True, ignore_dangling_symlinks=True)
        else:
            shutil.copy2(child, target, follow_symlinks=False)


def _drop_saved(saved: Path) -> None:
    """Delete the revert point; nothing a successful epoch deletes is archived."""
    shutil.rmtree(saved.parent, ignore_errors=True)


def _kill_check_group(proc: subprocess.Popen) -> None:
    """SIGKILL the check's whole process group and reap its leader, so nothing of
    the check survives to mutate the tree afterwards."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.wait(timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_check(root: Path, command: str) -> bool:
    """The configured check in the working directory, held to an hour. It runs as
    the leader of its own process group, and the group is killed and reaped whole
    once the verdict is in -- timeout, failure, or success alike."""
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=root,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        ok = proc.wait(timeout=3600) == 0
    except subprocess.TimeoutExpired:
        ok = False
    _kill_check_group(proc)
    return ok


def _remove_git_entry(root: Path) -> bool:
    """Remove .git whether directory, gitfile, or symlink; True only once it is
    verified gone, so git is never run against a linked repository."""
    target = root / ".git"
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
    except OSError:
        return False
    return not os.path.lexists(target)


def _erase_history(root: Path) -> bool:
    """Remove the .git entry, verify it is gone, then re-initialize and commit once
    under a neutral author on the command line. Everything is force-added past any
    ignore rules, and the commit allows an empty tree its one commit."""
    if not _remove_git_entry(root):
        print("the .git entry could not be removed; git was not run")
        return False
    steps = [
        ["git", "init", "-q"],
        ["git", "add", "-A", "-f"],
        [
            "git",
            "-c",
            f"user.name={AUTHOR_NAME}",
            "-c",
            f"user.email={AUTHOR_EMAIL}",
            "commit",
            "-q",
            "--allow-empty",
            "--author",
            f"{AUTHOR_NAME} <{AUTHOR_EMAIL}>",
            "-m",
            "distilled tree; history erased",
        ],
    ]
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for step in steps:
        try:
            done = subprocess.run(
                step,
                check=False,
                cwd=root,
                env=env,
                timeout=600,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0:
            return False
    return True


# -- the cleaning epoch ------------------------------------------------------------


def _cleaning_prompt(held: Config) -> str:
    """The epoch's opening prompt; a check is spoken of only when one is configured."""
    work_paths = ", ".join(f"`{path}`" for path in held.work_paths)
    parts = [
        (
            "You are this repository's cleaner, arriving with fresh eyes. Shrink it to"
            " its essence for the next engineer; do not improve, optimize, or refactor"
            " anything."
        ),
        (
            f"In the task work under {work_paths}: delete dead code -- code paths"
            " disabled by constant flags, commented-out code blocks, unused imports,"
            " functions and variables, and abandoned alternatives; delete from comments"
            " every experiment record -- what was tried, comparative results, benchmark"
            " numbers, and attempt histories; keep only comments that explain design"
            " intent, each cut to a single line. Never change externally visible behavior;"
            " if unsure whether something is dead, leave it."
        ),
        (
            "In the rest of the repository: delete every file past agents left behind;"
            " leave the task's own files untouched; write exactly one file, NEXT.md at the"
            f" tree's root, of at most {held.next_lines} lines, each line one direction"
            " worth exploring next, distilled from what you read -- no narratives, no"
            " history. Never follow symlinks."
        ),
    ]
    if held.check_command:
        parts.append(
            f"Then run the correctness check {held.check_command!r} and restore"
            " whatever you broke."
        )
    parts.append(
        "Answer in shape: what you deleted, what you kept, whether the check ran and"
        " passed."
    )
    return "\n\n".join(parts)


def _overages(found: Measure, held: Config) -> list[str]:
    """Each over-measure named exactly, for the repair prompt and the transcript."""
    overs = [f"stray file, not among the task's own: {rel}" for rel in found.strays]
    if found.notes_lines > held.next_lines:
        overs.append(
            f"NEXT.md holds {found.notes_lines} lines; the cap is {held.next_lines}"
        )
    if found.comment_count > held.comment_lines:
        overs.append(
            f"comment lines across configured work paths: {found.comment_count}; the cap"
            f" is {held.comment_lines}"
        )
    return overs


def _repair_prompt(overs: list[str], held: Config) -> str:
    listed = "\n".join(f"- {over}" for over in overs)
    work_paths = ", ".join(f"`{path}`" for path in held.work_paths)
    return (
        "You are still this repository's cleaner, held to the same rules. What"
        " survived your cleaning measures over; named exactly:\n"
        f"{listed}\n"
        "Cut again: delete stray files past agents left behind, hold NEXT.md to at"
        f" most {held.next_lines} lines, and thin comments under {work_paths} to"
        " single-line design intent. Never change externally visible behavior."
    )


def _clean_epoch(
    cleaner: Agent,
    held: Config,
    root: Path,
    manifest: set[str],
    epoch: int,
    over_budget: Callable[[], bool],
) -> None:
    """One cleaning epoch: save aside, clean, measure and repair, check, erase
    history. over_budget() persists spending and, once true, no further agent turn
    is issued -- the epoch finishes deterministically."""
    print(f"epoch {epoch}: saving the whole tree aside as the revert point")
    saved = _save_tree(root)
    try:
        session = cleaner.new(cwd=str(root))
        report = session(_cleaning_prompt(held), suppress=True, schema=Cleaned)
        ended = over_budget()
        if report is None:
            print(f"epoch {epoch}: the cleaner answered nothing usable")
        else:
            said_check = (
                "ran and passed"
                if report.check_ran and report.check_passed
                else "ran and failed"
                if report.check_ran
                else "did not run"
            )
            print(
                f"epoch {epoch}: cleaner deleted -- "
                + ("; ".join(report.deleted) or "nothing")
            )
            print(
                f"epoch {epoch}: cleaner kept -- "
                + ("; ".join(report.kept) or "nothing")
            )
            print(f"epoch {epoch}: cleaner says its check {said_check}")

        found = _measure(root, manifest, held.work_paths)
        overs = _overages(found, held)
        print(
            f"epoch {epoch}: measured {len(found.strays)} stray file(s), NEXT.md at"
            f" {found.notes_lines} line(s), {found.comment_count} comment line(s) in"
            f" configured work paths -- {len(overs)} over"
        )
        used = 0
        while overs and used < held.repairs and not ended:
            print(
                f"epoch {epoch}: handing {len(overs)} over-measure(s) back, repair"
                f" {used + 1} of {held.repairs}"
            )
            landed = False
            for _ in range(DELIVERY_TRIES):
                said = session(_repair_prompt(overs, held), suppress=True)
                ended = over_budget()
                if said:
                    landed = True
                    break
                if ended:
                    break
                print(
                    f"epoch {epoch}: a repair turn never landed; resting, then retrying"
                )
                time.sleep(5)
            if not landed:
                print(
                    f"epoch {epoch}: repair delivery gave out; falling to the mechanical cut"
                )
                break
            used += 1
            found = _measure(root, manifest, held.work_paths)
            overs = _overages(found, held)
        if overs:
            print(f"epoch {epoch}: the flow cuts mechanically")
            _delete_strays(root, found.strays)
            _truncate_notes(root, held.next_lines)
            if found.comment_count > held.comment_lines:
                print(
                    f"epoch {epoch}: comment lines still {found.comment_count} against"
                    f" a cap of {held.comment_lines} -- printed only, since no rule can"
                    " tell a design comment from a narrative one"
                )
        elif used:
            print(f"epoch {epoch}: within every cap after {used} repair(s)")
        else:
            print(f"epoch {epoch}: within every cap, no repairs needed")

        if held.check_command:
            if _run_check(root, held.check_command):
                print(f"epoch {epoch}: the check passed; the cleaning stands")
            else:
                _restore_tree(root, saved)
                print(
                    f"epoch {epoch}: the check failed -- this epoch's cleaning was reverted"
                )
        else:
            print(f"epoch {epoch}: no check configured; the check step is skipped")

        erased = _erase_history(root)
    except BaseException:
        print(f"epoch {epoch}: interrupted; the revert point survives at {saved}")
        raise
    if erased:
        print(f"epoch {epoch}: commit history erased; one commit stands")
        _drop_saved(saved)
        return
    try:
        _restore_tree(root, saved)
    except BaseException:
        print(
            f"epoch {epoch}: git failed and the restore broke; the revert point survives at {saved}"
        )
        raise
    print(
        f"epoch {epoch}: a git step failed; the tree, old history included, was"
        " restored from the revert point"
    )
    _drop_saved(saved)


# -- the chase ----------------------------------------------------------------------


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    held = config or Config()
    kept: dict[str, Any] = state if state is not None else {}
    kept.setdefault("turns", 0)
    kept.setdefault("epoch", 0)
    before = int(kept.get("spent", 0))

    root = Path.cwd()
    manifest = _ensure_manifest(root)
    next_chaser = kept["turns"] % 2
    print(
        f"chasing in {root}: {len(manifest)} task file(s) in the manifest, starting at"
        f" turn {kept['turns'] + 1} with chaser {next_chaser + 1}, "
        f"epoch {kept['epoch']}"
    )

    chasers = (agents.first_chaser, agents.second_chaser)
    announced = False

    def spent_all() -> int:
        return before + int(sum(seat.spent().output for seat in agents))

    def over_budget() -> bool:
        nonlocal announced
        kept["spent"] = spent = spent_all()
        over = spent >= held.budget * 1_000_000
        if over and not announced:
            announced = True
            print(
                f"the budget is reached at {spent / 1e6:.2f}M output tokens; no"
                " further agent turns"
            )
        return over

    while True:
        kept["spent"] = spent = spent_all()
        if spent >= held.budget * 1_000_000:
            print(
                f"budget ends the run: {spent / 1e6:.2f}M of {held.budget:g}M output"
                f" tokens after {kept['turns']} turn(s) and {kept['epoch']} epoch(s)"
            )
            kept.clear()
            return

        if held.cleanup_turns and kept["turns"] // held.cleanup_turns > kept["epoch"]:
            _clean_epoch(
                agents.cleaner, held, root, manifest, kept["epoch"] + 1, over_budget
            )
            kept["epoch"] += 1
            continue

        turn = kept["turns"] % 2
        session = chasers[turn].new(cwd=str(root))
        said = session(task, suppress=True)
        del session  # dropping the session is how the chase forgets
        if not said:
            print(
                f"turn {kept['turns'] + 1}: chaser {turn + 1}'s turn never landed;"
                " taking it again"
            )
            time.sleep(5)
            continue

        kept["turns"] += 1
        kept["spent"] = spent = spent_all()
        print(
            f"turn {kept['turns']} done by chaser {turn + 1} | epoch {kept['epoch']} |"
            f" {spent / 1e6:.2f}M output tokens spent"
        )
        time.sleep(5)
