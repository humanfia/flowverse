from __future__ import annotations

import datetime as dt
import hashlib
import os
import posixpath
import uuid
from pathlib import PurePosixPath
from typing import Any

from .tree import kind, quoted, sh

_INSIDE = "HUMANIZE_HOME must sit outside the cleaned repository"


def workspace_key(source: str) -> str:
    plain = "".join(character if character.isalnum() else "-" for character in source)
    readable = "-".join(part for part in plain.split("-") if part)[-80:] or "root"
    digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:12]
    return f"{readable}-{digest}"


async def managed_parent(flow: str, env: Any) -> PurePosixPath:
    done, out, err = await sh(
        env, 'printf "%s\\0%s" "$(pwd -P)" "${HUMANIZE_HOME:-$HOME/.humanize}"'
    )
    source, _, home = out.partition("\0")
    if done or not source.startswith("/") or not home:
        raise RuntimeError(
            f"could not find the workspace or HUMANIZE_HOME: {err.strip()}"
        )
    parent = posixpath.normpath(
        posixpath.join(source, home, flow, workspace_key(source))
    )
    if PurePosixPath(parent).is_relative_to(source):
        raise RuntimeError(_INSIDE)
    done, out, err = await sh(
        env,
        f"p={quoted(parent)} rest=\n"
        'while [ ! -d "$p" ]; do rest=/${p##*/}$rest; p=${p%/*}; p=${p:-/}; done\n'
        'resolved=$(cd "$p" && pwd -P) || exit 1\n'
        'printf %s "$resolved$rest"',
    )
    if done or not out.startswith("/"):
        raise RuntimeError(f"could not resolve {parent}: {err.strip()}")
    resolved = PurePosixPath(out)
    if resolved.is_relative_to(source):
        raise RuntimeError(_INSIDE)
    return resolved


async def open_store(flow: str, env: Any, state: Any) -> tuple[PurePosixPath, bool]:
    parent = await managed_parent(flow, env)
    if "run_id" in state:
        run_id = state["run_id"]
        if not isinstance(run_id, str) or run_id in ("", ".", "..") or "/" in run_id:
            raise ValueError("resumable cleanup state has incomplete run storage")
        root = parent / run_id
        if await kind(env, root) != "d":
            raise RuntimeError("resumable cleanup storage is missing or linked")
        return root, True
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}-{uuid.uuid4().hex[:10]}"
    root = parent / run_id
    done, _, err = await sh(
        env, f"mkdir -p -- {quoted(parent)} && mkdir -- {quoted(root)}"
    )
    if done:
        raise RuntimeError(f"could not make run storage at {root}: {err.strip()}")
    state["run_id"] = run_id
    state["manifest_ready"] = False
    return root, False
