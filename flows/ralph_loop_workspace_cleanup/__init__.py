"""A fresh-session Ralph loop with deterministic workspace cleanup.

    hmz exec -f ralph_loop_workspace_cleanup -a claude \
        -c cleanup.yaml "improve the project"

The agent receives the task in a new session on every landed turn.  After each
configured number of completed turns, the flow restores the repository to the
pristine tree captured before the first turn, then carries back only the
configured ``work_paths``.  The cleanup is transactional and its state lives
outside the repository, so an interrupted run can resume safely.
Each turn also has configurable wall-clock and token-idle watchdogs: a wall-clock
limit injects a short wrap-up request and closes the session after its grace
period, while an idle limit injects a status reminder.
"""

from __future__ import annotations

import codecs
import datetime as dt
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import tokenize
import uuid
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from _workspace_cleanup_watchdog import run_guarded
from hmz.flows import Agent, flow, home
from pydantic import BaseModel, Field, field_validator

FLOW_NAME = "ralph_loop_workspace_cleanup"
MILLION = 1_000_000
_PRISTINE = "pristine"
_CARRY = "carry"
_CLEANING = "cleaning"
_C_SUFFIXES = {".cu", ".cuh", ".c", ".h", ".cpp", ".hpp"}
_IDENTITY = (
    "-c",
    "user.name=ralph loop",
    "-c",
    "user.email=ralph-loop@localhost",
)
_RAW_OPEN = re.compile(rb'(?:u8|u|U|L)?R"')
_CODING = re.compile(r"coding[:=]")


class Agents(NamedTuple):
    """The single agent driven by the loop."""

    agent: Agent


class Config(BaseModel):
    """The loop budget and cleanup policy."""

    model_config = {"extra": "forbid"}

    budget: float = Field(
        default=10.0,
        ge=0,
        description="millions of output tokens before the loop stops",
    )
    cleanup_turns: int = Field(
        default=3,
        ge=0,
        description="completed turns between cleanups; 0 disables cleanup",
    )
    work_paths: tuple[str, ...] = Field(
        min_length=1,
        description="relative, non-overlapping paths whose contents survive cleanup",
    )
    session_timeout_minutes: float = Field(
        default=240.0,
        ge=0,
        description="minutes per session before a forced wrap-up prompt; 0 disables it",
    )
    idle_timeout_minutes: float = Field(
        default=10.0,
        ge=0,
        description="minutes without token usage increasing before a reminder; 0 disables it",
    )
    stop_grace_minutes: float = Field(
        default=10.0,
        ge=0,
        description="minutes after the wrap-up prompt before the session is closed",
    )

    @field_validator("work_paths")
    @classmethod
    def validate_work_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(Path(raw) for raw in value)
        for path in paths:
            if (
                not path.parts
                or path.is_absolute()
                or path == Path(".")
                or ".." in path.parts
                or ".git" in path.parts
            ):
                raise ValueError(
                    "work_paths must be relative paths below the repository"
                )
        if len(set(paths)) != len(paths):
            raise ValueError("work_paths must not contain duplicates")
        for index, path in enumerate(paths):
            for other in paths[index + 1 :]:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise ValueError("work_paths must not overlap")
        return tuple(path.as_posix() for path in paths)


def _workspace_key(source: Path) -> str:
    plain = "".join(char if char.isalnum() else "-" for char in str(source))
    readable = "-".join(part for part in plain.split("-") if part)[-80:] or "root"
    digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:12]
    return f"{readable}-{digest}"


def _managed_parent(source: Path) -> Path:
    parent = (home() / FLOW_NAME / _workspace_key(source)).resolve()
    if parent.is_relative_to(source):
        raise RuntimeError("HUMANIZE_HOME must sit outside the cleaned repository")
    return parent


def _open_store(source: Path, state: dict[str, Any]) -> tuple[Path, bool]:
    parent = _managed_parent(source)
    run_id = state.get("run_id")
    run_root = state.get("run_root")
    if run_id is not None or run_root is not None:
        if not isinstance(run_id, str) or not run_id or not isinstance(run_root, str):
            raise ValueError("resumable cleanup state has incomplete run storage")
        raw = Path(run_root)
        root = raw.resolve()
        if root.parent != parent or root.name != run_id:
            raise ValueError("resumable cleanup storage is outside HUMANIZE_HOME")
        if raw.is_symlink() or not root.is_dir():
            raise RuntimeError("resumable cleanup storage is missing or linked")
        return root, True
    if state:
        raise ValueError("resumable cleanup state predates managed run storage")
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{uuid.uuid4().hex[:10]}"
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / run_id
    root.mkdir()
    state.update(run_id=run_id, run_root=str(root), snapshot_ready=False)
    return root, False


def _remove_store(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"cleanup storage was replaced or linked: {root}")
    shutil.rmtree(root)


def ensure_snapshot(workdir: Path, store: Path) -> bool:
    pristine = store / _PRISTINE
    if pristine.is_symlink():
        raise RuntimeError(f"pristine snapshot is linked: {pristine}")
    if pristine.is_dir():
        return False
    partial = store / (_PRISTINE + ".partial")
    if partial.is_symlink():
        raise RuntimeError(f"partial pristine snapshot is linked: {partial}")
    if partial.exists():
        shutil.rmtree(partial)
    store.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        workdir, partial, symlinks=True, ignore=shutil.ignore_patterns(".git")
    )
    partial.rename(pristine)
    return True


def _entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _safe_entry(root: Path, relative: Path) -> Path:
    if (
        not relative.parts
        or relative.is_absolute()
        or relative == Path(".")
        or ".." in relative.parts
        or ".git" in relative.parts
    ):
        raise ValueError(f"unsafe work path: {relative}")
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"work path {relative} crosses symlink {current}")
    return root / relative


def _remove_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _copy_entry(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink() or source.is_file():
        shutil.copy2(source, target, follow_symlinks=False)
    elif source.is_dir():
        shutil.copytree(
            source, target, symlinks=True, ignore=shutil.ignore_patterns(".git")
        )


def _count_files(root: Path) -> int:
    count = 0
    for directory, dirs, files in os.walk(root):
        count += len(files)
        count += sum(1 for name in dirs if (Path(directory) / name).is_symlink())
    return count


def _git_env() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }


def _reinit_git(workdir: Path) -> None:
    commands = (
        ("git", "-c", "init.defaultBranch=main", "init", "--quiet"),
        ("git", "add", "-A"),
        ("git", *_IDENTITY, "commit", "--quiet", "--allow-empty", "-m", "task tree"),
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=workdir,
                check=False,
                capture_output=True,
                text=True,
                env=_git_env(),
            )
        except OSError as error:
            raise RuntimeError(f"git re-init could not run: {error}") from error
        if result.returncode:
            raise RuntimeError(
                f"git re-init failed at {' '.join(command)}: {result.stderr.strip()[:200]}"
            )


def cleanup(
    workdir: Path, store: Path, work_paths: tuple[Path, ...]
) -> tuple[int, tuple[str, ...], int]:
    """Restore pristine files and carry configured work paths as one transaction."""
    pristine = store / _PRISTINE
    if not pristine.is_dir():
        raise RuntimeError(f"pristine task tree missing at {pristine}; not cleaning")
    carry = store / _CARRY
    marker = store / _CLEANING
    partial = store / (_CARRY + ".partial")
    for name, path in (
        ("carry", carry),
        ("carry partial", partial),
        ("cleanup marker", marker),
    ):
        if path.is_symlink():
            raise RuntimeError(f"{name} storage is linked: {path}")
    if not marker.exists():
        if partial.exists():
            shutil.rmtree(partial)
        partial.mkdir()
        for relative in work_paths:
            source = _safe_entry(workdir, relative)
            if _entry_exists(source):
                _copy_entry(source, partial / relative)
        if carry.exists():
            shutil.rmtree(carry)
        partial.rename(carry)
        marker.write_text("cleanup in flight; carry/ is authoritative\n")
    elif not carry.is_dir():
        raise RuntimeError("cleanup marker exists without a carry directory")
    carried = tuple(
        relative.as_posix()
        for relative in work_paths
        if _entry_exists(carry / relative)
    )
    removed = _count_files(workdir)
    for child in list(workdir.iterdir()):
        _remove_entry(child)
    shutil.copytree(pristine, workdir, symlinks=True, dirs_exist_ok=True)
    for relative in work_paths:
        source = carry / relative
        if not _entry_exists(source):
            continue
        target = _safe_entry(workdir, relative)
        _remove_entry(target)
        _copy_entry(source, target)
    stripped = sum(
        _strip_path(_safe_entry(workdir, relative)) for relative in work_paths
    )
    _reinit_git(workdir)
    marker.unlink()
    if carry.exists():
        shutil.rmtree(carry)
    return removed, carried, stripped


def _strip_path(root: Path) -> int:
    if root.is_symlink():
        return 0
    if root.is_file():
        return _strip_source(root)
    changed = 0
    if root.is_dir():
        for directory, dirs, files in os.walk(root):
            dirs[:] = [
                name for name in dirs if not (Path(directory) / name).is_symlink()
            ]
            changed += sum(_strip_source(Path(directory) / name) for name in files)
    return changed


def _strip_source(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.suffix == ".py":
        return _strip_file(path, _strip_python)
    if path.suffix in _C_SUFFIXES:
        return _strip_file(path, _strip_c)
    return 0


def _strip_file(path: Path, strip: Any) -> int:
    temporary: Path | None = None
    try:
        before = path.read_bytes()
        after = strip(before)
    except Exception:  # noqa: BLE001 - unsafe rewrites remain unchanged
        return 0
    if after == before:
        return 0
    try:
        fd, name = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".", suffix=".stripping"
        )
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(after)
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return 0
    return 1


def _strip_python(source: bytes) -> bytes:
    tokens = list(tokenize.tokenize(io.BytesIO(source).readline))
    encoding = "utf-8"
    if tokens and tokens[0].type == tokenize.ENCODING:
        encoding = codecs.lookup(tokens[0].string).name
    kept = []
    for token in tokens:
        if token.type == tokenize.COMMENT:
            if (
                token.start[0] <= 2
                and _CODING.search(token.string)
                and encoding
                not in (
                    "utf-8",
                    "utf-8-sig",
                )
            ):
                raise ValueError("cannot drop a non-UTF-8 coding line")
            continue
        kept.append(token)
    result = tokenize.untokenize(kept)
    if isinstance(result, str):
        result = result.encode(encoding)
    list(tokenize.tokenize(io.BytesIO(result).readline))
    return result


def _strip_c(source: bytes) -> bytes:
    out = bytearray()
    index, length = 0, len(source)
    while index < length:
        pair = source[index : index + 2]
        if pair == b"//":
            index += 2
            while index < length and source[index] not in (10, 13):
                if source[index] == 92 and source[index + 1 : index + 2] == b"\n":
                    out += b"\n"
                    index += 2
                elif source[index] == 92 and source[index + 1 : index + 3] == b"\r\n":
                    out += b"\r\n"
                    index += 3
                else:
                    index += 1
            continue
        if pair == b"/*":
            end = source.find(b"*/", index + 2)
            if end < 0:
                raise ValueError("unterminated block comment")
            out += b" " + b"\n" * source.count(b"\n", index + 2, end)
            index = end + 2
            continue
        if source[index] in b"RuUL" and (
            index == 0 or not _is_ident(source[index - 1])
        ):
            match = _RAW_OPEN.match(source, index)
            if match is not None:
                end = _raw_end(source, match.end())
                out += source[index:end]
                index = end
                continue
        if 48 <= source[index] <= 57 and (
            index == 0 or not _is_ident(source[index - 1])
        ):
            end = _number_end(source, index)
            out += source[index:end]
            index = end
            continue
        if source[index] in (34, 39):
            end = _literal_end(source, index)
            out += source[index:end]
            index = end
            continue
        out.append(source[index])
        index += 1
    return bytes(out)


def _raw_end(source: bytes, after_quote: int) -> int:
    opening = source.find(b"(", after_quote)
    if opening < 0 or opening - after_quote > 16:
        raise ValueError("raw string with no delimiter")
    delimiter = source[after_quote:opening]
    if any(byte in b' \\)"\n\r' for byte in delimiter):
        raise ValueError("raw string delimiter unclear")
    closing = b")" + delimiter + b'"'
    end = source.find(closing, opening + 1)
    if end < 0:
        raise ValueError("unterminated raw string")
    return end + len(closing)


def _literal_end(source: bytes, start: int) -> int:
    quote = source[start]
    index = start + 1
    while index < len(source):
        if source[index] == 92:
            index += 2
        elif source[index] == quote:
            return index + 1
        elif source[index] in (10, 13):
            raise ValueError("line end inside a literal")
        else:
            index += 1
    raise ValueError("unterminated literal")


def _number_end(source: bytes, start: int) -> int:
    index = start
    while index < len(source):
        byte = source[index]
        if _is_ident(byte) or byte == 46:
            index += 1
            if byte in b"eEpP" and index < len(source) and source[index] in b"+-":
                index += 1
        elif byte == 39 and index + 1 < len(source) and _is_ident(source[index + 1]):
            index += 2
        else:
            break
    return index


def _is_ident(byte: int) -> bool:
    return 48 <= byte <= 57 or 65 <= byte <= 90 or 97 <= byte <= 122 or byte == 95


@flow(resumable=True)
def run(
    agents: Agents,
    task: str,
    config: Config | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    held = config or Config()
    kept = state if state is not None else {}
    workdir = Path.cwd().resolve()
    store, resumed = _open_store(workdir, kept)
    turns = int(kept.get("turns", 0))
    epoch = int(kept.get("epoch", 1))
    spent = int(kept.get("spent", 0))
    if kept.get("snapshot_ready") is True:
        pristine = store / _PRISTINE
        if pristine.is_symlink() or not pristine.is_dir():
            raise RuntimeError(f"pristine task tree missing or linked at {pristine}")
    else:
        if resumed and turns:
            raise RuntimeError("resumable cleanup state lost its pristine snapshot")
        ensure_snapshot(workdir, store)
        kept["snapshot_ready"] = True
    limit = held.budget * MILLION
    if resumed:
        print(
            f"resuming: {turns} turns done, epoch {epoch}, {spent / MILLION:.2f}M spent"
        )
    while True:
        kept["spent"] = spent
        if held.budget and spent >= limit:
            print(f"budget ends the run after {turns} turns")
            _remove_store(store)
            kept.clear()
            return
        if (
            held.cleanup_turns > 0
            and turns > 0
            and turns % held.cleanup_turns == 0
            and epoch <= turns // held.cleanup_turns
        ):
            removed, carried, stripped = cleanup(
                workdir, store, tuple(Path(path) for path in held.work_paths)
            )
            epoch += 1
            kept["epoch"] = epoch
            print(
                f"cleanup: epoch {epoch} begins -- {removed} files removed, "
                f"{', '.join(carried) if carried else 'nothing'} carried over "
                f"({stripped} stripped of comments)"
            )
        agent = agents[0]
        before = int(agent.spent().output)
        session = agent.new(cwd=str(workdir))
        landed = run_guarded(
            session,
            partial(session, task, suppress=True),
            session_timeout_minutes=held.session_timeout_minutes,
            idle_timeout_minutes=held.idle_timeout_minutes,
            stop_grace_minutes=held.stop_grace_minutes,
            label=f"turn {turns + 1}",
        )
        del session
        spent += max(0, int(agent.spent().output) - before)
        kept["spent"] = spent
        if landed:
            turns += 1
            kept["turns"] = turns
            print(f"turn {turns}: epoch {epoch}, {spent / MILLION:.2f}M spent")
        else:
            print("turn did not land; taking it again")
        time.sleep(5)
