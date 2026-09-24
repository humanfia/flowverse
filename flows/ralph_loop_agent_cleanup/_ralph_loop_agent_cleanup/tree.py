from __future__ import annotations

import os
import shutil
import signal
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

NOTES = "NEXT.md"
IGNORES = ".gitignore"
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
GIT_SECONDS = 3600
CHECK_LOG_BYTES = 1024**2
MIB = 1024**2


class Measure(NamedTuple):
    strays: list[str]
    notes_lines: int
    comment_count: int


class Footprint(NamedTuple):
    files: int
    bytes: int


def _git(
    *args: str,
    cwd: Path | None = None,
    index: Path | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            input=stdin,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(["git", *args], -1, "", str(error))


def listed(root: Path) -> list[str]:
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
    entries = {entry.rstrip("/") for entry in done.stdout.split("\0") if entry}
    if os.path.lexists(root / ".git"):
        tracked = _git("ls-files", "-z", cwd=root)
        if not tracked.returncode:
            entries.update(
                rel
                for rel in tracked.stdout.split("\0")
                if rel and os.path.lexists(root / rel)
            )
    return sorted(entries)


def footprint(root: Path) -> Footprint:
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
    return store / "manifest.txt"


def ensure_manifest(root: Path, store: Path) -> set[str]:
    path = manifest_path(store)
    if path.is_symlink():
        raise RuntimeError(f"manifest was replaced by a symlink: {path}")
    if not path.exists():
        path.write_text("\n".join(listed(root)) + "\n", encoding="utf-8")
    kept = path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in kept if line.strip()}


def _py_comment_lines(text: str) -> int:
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
    entries = listed(root)
    notes = root / NOTES
    strays = [
        rel
        for rel in entries
        if rel not in manifest
        and not (rel == NOTES and not notes.is_symlink())
        and Path(rel).name != IGNORES
        and not under_work_path(rel, work_paths)
    ]
    comments = sum(
        _comment_lines(root / rel)
        for rel in entries
        if under_work_path(rel, work_paths)
    )
    return Measure(strays, _notes_lines(root), comments)


def _notes_lines(root: Path) -> int:
    path = root / NOTES
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def truncate_notes(root: Path, limit: int) -> None:
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
    for rel in strays:
        path = root / rel
        try:
            if path.is_dir() and not path.is_symlink():
                continue
            path.unlink(missing_ok=True)
        except OSError:
            print(f"could not delete stray {rel}")


def _writable(path: str | Path) -> None:
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISDIR(mode):
            os.chmod(path, mode | stat.S_IRWXU)
    except OSError:
        pass


def _removed(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def _remove(path: Path) -> None:
    try:
        _removed(path)
    except PermissionError:
        _writable(path.parent)
        if path.is_dir() and not path.is_symlink():
            _writable(path)
            for directory, dirs, _files in os.walk(path):
                for name in dirs:
                    _writable(Path(directory) / name)
        _removed(path)


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def save_tree(root: Path, store: Path) -> Path:
    saved = store / "revert"
    for aside in (store / "revert.partial", store / "revert.dropping"):
        if aside.is_symlink():
            raise RuntimeError(f"revert storage was replaced by a symlink: {aside}")
        _remove(aside)
    if saved.is_symlink():
        raise RuntimeError(f"revert point was replaced by a symlink: {saved}")
    if saved.is_dir():
        restore_tree(root, saved)
        return saved
    partial = store / "revert.partial"
    partial.mkdir()
    for rel in listed(root):
        _copy(root / rel, partial / rel)
    if os.path.lexists(root / ".git"):
        _copy(root / ".git", partial / ".git")
    partial.rename(saved)
    return saved


def _saved_ignores(saved: Path) -> dict[str, Path]:
    found = {}
    for directory, dirs, files in os.walk(saved):
        dirs[:] = [name for name in dirs if name != ".git"]
        source = Path(directory) / IGNORES
        if IGNORES in files and not source.is_symlink():
            found[source.relative_to(saved).as_posix()] = source
    return found


def freeze_ignores(root: Path, saved: Path) -> list[str]:
    kept = _saved_ignores(saved)
    touched = []
    for rel in listed(root):
        if Path(rel).name == IGNORES and rel not in kept:
            _remove(root / rel)
            touched.append(rel)
    for rel, source in kept.items():
        target = root / rel
        same = target.is_file() and not target.is_symlink()
        if same and target.read_bytes() == source.read_bytes():
            continue
        try:
            _remove(target)
            _copy(source, target)
        except OSError:
            continue
        touched.append(rel)
    return sorted(touched)


def _clear_changed_types(root: Path, saved: Path) -> None:
    for directory, dirs, files in os.walk(saved):
        base = Path(directory)
        here = root / base.relative_to(saved)
        for name in dirs:
            source, target = base / name, here / name
            if source.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    _remove(target)
            elif os.path.lexists(target) and (
                target.is_symlink() or not target.is_dir()
            ):
                _remove(target)
        for name in files:
            target = here / name
            if target.is_dir() and not target.is_symlink():
                _remove(target)


def restore_tree(root: Path, saved: Path) -> None:
    _clear_changed_types(root, saved)
    freeze_ignores(root, saved)
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
    if not os.path.lexists(saved):
        return
    dropping = saved.with_name(saved.name + ".dropping")
    _remove(dropping)
    os.replace(saved, dropping)
    try:
        _remove(dropping)
    except OSError as error:
        print(f"could not delete {dropping}: {error}")


_HOOK = """#!/bin/sh
# Installed by the workspace-cleanup flows: files over {limit} bytes stay out of git.
git diff --cached --name-only --diff-filter=AM -z | xargs -0 -r sh -c '
status=0
for path do
  size=$(git cat-file -s ":$path")
  if [ "$size" -gt {limit} ]; then
    echo "refused: $path is $size bytes, over the {mb:g} MB this repository tracks;" \\
      "delete it or leave it untracked" >&2
    status=1
  fi
done
exit $status' sh
"""
NOTED = 20


def left_out(root: Path, entries: list[str], limit: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in entries:
        path = root / rel
        if path.is_symlink():
            continue
        if path.is_dir():
            out[rel] = "a nested repository"
        elif (size := _size(path)) > limit:
            out[rel] = f"{size / MIB:,.1f} MB"
    return out


def _noted(out: dict[str, str], limit: int) -> str:
    if not out:
        return ""
    lines = [f"- {rel} ({why})" for rel, why in list(out.items())[:NOTED]]
    if len(out) > NOTED:
        lines.append(f"- and {len(out) - NOTED} more")
    return (
        f"\n\nLeft out of git, over {limit / MIB:g} MB or a repository:\n"
        + "\n".join(lines)
    )


def _pattern(rel: str) -> str:
    escaped = "".join("\\" + ch if ch in "\\*?[" else ch for ch in rel)
    if escaped.endswith(" "):
        escaped = escaped[:-1] + "\\ "
    return "/" + escaped


def _remove_git_entry(root: Path) -> bool:
    try:
        _remove(root / ".git")
    except OSError:
        return False
    return not os.path.lexists(root / ".git")


def history_repo(store: Path) -> Path:
    return store.parent / "history.git"


def epoch_ref(store: Path, epoch: int) -> str:
    return f"refs/runs/{store.name}/epoch-{epoch:03d}"


def _open_history(store: Path) -> Path:
    history = history_repo(store)
    if history.is_symlink():
        raise RuntimeError(f"history repository is a symlink: {history}")
    if not (history / "HEAD").exists():
        made = _git("init", "--quiet", "--bare", str(history))
        if made.returncode:
            raise RuntimeError(f"could not create {history}: {made.stderr.strip()}")
    return history


def _commit_tree(
    history: Path, work_tree: Path, entries: list[str], message: str, parent: str = ""
) -> str:
    at = f"--git-dir={history}"
    with tempfile.TemporaryDirectory(prefix="cleanup-index-") as scratch:
        index = Path(scratch) / "index"
        if entries:
            added = _git(
                "--literal-pathspecs",
                at,
                f"--work-tree={work_tree}",
                "add",
                "--force",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                index=index,
                stdin="\0".join(entries),
            )
            if added.returncode:
                return ""
        tree = _git(at, "write-tree", index=index)
    if tree.returncode:
        return ""
    parents = ("-p", parent) if parent else ()
    made = _git(
        at, *_COMMITTING, "commit-tree", tree.stdout.strip(), *parents, "-m", message
    )
    return "" if made.returncode else made.stdout.strip()


def erase_history(
    root: Path, store: Path, epoch: int, limit: int, title: str = ""
) -> bool:
    history = _open_history(store)
    entries = listed(root)
    out = left_out(root, entries, limit)
    if not _remove_git_entry(root):
        print("the .git entry could not be removed; git was not run")
        return False
    if _git("-c", "init.defaultBranch=main", "init", "-q", cwd=root).returncode:
        return False
    git = root / ".git"
    try:
        (git / "info").mkdir(exist_ok=True)
        with (git / "info" / "exclude").open("a", encoding="utf-8") as exclude:
            exclude.writelines(_pattern(rel) + "\n" for rel in out if "\n" not in rel)
        (git / "hooks").mkdir(exist_ok=True)
        hook = git / "hooks" / "pre-commit"
        hook.write_text(_HOOK.format(limit=limit, mb=limit / MIB), encoding="utf-8")
        hook.chmod(0o755)
    except OSError:
        return False
    message = (title or f"epoch {epoch}: distilled tree") + _noted(out, limit)
    commit = _commit_tree(
        history, root, [rel for rel in entries if rel not in out], message
    )
    if not commit:
        return False
    ref = f"{epoch_ref(store, epoch)}.distilled"
    steps = (
        (f"--git-dir={history}", "update-ref", ref, commit),
        ("config", "core.hooksPath", ".git/hooks"),
        (
            "fetch",
            "-q",
            "--no-tags",
            "--update-head-ok",
            str(history),
            f"{ref}:refs/heads/main",
        ),
        ("reset", "-q"),
    )
    for step in steps:
        try:
            done = _git(*step, cwd=root)
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode:
            return False
    if out:
        print(
            f"epoch {epoch}: {len(out)} entr(ies) left out of git:{_noted(out, limit)}"
        )
    return True


def archive_history(saved: Path, store: Path, epoch: int, limit: int) -> str | None:
    history = _open_history(store)
    at = f"--git-dir={history}"
    ref = epoch_ref(store, epoch)
    parent = ""
    if os.path.lexists(saved / ".git"):
        refs = _git(at, "fetch", "-q", "--no-tags", str(saved), f"+refs/*:{ref}.refs/*")
        head = _git(at, "fetch", "-q", "--no-tags", str(saved), f"+HEAD:{ref}.head")
        if not head.returncode:
            parent = _git(at, "rev-parse", f"{ref}.head").stdout.strip()
        elif refs.returncode:
            kept = history.parent / f"unreadable-{store.name}-epoch-{epoch:03d}.git"
            _remove(kept)
            _copy(saved / ".git", kept)
            print(
                f"epoch {epoch}: git could not read the replaced history; kept at {kept}"
            )
    entries = listed(saved)
    out = left_out(saved, entries, limit)
    message = f"epoch {epoch}: the tree before cleaning" + _noted(out, limit)
    commit = _commit_tree(
        history, saved, [rel for rel in entries if rel not in out], message, parent
    )
    if not commit or _git(at, "update-ref", ref, commit).returncode:
        print(f"epoch {epoch}: the tree before cleaning could not be archived")
        return None
    return ref


def link_history(store: Path, epoch: int) -> None:
    history = history_repo(store)
    at = f"--git-dir={history}"
    ref = epoch_ref(store, epoch)
    grafted = _git(at, "replace", "-f", "--graft", f"{ref}.distilled", ref)
    if grafted.returncode:
        print(f"epoch {epoch}: the distilled commit could not be chained to {ref}")
    _git(at, "repack", "-d", "-q")
    _git(at, "gc", "--auto", "--quiet")


def run_check(root: Path, command: str, log: Path) -> bool:
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
    _cap(log)
    return ok


def _cap(log: Path) -> None:
    try:
        size = log.stat().st_size
        if size <= CHECK_LOG_BYTES:
            return
        with log.open("rb") as held:
            held.seek(size - CHECK_LOG_BYTES)
            end = held.read()
        log.write_bytes(b"[earlier output cut]\n" + end)
    except OSError:
        pass


def tail(log: Path, lines: int = 20) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
