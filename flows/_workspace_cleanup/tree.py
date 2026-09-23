"""Deterministic tree work: listing, manifest, measures, revert point, git, check.

Every file the flow reasons about is a file git would add: `.gitignore` is honoured, so
ignored build outputs, virtual environments and secrets are never counted as strays, never
copied into a revert point and never committed.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

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
# A neutral identity, no hooks and no signing: the user's git config must not decide
# whether a cleaning can commit.
_COMMITTING = (
    "-c",
    f"user.name={AUTHOR_NAME}",
    "-c",
    f"user.email={AUTHOR_EMAIL}",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "commit.gpgsign=false",
)
CHECK_SECONDS = 3600


class Measure(NamedTuple):
    """What the flow counts for itself after a cleaning."""

    strays: list[str]
    notes_lines: int
    comment_count: int


class Footprint(NamedTuple):
    """What one revert point copies: the listed tree plus .git."""

    files: int
    bytes: int


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )


def listed(root: Path) -> list[str]:
    """Every entry git would add under root, sorted, whatever state root/.git is in.

    Read against an empty scratch repository, so the answer does not depend on what the
    agents did to the repository's own index or history. Symlinks are listed, never
    entered; a nested repository is listed once, as its directory.
    """
    with tempfile.TemporaryDirectory(prefix="cleanup-listing-") as scratch:
        index = Path(scratch) / "index.git"
        made = _git("init", "--quiet", "--bare", str(index))
        if made.returncode:
            raise RuntimeError(f"git could not list the tree: {made.stderr.strip()}")
        done = _git(
            f"--git-dir={index}",
            f"--work-tree={root}",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
    if done.returncode:
        raise RuntimeError(f"git could not list the tree: {done.stderr.strip()}")
    return sorted(entry.rstrip("/") for entry in done.stdout.split("\0") if entry)


def footprint(root: Path) -> Footprint:
    """How many files and bytes a revert point of this tree copies."""
    files = size = 0
    for rel in listed(root):
        path = root / rel
        if path.is_dir() and not path.is_symlink():
            for directory, _dirs, names in os.walk(path):
                files += len(names)
                size += sum(_size(Path(directory) / name) for name in names)
        else:
            files += 1
            size += _size(path)
    git = root / ".git"
    if git.is_dir() and not git.is_symlink():
        for directory, _dirs, names in os.walk(git):
            files += len(names)
            size += sum(_size(Path(directory) / name) for name in names)
    return Footprint(files, size)


def _size(path: Path) -> int:
    try:
        return path.lstat().st_size
    except OSError:
        return 0


def manifest_path(store: Path) -> Path:
    """The task-file manifest inside the Humanize-managed run root."""
    return store / "manifest.txt"


def ensure_manifest(root: Path, store: Path) -> set[str]:
    """Record the task-provided files once inside the managed run root."""
    path = manifest_path(store)
    if path.is_symlink():
        raise RuntimeError(f"manifest was replaced by a symlink: {path}")
    if not path.exists():
        path.write_text("\n".join(listed(root)) + "\n", encoding="utf-8")
    kept = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in kept if line.strip()}


# -- measures ----------------------------------------------------------------------


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


def _comment_lines(path: Path) -> int:
    suffix = path.suffix.lower()
    if path.is_symlink() or not path.is_file():
        return 0
    if suffix != ".py" and suffix not in C_SUFFIXES:
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return _py_comment_lines(text) if suffix == ".py" else _c_comment_lines(text)


def under_work_path(relative: str, work_paths: tuple[str, ...]) -> bool:
    return any(
        relative == path or relative.startswith(path + "/") for path in work_paths
    )


def measure(root: Path, manifest: set[str], work_paths: tuple[str, ...]) -> Measure:
    """Strays, NEXT.md's length and work-path comment lines, over the listed tree.

    A stray is an entry neither in the manifest nor under a work path: a task may
    legitimately add files under its work paths, and no rule can tell those from junk,
    so their contents remain the cleaner's judgment alone. A real root NEXT.md is the
    one sanctioned flow output; a symlink there counts as stray.
    """
    entries = listed(root)
    notes = root / NOTES
    strays = [
        rel
        for rel in entries
        if rel not in manifest
        and not (rel == NOTES and not notes.is_symlink())
        and not under_work_path(rel, work_paths)
    ]
    comments = sum(
        _comment_lines(root / rel)
        for rel in entries
        if under_work_path(rel, work_paths)
    )
    return Measure(strays, _notes_lines(root), comments)


def _notes_lines(root: Path) -> int:
    """How many lines NEXT.md holds; 0 when absent or a symlink (never followed)."""
    path = root / NOTES
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def truncate_notes(root: Path, limit: int) -> None:
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


def delete_strays(root: Path, strays: list[str]) -> None:
    """Unlink stray files and symlinks, links never followed; directories stay."""
    for rel in strays:
        path = root / rel
        try:
            if path.is_dir() and not path.is_symlink():
                continue
            path.unlink(missing_ok=True)
        except OSError:
            print(f"could not delete stray {rel}")


# -- the revert point --------------------------------------------------------------


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def save_tree(root: Path, store: Path) -> Path:
    """Create the epoch's revert point, or recover one an interrupted epoch left.

    It holds the listed tree and .git as they are, so a failed check or a failed git
    step can put back exactly what the coding turns left, old history included.
    """
    saved = store / "revert"
    if saved.is_symlink():
        raise RuntimeError(f"revert point was replaced by a symlink: {saved}")
    if saved.is_dir():
        restore_tree(root, saved)
        return saved
    partial = store / "revert.partial"
    if partial.is_symlink():
        raise RuntimeError(f"partial revert was replaced by a symlink: {partial}")
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    for rel in listed(root):
        _copy(root / rel, partial / rel)
    if os.path.lexists(root / ".git"):
        _copy(root / ".git", partial / ".git")
    partial.rename(saved)
    return saved


def restore_tree(root: Path, saved: Path) -> None:
    """Put the listed tree and .git back exactly as the revert point holds them.

    Ignored files are left where they are: they were never saved, so they are never
    taken away.
    """
    emptied: set[Path] = set()
    for rel in listed(root):
        _remove(root / rel)
        emptied.update((root / rel).parents)
    _remove(root / ".git")
    for directory in sorted(emptied, key=lambda path: len(path.parts), reverse=True):
        if directory.is_relative_to(root) and directory != root:
            try:
                directory.rmdir()
            except OSError:
                pass
    shutil.copytree(saved, root, symlinks=True, dirs_exist_ok=True)


def drop_saved(saved: Path) -> None:
    """Delete the revert point once the epoch it guarded is settled."""
    shutil.rmtree(saved, ignore_errors=True)


# -- git -----------------------------------------------------------------------------


def _remove_git_entry(root: Path) -> bool:
    """Remove .git whether directory, gitfile, or symlink; True once it is gone."""
    try:
        _remove(root / ".git")
    except OSError:
        return False
    return not os.path.lexists(root / ".git")


def erase_history(root: Path, epoch: int) -> bool:
    """Replace root's history with one commit of the cleaned tree.

    The history it replaces is not lost: the revert point still holds it, and
    `archive_history` moves it into the run's history once this has succeeded.
    """
    if not _remove_git_entry(root):
        print("the .git entry could not be removed; git was not run")
        return False
    steps = (
        ("-c", "init.defaultBranch=main", "init", "-q"),
        ("add", "-A"),
        (
            *_COMMITTING,
            "commit",
            "-q",
            "--allow-empty",
            "--no-verify",
            "-m",
            f"epoch {epoch}: distilled tree",
        ),
    )
    for step in steps:
        try:
            done = _git(*step, cwd=root)
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode:
            return False
    return True


def archive_history(saved: Path, store: Path, epoch: int) -> Path:
    """Keep the history an epoch replaced, plus the tree the coding turns left.

    The .git the revert point holds moves into ``history/epoch-NNN.git`` under the run
    root, and a last commit records the tree as the coding turns left it, uncommitted
    work included -- so each epoch's archive ends where the next epoch's repository
    begins, and the runs can be stitched back into one history. A .git that was a
    gitfile or a symlink pointed at history that was never deleted, so its archive
    starts empty.
    """
    history = store / "history"
    history.mkdir(exist_ok=True)
    archive = history / f"epoch-{epoch:03d}.git"
    again = 1
    while os.path.lexists(archive):
        archive = history / f"epoch-{epoch:03d}-{again}.git"
        again += 1
    old = saved / ".git"
    if old.is_dir() and not old.is_symlink():
        os.replace(old, archive)
    else:
        made = _git("init", "--quiet", "--bare", str(archive))
        if made.returncode:
            raise RuntimeError(f"could not archive epoch {epoch}: {made.stderr}")
    where = (f"--git-dir={archive}", f"--work-tree={saved}")
    added = _git(*where, "add", "-A")
    committed = _git(
        *where,
        *_COMMITTING,
        "commit",
        "-q",
        "--allow-empty",
        "--no-verify",
        "-m",
        f"epoch {epoch}: the tree before cleaning",
    )
    if added.returncode or committed.returncode:
        print(
            f"epoch {epoch}: the tree before cleaning could not be committed to"
            f" {archive}; its history is archived without it"
        )
    return archive


# -- the check -----------------------------------------------------------------------


def run_check(root: Path, command: str, log: Path) -> bool:
    """Run the configured check in root, held to an hour, its output kept in log.

    The check leads its own process group, and the group is killed and reaped whole
    once the verdict is in, so nothing of it survives to mutate the tree afterwards.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as out:
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=root,
                start_new_session=True,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        except OSError as error:
            out.write(f"the check could not start: {error}\n".encode())
            return False
        try:
            ok = proc.wait(timeout=CHECK_SECONDS) == 0
        except subprocess.TimeoutExpired:
            out.write(f"\nthe check ran past {CHECK_SECONDS} seconds\n".encode())
            ok = False
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
    return ok


def tail(log: Path, lines: int = 20) -> str:
    """The last lines of a check log, for printing next to its verdict."""
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
