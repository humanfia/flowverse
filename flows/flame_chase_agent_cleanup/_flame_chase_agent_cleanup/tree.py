from __future__ import annotations

import asyncio
import contextlib
import shlex
from collections.abc import AsyncIterator
from pathlib import PurePosixPath
from typing import Any, NamedTuple

from hmz.flows import EnvCommandTimeout, EnvError, EnvFileNotFound

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

_LIB = r"""set -o pipefail
unset $(compgen -e GIT_)

listed() (
  scratch=$(mktemp -d "${TMPDIR:-/tmp}/cleanup-listing.XXXXXX") || exit 1
  trap 'rm -rf "$scratch"' EXIT
  git init --quiet --bare "$scratch/index.git" || exit 1
  {
    git --git-dir="$scratch/index.git" --work-tree=. ls-files --others --exclude-standard -z || exit 1
    if [ -e .git ] || [ -L .git ]; then
      GIT_CEILING_DIRECTORIES=${PWD%/*} git ls-files -z 2>/dev/null |
        while IFS= read -r -d '' rel; do
          if [ -e "$rel" ] || [ -L "$rel" ]; then printf '%s\0' "$rel"; fi
        done
    fi
    :
  } | while IFS= read -r -d '' rel; do printf '%s\0' "${rel%/}"; done | LC_ALL=C sort -z -u
)

remove() {
  { [ -e "$1" ] || [ -L "$1" ]; } || return 0
  rm -rf -- "$1" 2>/dev/null && return 0
  case $1 in */*) chmod u+rwx -- "${1%/*}" 2>/dev/null ;; esac
  if [ -d "$1" ] && [ ! -L "$1" ]; then chmod -R u+rwx -- "$1" 2>/dev/null; fi
  rm -rf -- "$1"
}

snapshot() {
  local at
  at=$(mktemp "${TMPDIR:-/tmp}/cleanup-listing.XXXXXX") || return 1
  if listed > "$at"; then printf %s "$at"; return 0; fi
  rm -f "$at"
  return 1
}

freeze() {
  local listing
  listing=$(snapshot) || return 1
  while IFS= read -r -d '' rel; do
    case /$rel in */.gitignore) ;; *) continue ;; esac
    if [ -L "$1/$rel" ] || [ ! -f "$1/$rel" ]; then
      remove "./$rel"
      printf '%s\0' "$rel"
    fi
  done < "$listing"
  rm -f "$listing"
  (cd "$1" && find . -name .git -prune -o -name .gitignore -type f -print0) |
    while IFS= read -r -d '' rel; do
      rel=${rel#./}
      if [ -f "./$rel" ] && [ ! -L "./$rel" ] && cmp -s "$1/$rel" "./$rel"; then continue; fi
      remove "./$rel"
      case $rel in */*) mkdir -p -- "./${rel%/*}" ;; esac
      if cp -p -- "$1/$rel" "./$rel"; then printf '%s\0' "$rel"; fi
    done
}

save() {
  for aside in "$1/revert.partial" "$1/revert.dropping"; do
    if [ -L "$aside" ]; then
      echo "revert storage was replaced by a symlink: $aside" >&2
      return 2
    fi
    remove "$aside" || return 1
  done
  if [ -L "$1/revert" ]; then
    echo "revert point was replaced by a symlink: $1/revert" >&2
    return 2
  fi
  if [ -d "$1/revert" ]; then echo inflight; return 0; fi
  mkdir -- "$1/revert.partial" || return 1
  local listing failed=
  listing=$(snapshot) || return 1
  while IFS= read -r -d '' rel; do
    case $rel in */*) mkdir -p -- "$1/revert.partial/${rel%/*}" || { failed=1; break; } ;; esac
    cp -PRp -- "./$rel" "$1/revert.partial/$rel" || { failed=1; break; }
  done < "$listing"
  rm -f "$listing"
  [ -z "$failed" ] || return 1
  if [ -e .git ] || [ -L .git ]; then cp -PRp -- .git "$1/revert.partial/.git" || return 1; fi
  mv -- "$1/revert.partial" "$1/revert"
}

restore() {
  (cd "$1" && find . -mindepth 1 -name .git -prune -o -print0) |
    while IFS= read -r -d '' rel; do
      if [ -L "$1/$rel" ] || [ ! -d "$1/$rel" ]; then
        if [ -d "$rel" ] && [ ! -L "$rel" ]; then remove "$rel"; fi
      elif [ -L "$rel" ] || { [ -e "$rel" ] && [ ! -d "$rel" ]; }; then
        remove "$rel"
      fi
    done || return 1
  freeze "$1" > /dev/null || return 1
  local listing parents failed=
  listing=$(snapshot) || return 1
  if ! parents=$(mktemp "${TMPDIR:-/tmp}/cleanup-parents.XXXXXX"); then
    rm -f "$listing"
    return 1
  fi
  while IFS= read -r -d '' rel; do
    remove "./$rel" || { failed=1; break; }
    case $rel in */*) printf '%s\0' "${rel%/*}" >> "$parents" ;; esac
  done < "$listing"
  rm -f "$listing"
  [ -n "$failed" ] || remove ./.git || failed=1
  LC_ALL=C sort -z -r -u "$parents" | xargs -0 -r rmdir -p -- 2>/dev/null
  rm -f "$parents"
  [ -z "$failed" ] || return 1
  cp -PRp -- "$1/." .
}

drop() {
  { [ -e "$1" ] || [ -L "$1" ]; } || return 0
  remove "$1.dropping" || return 1
  mv -f -- "$1" "$1.dropping" || return 1
  remove "$1.dropping" || return 3
}
"""


class Measure(NamedTuple):
    strays: list[str]
    notes_lines: int
    comment_count: int


class Footprint(NamedTuple):
    files: int
    bytes: int


def quoted(path: Any) -> str:
    return shlex.quote(str(path))


async def sh(env: Any, script: str, *, timeout: float = 0) -> tuple[int, str, str]:
    try:
        return await env.exec(_LIB + script, timeout=timeout)
    except EnvCommandTimeout as error:
        return -1, "", str(error)


async def git(
    env: Any, *args: str, index: str | None = None, cwd: Any = None
) -> tuple[int, str, str]:
    command = " ".join(quoted(arg) for arg in ("git", *args))
    if index is not None:
        command = f"GIT_INDEX_FILE={quoted(index)} {command}"
    if cwd is not None:
        command = f"cd {quoted(cwd)} && {command}"
    return await sh(env, command, timeout=GIT_SECONDS)


async def kind(env: Any, path: Any) -> str:
    _, out, _ = await sh(
        env,
        f"p={quoted(path)}\n"
        'if [ -L "$p" ]; then echo l; elif [ -d "$p" ]; then echo d;'
        ' elif [ -f "$p" ]; then echo f; elif [ -e "$p" ]; then echo o; else echo -; fi',
    )
    return out.strip() or "-"


@contextlib.asynccontextmanager
async def scratch(env: Any) -> AsyncIterator[PurePosixPath]:
    done, out, err = await sh(env, 'mktemp -d "${TMPDIR:-/tmp}/cleanup.XXXXXX"')
    if done:
        raise RuntimeError(f"could not make a scratch directory: {err.strip()}")
    where = PurePosixPath(out.strip())
    try:
        yield where
    finally:
        await sh(env, f"rm -rf -- {quoted(where)}")


async def listed(env: Any, at: Any = None) -> dict[str, str]:
    script = (
        "listed | while IFS= read -r -d '' rel; do\n"
        '  if [ -L "$rel" ]; then k=l; elif [ -d "$rel" ]; then k=d;'
        ' elif [ -f "$rel" ]; then k=f; else k=o; fi\n'
        '  printf \'%s%s\\0\' "$k" "$rel"\n'
        "done"
    )
    if at is not None:
        script = f"cd {quoted(at)} || exit 1\n{script}"
    done, out, err = await sh(env, script)
    if done:
        raise RuntimeError(f"git could not list the tree: {err.strip()}")
    found = {item[1:]: item[0] for item in out.split("\0") if item}
    return {rel: found[rel] for rel in sorted(found)}


async def footprint(env: Any) -> Footprint:
    done, out, err = await sh(
        env,
        'listing=$(mktemp "${TMPDIR:-/tmp}/cleanup-footprint.XXXXXX") || exit 3\n'
        "trap 'rm -f \"$listing\"' EXIT\n"
        'listed > "$listing" || exit 3\n'
        "{ while IFS= read -r -d '' rel; do printf './%s\\0' \"$rel\"; done"
        ' < "$listing"\n'
        "  if [ -e .git ] || [ -L .git ]; then printf './.git\\0'; fi; } |\n"
        "  xargs -0 -r sh -c 'exec find -P \"$@\" ! -type d -exec ls -ldn {} +' sh"
        " 2>/dev/null |\n"
        "  awk '{ files++; bytes += $5 } END { printf \"%.0f %.0f\\n\", files, bytes }'",
    )
    try:
        if done == 3:
            raise ValueError(done)
        files, size = out.split()
        return Footprint(int(files), int(size))
    except ValueError:
        raise RuntimeError(f"could not measure the workspace: {err.strip()}") from None


def manifest_path(store: PurePosixPath) -> PurePosixPath:
    return store / "manifest.txt"


async def ensure_manifest(env: Any, store: PurePosixPath) -> set[str]:
    path = manifest_path(store)
    found = await kind(env, path)
    if found == "l":
        raise RuntimeError(f"manifest was replaced by a symlink: {path}")
    if found == "-":
        entries = await listed(env)
        await env.write(str(path), ("\n".join(entries) + "\n").encode())
    kept = (await env.read(str(path))).decode().splitlines()
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


def _commented(relative: str) -> bool:
    suffix = PurePosixPath(relative).suffix.lower()
    return suffix == ".py" or suffix in C_SUFFIXES


async def _comment_lines(env: Any, relative: str) -> int:
    try:
        text = (await env.read(relative)).decode("utf-8", errors="replace")
    except EnvError:
        return 0
    if PurePosixPath(relative).suffix.lower() == ".py":
        return _py_comment_lines(text)
    return _c_comment_lines(text)


def under_work_path(relative: str, work_paths: tuple[str, ...]) -> bool:
    return any(
        relative == path or relative.startswith(path + "/") for path in work_paths
    )


async def measure(env: Any, manifest: set[str], work_paths: tuple[str, ...]) -> Measure:
    entries = await listed(env)
    strays = [
        rel
        for rel, found in entries.items()
        if rel not in manifest
        and not (rel == NOTES and found != "l")
        and PurePosixPath(rel).name != IGNORES
        and not under_work_path(rel, work_paths)
    ]
    counted = await asyncio.gather(
        *(
            _comment_lines(env, rel)
            for rel, found in entries.items()
            if found == "f" and under_work_path(rel, work_paths) and _commented(rel)
        )
    )
    return Measure(strays, await _notes_lines(env), sum(counted))


async def _notes_lines(env: Any) -> int:
    if await kind(env, NOTES) != "f":
        return 0
    try:
        return len(
            (await env.read(NOTES)).decode("utf-8", errors="replace").splitlines()
        )
    except EnvError:
        return 0


async def truncate_notes(env: Any, limit: int) -> None:
    if await kind(env, NOTES) != "f":
        return
    try:
        lines = (await env.read(NOTES)).decode("utf-8", errors="replace").splitlines()
        if len(lines) > limit:
            await env.write(NOTES, ("\n".join(lines[:limit]) + "\n").encode())
    except EnvError:
        print(f"could not truncate {NOTES}")


async def delete_strays(env: Any, strays: list[str]) -> None:
    if not strays:
        return
    async with scratch(env) as where:
        await env.write(
            str(where / "strays"), "".join(f"./{rel}\0" for rel in strays).encode()
        )
        _, out, _ = await sh(
            env,
            "while IFS= read -r -d '' rel; do\n"
            '  if [ -d "$rel" ] && [ ! -L "$rel" ]; then continue; fi\n'
            '  rm -f -- "$rel" 2>/dev/null || printf \'could not delete stray %s\\n\' "${rel#./}"\n'
            f"done < {quoted(where / 'strays')}",
        )
    if out:
        print(out, end="")


async def save_tree(env: Any, store: PurePosixPath) -> PurePosixPath:
    saved = store / "revert"
    done, out, err = await sh(env, f"save {quoted(store)}")
    if done:
        raise RuntimeError(err.strip() or f"could not save the tree aside in {saved}")
    if out.strip() == "inflight":
        await restore_tree(env, saved)
    return saved


async def freeze_ignores(env: Any, saved: PurePosixPath) -> list[str]:
    done, out, err = await sh(env, f"freeze {quoted(saved)}")
    if done:
        raise RuntimeError(f"could not hold the .gitignore files: {err.strip()}")
    return sorted({rel for rel in out.split("\0") if rel})


async def restore_tree(env: Any, saved: PurePosixPath) -> None:
    done, _, err = await sh(env, f"restore {quoted(saved)}")
    if done:
        raise RuntimeError(f"could not put the tree back from {saved}: {err.strip()}")


async def drop_saved(env: Any, saved: PurePosixPath) -> None:
    done, _, err = await sh(env, f"drop {quoted(saved)}")
    if done == 3:
        print(f"could not delete {saved}.dropping: {err.strip()}")
    elif done:
        raise RuntimeError(f"could not drop the revert point {saved}: {err.strip()}")


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


async def left_out(
    env: Any, entries: dict[str, str], limit: int, at: Any = "."
) -> dict[str, str]:
    files = [rel for rel, what in entries.items() if what == "f"]
    big: dict[str, int] = {}
    if files:
        async with scratch(env) as where:
            listing = where / "files"
            await env.write(
                str(listing), "".join(f"./{rel}\0" for rel in files).encode()
            )
            done, out, err = await sh(
                env,
                f"cd {quoted(at)} || exit 1\n"
                "xargs -0 -r sh -c"
                f" 'exec find -P \"$@\" -prune -type f -size +{limit}c -print0' sh"
                f" < {quoted(listing)} |\n"
                "  while IFS= read -r -d '' rel; do\n"
                '    size=$(wc -c < "$rel") || exit 1\n'
                '    printf \'%s\\0%s\\0\' "${size//[[:space:]]/}" "${rel#./}"\n'
                "  done",
            )
        if done:
            raise RuntimeError(f"could not size the files under {at}: {err.strip()}")
        fields = out.split("\0")
        big = {
            rel: int(size)
            for size, rel in zip(fields[::2], fields[1::2], strict=False)
            if size
        }
    found: dict[str, str] = {}
    for rel, what in entries.items():
        if what == "d":
            found[rel] = "a nested repository"
        elif rel in big:
            found[rel] = f"{big[rel] / MIB:,.1f} MB"
    return found


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


def history_repo(store: PurePosixPath) -> PurePosixPath:
    return store.parent / "history.git"


def epoch_ref(store: PurePosixPath, epoch: int) -> str:
    return f"refs/runs/{store.name}/epoch-{epoch:03d}"


async def _open_history(env: Any, store: PurePosixPath) -> PurePosixPath:
    history = history_repo(store)
    done, _, err = await sh(
        env,
        f"h={quoted(history)}\n"
        'if [ -L "$h" ]; then exit 3; fi\n'
        '[ -e "$h/HEAD" ] || git init --quiet --bare "$h"',
    )
    if done == 3:
        raise RuntimeError(f"history repository is a symlink: {history}")
    if done:
        raise RuntimeError(f"could not create {history}: {err.strip()}")
    return history


async def _commit_tree(
    env: Any,
    history: PurePosixPath,
    work_tree: Any,
    entries: list[str],
    message: str,
    parent: str = "",
) -> str:
    at = f"--git-dir={history}"
    async with scratch(env) as where:
        index = str(where / "index")
        if entries:
            paths = where / "paths"
            await env.write(str(paths), "\0".join(entries).encode())
            added = await git(
                env,
                "--literal-pathspecs",
                at,
                "--work-tree=.",
                "add",
                "--force",
                f"--pathspec-from-file={paths}",
                "--pathspec-file-nul",
                index=index,
                cwd=work_tree,
            )
            if added[0]:
                return ""
        tree = await git(env, at, "write-tree", index=index)
    if tree[0]:
        return ""
    parents = ("-p", parent) if parent else ()
    made = await git(
        env, at, *_COMMITTING, "commit-tree", tree[1].strip(), *parents, "-m", message
    )
    return "" if made[0] else made[1].strip()


async def erase_history(
    env: Any, store: PurePosixPath, epoch: int, limit: int, title: str = ""
) -> bool:
    history = await _open_history(env, store)
    entries = await listed(env)
    out = await left_out(env, entries, limit)
    if (await sh(env, "remove ./.git && [ ! -e .git ] && [ ! -L .git ]"))[0]:
        print("the .git entry could not be removed; git was not run")
        return False
    if (await git(env, "-c", "init.defaultBranch=main", "init", "-q"))[0]:
        return False
    try:
        try:
            exclude = await env.read(".git/info/exclude")
        except EnvFileNotFound:
            exclude = b""
        added = "".join(_pattern(rel) + "\n" for rel in out if "\n" not in rel)
        await env.write(".git/info/exclude", exclude + added.encode())
        await env.write(
            ".git/hooks/pre-commit",
            _HOOK.format(limit=limit, mb=limit / MIB).encode(),
        )
    except EnvError:
        return False
    if (await sh(env, "chmod 755 .git/hooks/pre-commit"))[0]:
        return False
    message = (title or f"epoch {epoch}: distilled tree") + _noted(out, limit)
    commit = await _commit_tree(
        env, history, ".", [rel for rel in entries if rel not in out], message
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
        if (await git(env, *step))[0]:
            return False
    if out:
        print(
            f"epoch {epoch}: {len(out)} entr(ies) left out of git:{_noted(out, limit)}"
        )
    return True


async def archive_history(
    env: Any, saved: PurePosixPath, store: PurePosixPath, epoch: int, limit: int
) -> str | None:
    history = await _open_history(env, store)
    at = f"--git-dir={history}"
    ref = epoch_ref(store, epoch)
    parent = ""
    if await kind(env, saved / ".git") != "-":
        refs = await git(
            env, at, "fetch", "-q", "--no-tags", str(saved), f"+refs/*:{ref}.refs/*"
        )
        head = await git(
            env, at, "fetch", "-q", "--no-tags", str(saved), f"+HEAD:{ref}.head"
        )
        if not head[0]:
            parent = (await git(env, at, "rev-parse", f"{ref}.head"))[1].strip()
        elif refs[0]:
            kept = history.parent / f"unreadable-{store.name}-epoch-{epoch:03d}.git"
            copied = await sh(
                env,
                f"remove {quoted(kept)} && cp -PRp -- {quoted(saved / '.git')} {quoted(kept)}",
            )
            if copied[0]:
                print(
                    f"epoch {epoch}: git could not read the replaced history, and it"
                    f" could not be kept at {kept}: {copied[2].strip()}"
                )
                return None
            print(
                f"epoch {epoch}: git could not read the replaced history; kept at {kept}"
            )
    entries = await listed(env, saved)
    out = await left_out(env, entries, limit, saved)
    message = f"epoch {epoch}: the tree before cleaning" + _noted(out, limit)
    commit = await _commit_tree(
        env, history, saved, [rel for rel in entries if rel not in out], message, parent
    )
    if not commit or (await git(env, at, "update-ref", ref, commit))[0]:
        print(f"epoch {epoch}: the tree before cleaning could not be archived")
        return None
    return ref


async def link_history(env: Any, store: PurePosixPath, epoch: int) -> None:
    at = f"--git-dir={history_repo(store)}"
    ref = epoch_ref(store, epoch)
    grafted = await git(env, at, "replace", "-f", "--graft", f"{ref}.distilled", ref)
    if grafted[0]:
        print(f"epoch {epoch}: the distilled commit could not be chained to {ref}")
    await git(env, at, "repack", "-d", "-q")
    await git(env, at, "gc", "--auto", "--quiet")


async def run_check(env: Any, command: str, log: PurePosixPath) -> bool:
    status = PurePosixPath(f"{log}.status")
    script = (
        f"mkdir -p -- {quoted(log.parent)} || exit 125\n"
        f"rm -f -- {quoted(status)}\n"
        f"sh -c {quoted(command)} > {quoted(log)} 2>&1 < /dev/null\n"
        f'printf %s "$?" > {quoted(status)}\n'
        "kill -KILL -- -$$ 2>/dev/null"
    )
    said = ""
    try:
        await env.exec(script, timeout=CHECK_SECONDS)
    except EnvCommandTimeout:
        said = f"\nthe check ran past {CHECK_SECONDS} seconds\n"
    except EnvError as error:
        said = f"\nthe check could not start: {error}\n"
    try:
        ok = not said and (await env.read(str(status))).strip() == b"0"
    except EnvError:
        ok = False
    if said:
        await sh(
            env,
            f"mkdir -p -- {quoted(log.parent)} && printf %s {quoted(said)} >> {quoted(log)}",
        )
    await sh(env, f"rm -f -- {quoted(status)}")
    await _cap(env, log)
    return ok


async def _cap(env: Any, log: PurePosixPath) -> None:
    await sh(
        env,
        f"f={quoted(log)}\n"
        "size=$(wc -c < \"$f\" | tr -d ' ') || exit 0\n"
        f'if [ "$size" -gt {CHECK_LOG_BYTES} ]; then\n'
        f"  {{ printf '[earlier output cut]\\n'; tail -c {CHECK_LOG_BYTES} \"$f\"; }}"
        ' > "$f.cut" && mv -f -- "$f.cut" "$f"\n'
        "fi",
    )


async def tail(env: Any, log: PurePosixPath, lines: int = 20) -> str:
    try:
        text = (await env.read(str(log))).decode("utf-8", errors="replace")
    except EnvError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
