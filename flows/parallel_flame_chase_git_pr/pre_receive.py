#!/usr/bin/env python3
"""Central-repository branch protection for the local GitHub-like protocol."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath

MERGE_REV_LIST_FIELD_COUNT = 3


def run_git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def allowed(path: str, patterns: list[str]) -> bool:
    import fnmatch

    canonical = PurePosixPath(path).as_posix()
    if any(
        canonical == prefix or canonical.startswith(f"{prefix}/")
        for prefix in (".git", ".flowbench", ".pfc")
    ):
        return False
    return any(
        pattern in {"**", canonical} or fnmatch.fnmatchcase(canonical, pattern)
        for pattern in patterns
    )


def reject(message: str) -> None:
    print(f"parallel-flame branch protection: {message}", file=sys.stderr)
    raise SystemExit(1)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def valid_receipt_artifacts(receipt: sqlite3.Row, shared: Path) -> bool:
    """Verify that staged stdout/stderr still match their bound hashes."""
    root = (shared / "evaluations").resolve()
    for path_field, hash_field in (
        ("stdout_path", "stdout_sha256"),
        ("stderr_path", "stderr_sha256"),
    ):
        path = Path(receipt[path_field])
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        if (
            not resolved.is_relative_to(root)
            or path.is_symlink()
            or not path.is_file()
            or digest(path) != receipt[hash_field]
        ):
            return False
    return True


def protect_main(repository: Path, database: Path, old: str, new: str) -> None:
    parents = run_git(repository, "rev-list", "--parents", "-n", "1", new).split()
    if len(parents) != MERGE_REV_LIST_FIELD_COUNT or parents[1] != old:
        reject(
            "main accepts only a two-parent merge whose first parent is current main"
        )
    message = run_git(repository, "show", "-s", "--format=%B", new)
    trailers = re.findall(r"^PFC-PR:\s*(PR\d{6})\s*$", message, re.MULTILINE)
    if not trailers:
        reject("merge commit is missing PFC-PR trailer")
    pr_id = trailers[-1]
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        pr = connection.execute(
            "SELECT * FROM pull_requests WHERE id=?", (pr_id,)
        ).fetchone()
        if pr is None or pr["status"] != "reviewing":
            reject("PFC-PR does not name the selected fast-path candidate")
        if parents[2] != pr["head_sha"]:
            reject("merge second parent is not the frozen PR head")
        merge_tree = run_git(repository, "rev-parse", f"{new}^{{tree}}").strip()
        head_tree = run_git(
            repository, "rev-parse", f"{pr['head_sha']}^{{tree}}"
        ).strip()
        if merge_tree != head_tree:
            reject("fast-path merge tree is not exactly the evaluated PR head tree")
        receipt = connection.execute(
            """
            SELECT * FROM receipts
            WHERE id=? AND (pr_id IS NULL OR pr_id=?) AND lane=? AND commit_sha=?
                AND tree_sha=? AND kind='provisional' AND exit_code=0
            """,
            (
                pr["provisional_receipt_id"],
                pr_id,
                pr["lane"],
                pr["head_sha"],
                head_tree,
            ),
        ).fetchone()
        if receipt is None or not valid_receipt_artifacts(receipt, database.parent):
            reject("provisional receipt identity/artifacts failed integrity validation")
        row = connection.execute(
            "SELECT value_json FROM meta WHERE key='allowed_paths'"
        ).fetchone()
        patterns = json.loads(row["value_json"]) if row else ["**"]
        row = connection.execute(
            "SELECT value_json FROM meta WHERE key='trusted_evaluator_command'"
        ).fetchone()
        trusted_command = json.loads(row["value_json"]) if row else []
        if trusted_command and json.loads(receipt["command_json"]) != trusted_command:
            reject("receipt command is not the frozen official evaluator command")
    finally:
        connection.close()
    paths = [
        path
        for path in run_git(repository, "diff", "--name-only", "-z", old, new).split(
            "\0"
        )
        if path
    ]
    invalid = [path for path in paths if not allowed(path, patterns)]
    if invalid:
        reject(f"merge changes protected paths: {invalid}")
    candidate_paths = [
        path
        for path in run_git(
            repository, "diff", "--name-only", "-z", pr["base_sha"], pr["head_sha"]
        ).split("\0")
        if path
    ]
    invalid_candidate = [
        path for path in candidate_paths if not allowed(path, patterns)
    ]
    if invalid_candidate:
        reject(f"candidate changes protected paths: {invalid_candidate}")


def protect_lane(database: Path, reference: str, new: str) -> None:
    """Keep a ready/reviewing branch at its frozen registered head."""
    if set(new) == {"0"}:
        reject("run-owned lane branches are retained and cannot be deleted")
    branch = reference.removeprefix("refs/heads/")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT status, head_sha FROM pull_requests
            WHERE branch=? ORDER BY created_at DESC LIMIT 1
            """,
            (branch,),
        ).fetchone()
    finally:
        connection.close()
    if (
        row is not None
        and row["status"] in {"ready", "reviewing"}
        and new != row["head_sha"]
    ):
        reject("a ready/reviewing PR branch is frozen at its registered head")


def main() -> None:
    repository = Path.cwd()
    shared = Path(__file__).resolve().parents[2]
    database = shared / "coordination.sqlite"
    for line in sys.stdin:
        old, new, reference = line.strip().split()
        if reference == "refs/heads/main":
            protect_main(repository, database, old, new)
        elif re.fullmatch(r"refs/heads/lane-[1-4]/[^\s]+", reference):
            protect_lane(database, reference, new)
        else:
            reject(f"unsupported pushed reference: {reference}")


if __name__ == "__main__":
    main()
