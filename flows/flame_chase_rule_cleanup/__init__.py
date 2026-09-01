"""Two agents alternate fresh-session turns on a kernel repo that is periodically wiped back to its pristine snapshot.

    hmz exec -f flame_chase_rule_cleanup -a claude/MODEL:EFFORT -a codex/MODEL:EFFORT \\
        "make the kernel under solution/ faster"

A rule-cleaned flame chase for kernel-optimization work. The flame and the
chaser take turns on the task in the working repository; every turn is a fresh
session handed the task verbatim and dropped afterwards, so each agent reads
the repository, never a history. A completed turn is one coding agent session
that answers; a session that does not land is retried without advancing the
turn count.

Before the first turn ever runs, the working tree minus .git is stored as the
pristine task tree under ~/.flame_chase_rule_cleanup/<sha256 of the working
directory's absolute path>/pristine -- outside the tree, where a wipe cannot
touch it and a resumed run finds it; a snapshot already there is kept, not
remade. Every wipe_turns completed coding-agent turns (default 5; 0 never
wipes), between turns, plain deterministic code erases the repository's
memory: solution/ is saved aside outside the tree, everything inside the working
directory is deleted
(.git included), the pristine tree is restored, the saved solution/ replaces
the pristine one, every '#', '//' and '/* */' comment is mechanically stripped
from its Python and .cu/.cuh/.c/.h/.cpp/.hpp sources (a file that cannot be
processed cleanly -- or whose declared encoding would not survive losing its
coding line -- is kept unmodified; string literals and docstrings are never
altered), and git is re-initialized with a single commit under a neutral
identity passed on the command line. The wipe is a transaction: the saved
solution/ is kept outside the tree until restore, stripping and the git commit
have all succeeded, so an interrupted wipe resumes whole, and a wipe that
cannot finish stops the run with its error rather than record an epoch that
did not happen. Nothing erased is archived; only git runs as a subprocess. A
tree with no solution/ is still reset to the pristine tree, and the run says so.

One thing ends the run: budget_millions million output tokens (default 10)
spent between the two agents across every run in this workspace, measured as a
before/after delta around every turn and added to resumable state the moment
the turn ends. Completed turns, tokens spent and the wipe epoch live in that
state, cleared when the budget ends the run. Each turn prints its number, the
agent, the epoch and the spend; each wipe prints which epoch begins, how many
files were removed and whether solution/ was carried over.
"""

from __future__ import annotations

import codecs
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import tokenize
from pathlib import Path
from typing import Any, NamedTuple

from hmz.flows import Agent, flow
from pydantic import BaseModel, Field

FLAME = "flame"
CHASER = "chaser"


class Agents(NamedTuple):
    flame: Agent  # takes the first turn, then every other turn
    chaser: Agent  # takes the second turn, then every other turn


class Config(BaseModel):
    model_config = {"extra": "forbid"}

    budget_millions: float = Field(
        default=10.0,
        ge=0,
        description="millions of output tokens the pair may spend between them "
        "across every run in this workspace before the flow stops",
    )
    wipe_turns: int = Field(
        default=5,
        ge=0,
        description="completed coding-agent turns between memory wipes of the working "
        "repository; 0 never wipes",
    )


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    held = config or Config()
    kept: dict[str, Any] = {} if state is None else state

    workdir = Path.cwd().resolve()
    key = hashlib.sha256(str(workdir).encode()).hexdigest()[:16]
    keep_dir = (Path.home() / ".flame_chase_rule_cleanup" / key).resolve()
    if keep_dir.is_relative_to(workdir):
        raise RuntimeError(
            f"{keep_dir} sits inside the working tree {workdir}; a wipe would "
            "eat its own snapshot -- run the chase inside a repository"
        )

    if ensure_snapshot(workdir, keep_dir):
        print(f"pristine task tree stored at {keep_dir / 'pristine'}")

    spent = int(kept.get("spent", 0))
    turns = int(kept.get("turns", 0))
    epoch = int(kept.get("epoch", 1))
    next_seat = FLAME if turns % 2 == 0 else CHASER
    limit = held.budget_millions * 1_000_000

    if kept:
        print(
            f"resuming: {turns} turns done, epoch {epoch}, "
            f"{spent / 1e6:.2f}M output tokens spent, {next_seat} next"
        )
    wiping = (
        f"wiping every {held.wipe_turns} turns" if held.wipe_turns else "never wiping"
    )
    print(f"flame chase: budget {held.budget_millions:g}M output tokens, {wiping}")

    while True:
        kept["spent"] = spent
        if spent >= limit:
            print(
                f"budget ends the run: {spent / 1e6:.2f}M of "
                f"{held.budget_millions:g}M output tokens after {turns} turns, "
                f"epoch {epoch}"
            )
            kept.clear()
            return

        # A wipe is due after each configured number of completed turns once
        # those turns have outrun the wipes already taken (epoch - 1 of them).
        if (
            held.wipe_turns > 0
            and turns > 0
            and turns % held.wipe_turns == 0
            and epoch <= turns // held.wipe_turns
        ):
            removed, carried, stripped = wipe(workdir, keep_dir)
            epoch += 1
            kept["epoch"] = epoch
            if carried:
                print(
                    f"wipe: epoch {epoch} begins -- {removed} files removed, "
                    f"solution/ carried over ({stripped} stripped of comments)"
                )
            else:
                print(
                    f"wipe: epoch {epoch} begins -- {removed} files removed, "
                    "no solution/ to carry; the tree is the pristine task tree again"
                )

        seat = agents.flame if next_seat == FLAME else agents.chaser
        before = int(seat.spent().output)
        session = seat.new(cwd=str(workdir))
        said = session(task, suppress=True)
        del session  # dropped: the next turn arrives remembering nothing
        spent += max(0, int(seat.spent().output) - before)
        kept["spent"] = spent  # the turn's cost lands in state at once

        if not said:
            print(f"{next_seat}'s turn did not land; taking it again")
        else:
            turns += 1
            kept["turns"] = turns
            print(
                f"turn {turns}: {next_seat} completed, epoch {epoch}, "
                f"{spent / 1e6:.2f}M output tokens spent"
            )
            next_seat = CHASER if next_seat == FLAME else FLAME
        time.sleep(5)


# --------------------------------------------------------------------------
# The deterministic half: the pristine snapshot, the wipe transaction, and
# the comment strippers. Standard library throughout; git is the only
# subprocess anything here runs, and nothing erased is archived anywhere.
# --------------------------------------------------------------------------

_PRISTINE = "pristine"
_CARRY = "carry"
_WIPING = "wiping"
_C_SUFFIXES = {".cu", ".cuh", ".c", ".h", ".cpp", ".hpp"}
# -c sets author and committer both, so the commit lands whatever git
# configuration the machine has
_IDENTITY = ("-c", "user.name=flame chase", "-c", "user.email=flame-chase@localhost")
_RAW_OPEN = re.compile(rb'(?:u8|u|U|L)?R"')
_CODING = re.compile(r"coding[:=]")


def ensure_snapshot(workdir: Path, keep_dir: Path) -> bool:
    """Store the working tree, minus .git, as the pristine task tree.

    A snapshot already there is kept, not remade. Answers whether one was
    made now.
    """
    pristine = keep_dir / _PRISTINE
    if pristine.exists():
        return False
    keep_dir.mkdir(parents=True, exist_ok=True)
    partial = keep_dir / (_PRISTINE + ".partial")  # whole or absent, never half
    if partial.exists():
        shutil.rmtree(partial)
    shutil.copytree(
        workdir, partial, symlinks=True, ignore=shutil.ignore_patterns(".git")
    )
    partial.rename(pristine)
    return True


def wipe(workdir: Path, keep_dir: Path) -> tuple[int, bool, int]:
    """Erase the repository's memory, as a transaction.

    Saves solution/ aside outside the tree, writes a marker, deletes
    everything inside workdir (.git included), restores the pristine tree,
    copies the saved solution/ back over it, strips comments from its sources,
    and re-initializes git with one neutral commit. The carry is retained and
    the marker stands until every step has succeeded, so a wipe that dies --
    or whose git step fails and raises -- is redone whole from the carry on
    the next attempt instead of mistaking a half-restored tree for the real
    solution. Answers (files removed, whether solution/ was carried, sources
    comment-stripped).
    """
    pristine = keep_dir / _PRISTINE
    if not pristine.is_dir():
        raise RuntimeError(f"pristine task tree missing at {pristine}; not wiping")

    carry = keep_dir / _CARRY
    marker = keep_dir / _WIPING
    solution = workdir / "solution"

    # 1. solution/ aside. A marker from an interrupted wipe means the carry,
    #    not the tree, holds the real solution -- never refresh it then.
    if not marker.exists():
        partial = keep_dir / (_CARRY + ".partial")
        if partial.exists():
            shutil.rmtree(partial)
        if solution.is_dir():
            shutil.copytree(
                solution, partial, symlinks=True, ignore=shutil.ignore_patterns(".git")
            )
            if carry.exists():
                shutil.rmtree(carry)
            partial.rename(carry)
        elif carry.exists():
            shutil.rmtree(carry)  # stale: the tree is authoritative and has none
        marker.write_text("wipe in flight; carry/ is authoritative\n")
    carried = carry.is_dir()

    # 2. everything inside the working directory goes, .git included
    removed = _count_files(workdir)
    for child in list(workdir.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    # 3. the pristine task tree comes back
    shutil.copytree(pristine, workdir, symlinks=True, dirs_exist_ok=True)

    # 4. a copy of the carry replaces the pristine solution/; the carry stays
    if carried:
        if solution.is_symlink() or solution.is_file():
            solution.unlink()
        elif solution.is_dir():
            shutil.rmtree(solution)
        shutil.copytree(carry, solution, symlinks=True)

    # 5. comments go from the carried solution/; a tree with none stays
    #    exactly the pristine task tree
    stripped = _strip_tree(solution) if carried and solution.is_dir() else 0

    # 6. version control starts over: one commit, no history of past epochs.
    #    A git failure raises out of the wipe before it is recorded anywhere.
    _reinit_git(workdir)

    # all of it succeeded: close the transaction, then let the carry go
    marker.unlink()
    if carry.exists():
        shutil.rmtree(carry)
    return removed, carried, stripped


def _count_files(root: Path) -> int:
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        count += len(filenames)
        count += sum(1 for d in dirnames if os.path.islink(os.path.join(dirpath, d)))
    return count


def _reinit_git(workdir: Path) -> None:
    """git init, add everything, one neutral-identity commit; raises rather
    than leave a wipe without its single-commit repository."""
    for argv in (
        ("git", "-c", "init.defaultBranch=main", "init", "--quiet"),
        ("git", "add", "-A"),
        ("git", *_IDENTITY, "commit", "--quiet", "--allow-empty", "-m", "task tree"),
    ):
        try:
            done = subprocess.run(
                list(argv),
                check=False,  # the returncode is judged and escalated below
                cwd=str(workdir),
                capture_output=True,
                text=True,
                env=_git_env(),
            )
        except OSError as err:
            raise RuntimeError(f"git re-init could not run: {err}") from err
        if done.returncode != 0:
            raise RuntimeError(
                f"git re-init failed at '{' '.join(argv)}': {done.stderr.strip()[:200]}"
            )


def _git_env() -> dict[str, str]:
    """The environment git runs under, with every ambient GIT_* variable --
    GIT_DIR, GIT_WORK_TREE, index, object and identity overrides -- dropped,
    so the re-init acts on workdir alone whatever the machine exports."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _strip_tree(solution: Path) -> int:
    """Strip comments from source files under solution/; answers how many changed."""
    changed = 0
    for dirpath, _dirnames, filenames in os.walk(solution):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if path.suffix == ".py":
                changed += _strip_file(path, _strip_python)
            elif path.suffix in _C_SUFFIXES:
                changed += _strip_file(path, _strip_c)
    return changed


def _strip_file(path: Path, strip) -> int:
    """Rewrite one file without its comments, atomically: the stripped bytes
    go to an exclusively created same-directory temporary (never a path that
    could already exist or be a symlink) and replace the original only whole.
    Only that newly created file is ever cleaned up. A file that cannot be
    processed cleanly is kept unmodified."""
    try:
        before = path.read_bytes()
        after = strip(before)
    except Exception:  # noqa: BLE001 - any unsafe rewrite keeps the file unchanged
        return 0
    if after == before:
        return 0
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".stripping"
        )
    except OSError:
        return 0
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(after)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return 0
    return 1


def _strip_python(source: bytes) -> bytes:
    """Drop every '#' comment via the standard tokenizer; strings and
    docstrings are never altered. A file whose declared encoding is not
    utf-8 cannot lose its PEP 263 coding line safely, so it raises and is
    kept unmodified whole."""
    tokens = list(tokenize.tokenize(io.BytesIO(source).readline))
    encoding = "utf-8"
    if tokens and tokens[0].type == tokenize.ENCODING:
        # the canonical name, so every alias -- utf8, UTF-8, utf_8 -- reads
        # as the utf-8 it is rather than as an encoding to be preserved
        encoding = codecs.lookup(tokens[0].string).name
    kept = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            if (
                tok.start[0] <= 2
                and _CODING.search(tok.string)
                and encoding not in ("utf-8", "utf-8-sig")
            ):
                raise ValueError(f"cannot drop the coding line of a {encoding} file")
            continue
        kept.append(tok)
    out = tokenize.untokenize(kept)
    if isinstance(out, str):
        out = out.encode(encoding)
    list(tokenize.tokenize(io.BytesIO(out).readline))  # must still tokenize
    return out


def _strip_c(source: bytes) -> bytes:
    """Drop '//' and '/* ... */' comments by a byte scan that tracks string
    and character literals. Numeric literals are consumed whole so a C++14
    digit separator (1'000) never opens a character literal; raw strings are
    copied whole; newlines inside comments are kept so line numbers hold. A
    scan that cannot see the end of something raises, and the caller keeps
    the file unmodified."""
    out = bytearray()
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        pair = source[i : i + 2]
        if pair == b"//":
            i += 2
            while i < n and source[i] not in (0x0A, 0x0D):
                if source[i] == 0x5C and source[i + 1 : i + 2] == b"\n":
                    out += b"\n"  # a backslash-newline continues the comment
                    i += 2
                elif source[i] == 0x5C and source[i + 1 : i + 3] == b"\r\n":
                    out += b"\r\n"
                    i += 3
                else:
                    i += 1
            continue  # the line end itself is written on the next pass
        if pair == b"/*":
            end = source.find(b"*/", i + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            out += b" " + b"\n" * source.count(b"\n", i + 2, end)
            i = end + 2
            continue
        if 0x30 <= c <= 0x39 and (i == 0 or not _is_ident(source[i - 1])):
            j = _number_end(source, i)
            out += source[i:j]
            i = j
            continue
        if c in b"RuUL" and (i == 0 or not _is_ident(source[i - 1])):
            m = _RAW_OPEN.match(source, i)
            if m is not None:
                j = _raw_end(source, m.end())
                out += source[i:j]
                i = j
                continue
        if c in (0x22, 0x27):  # " or '
            j = _literal_end(source, i)
            out += source[i:j]
            i = j
            continue
        out.append(c)
        i += 1
    return bytes(out)


def _number_end(source: bytes, start: int) -> int:
    """Index just past the pp-number starting at start, C++14 digit
    separators and exponent signs included, so an apostrophe inside 1'000 is
    part of the number, not the opening of a character literal."""
    i = start
    n = len(source)
    while i < n:
        c = source[i]
        if _is_ident(c) or c == 0x2E:  # alnum, underscore, dot
            i += 1
            if c in b"eEpP" and i < n and source[i] in b"+-":
                i += 1  # an exponent's sign belongs to the number
            continue
        if c == 0x27 and i + 1 < n and _is_ident(source[i + 1]):
            i += 2  # digit separator
            continue
        break
    return i


def _raw_end(source: bytes, after_quote: int) -> int:
    """Index just past a raw string whose opening R" ends at after_quote."""
    lp = source.find(b"(", after_quote)
    if lp < 0 or lp - after_quote > 16:
        raise ValueError("raw string with no delimiter")
    delim = source[after_quote:lp]
    if any(b in b' \\)"' for b in delim) or b"\n" in delim or b"\r" in delim:
        raise ValueError("raw string delimiter unclear")
    closer = b")" + delim + b'"'
    end = source.find(closer, lp + 1)
    if end < 0:
        raise ValueError("unterminated raw string")
    return end + len(closer)


def _literal_end(source: bytes, start: int) -> int:
    """Index just past the string or character literal opening at start."""
    quote = source[start]
    i = start + 1
    n = len(source)
    while i < n:
        c = source[i]
        if c == 0x5C:  # escape: the next byte is literal text
            i += 2
            continue
        if c == quote:
            return i + 1
        if c in (0x0A, 0x0D):
            raise ValueError("line end inside a literal")
        i += 1
    raise ValueError("unterminated literal")


def _is_ident(b: int) -> bool:
    return 0x30 <= b <= 0x39 or 0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A or b == 0x5F
