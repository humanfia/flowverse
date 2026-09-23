"""Where a run keeps what must not live in the repository it cleans."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from hmz.flows import home


def workspace_key(source: Path) -> str:
    """A readable, collision-safe directory name for one working repository."""
    plain = "".join(
        character if character.isalnum() else "-" for character in str(source)
    )
    readable = "-".join(part for part in plain.split("-") if part)[-80:] or "root"
    digest = hashlib.sha256(os.fsencode(source)).hexdigest()[:12]
    return f"{readable}-{digest}"


def managed_parent(flow: str, source: Path) -> Path:
    """The directory under HUMANIZE_HOME that holds this workspace's runs."""
    parent = (home() / flow / workspace_key(source)).resolve()
    if parent.is_relative_to(source):
        raise RuntimeError("HUMANIZE_HOME must sit outside the cleaned repository")
    return parent


def open_store(flow: str, source: Path, state: dict[str, Any]) -> tuple[Path, bool]:
    """Reopen the run root a resumed run recorded, or create a fresh one."""
    parent = managed_parent(flow, source)
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
    root.mkdir(exist_ok=False)
    state.update(run_id=run_id, run_root=str(root), manifest_ready=False)
    return root, False
