#!/usr/bin/env python3
"""The runtime's own tool: every Git, SQLite and file-tree step of a Git/PR run.

The flow writes this into a run as `shared/bin/pfc-runtime`, beside the lanes' `pfc`, and
runs it through its environment: one command per step, each printing its answer as JSON.
Exit status 3 means the step refused what it was given -- a receipt that does not qualify, a
change outside the task's paths, a deliverable that is not there; any other failure is a
crash, with its traceback on stderr.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, cast

try:
    from pfc_storage import SCHEMA_VERSION, CoordinationStore
except ModuleNotFoundError:
    from .storage import SCHEMA_VERSION, CoordinationStore

if TYPE_CHECKING:
    from collections.abc import Callable

LANES = ("lane-1", "lane-2", "lane-3")
PROTECTED_PREFIXES = (".git", ".flowbench", ".pfc")
REFUSED = 3
ARTIFACT_FILE_LIMIT = 64 * 1024 * 1024
CHECKPOINT_FILE_LIMIT = 1024 * 1024
JSONL_LINE_LIMIT = 128 * 1024
REPORT_BATCH = (12, 128 * 1024)
SYSTEM_REPORT_BATCH = (24, 256 * 1024)
MERGE_PARENT_COUNT = 2
CYCLES_PATTERN = re.compile(r"(?im)^\s*CYCLES\s*:\s*([0-9]+)\s*$")
TOOLS = {
    "pfc": "agent_cli.py",
    "pfc_storage.py": "storage.py",
    "pfc-runtime": "repository.py",
    "pfc-pre-receive": "pre_receive.py",
}
EXECUTABLES = ("pfc", "pfc-runtime")
STORE_METHODS = frozenset(
    {
        "activate_pr",
        "active_review",
        "finalize_merge",
        "ledger",
        "meta",
        "pr",
        "prs",
        "receipt",
        "receipts_after",
        "record_telemetry",
        "reject_pr",
    }
)
_BOUNDARY_PATTERNS = (
    re.compile(r"MUST NOT modify any file except `([^`]+)`", re.IGNORECASE),
    re.compile(r"MUST (?:only )?modify `([^`]+)`", re.IGNORECASE),
)
_EVALUATOR_PATTERN = re.compile(r"MUST run `([^`]+)`", re.IGNORECASE)


class Refused(Exception):
    """A step refused what it was given, which the runtime acts on rather than crashes on."""


@dataclass(frozen=True, slots=True)
class GitRunPaths:
    root: Path

    @property
    def shared(self) -> Path:
        return self.root / "shared"

    @property
    def private(self) -> Path:
        return self.root / "private"

    @property
    def objective(self) -> Path:
        return self.root / "objective.md"

    @property
    def reports(self) -> Path:
        return self.shared / "reports"

    @property
    def artifacts(self) -> Path:
        return self.shared / "artifacts"

    @property
    def checkpoints(self) -> Path:
        return self.shared / "checkpoints"

    @property
    def central(self) -> Path:
        return self.shared / "repository.git"

    @property
    def planning(self) -> Path:
        return self.shared / "planning-workspace"

    @property
    def integration(self) -> Path:
        return self.shared / "integration-workspace"

    @property
    def bin(self) -> Path:
        return self.shared / "bin"

    @property
    def database(self) -> Path:
        return self.shared / "coordination.sqlite"

    @property
    def events(self) -> Path:
        return self.shared / "coordination-events.jsonl"

    @property
    def official_ledger(self) -> Path:
        return self.shared / "official-ledger.json"

    @property
    def system_reports(self) -> Path:
        return self.shared / "system-reports"

    @property
    def evaluation_artifacts(self) -> Path:
        return self.shared / "evaluations"

    @property
    def object_store(self) -> Path:
        return self.shared / "objects"

    @property
    def manifest(self) -> Path:
        return self.shared / "manifest.json"

    @property
    def state_mirror(self) -> Path:
        return self.shared / "state.json"

    @property
    def workspace_map(self) -> Path:
        return self.shared / "workspace-map.json"

    @property
    def leaderboard(self) -> Path:
        return self.shared / "leaderboard.json"

    def lane(self, lane: str) -> Path:
        return self.private / lane

    def report(self, lane: str) -> Path:
        return self.reports / f"{lane}.jsonl"

    def system_report(self, lane: str) -> Path:
        return self.system_reports / f"{lane}.jsonl"

    def artifact_root(self, lane: str) -> Path:
        return self.artifacts / lane

    def checkpoint(self, lane: str) -> Path:
        return self.checkpoints / f"{lane}.json"

    def store(self) -> CoordinationStore:
        return CoordinationStore(self.database, self.events)


def git(
    *arguments: str,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=text,
    )


def configure_clone(path: Path, *, lane: str, run_root: Path) -> None:
    git("config", "user.name", f"PFC {lane}", cwd=path)
    git("config", "user.email", f"{lane}@parallel-flame-chase.invalid", cwd=path)
    git("config", "pfc.run-root", str(run_root), cwd=path)
    git("config", "pfc.lane", lane, cwd=path)
    git("config", "advice.detachedHead", "false", cwd=path)


def discover_allowed_paths(source: Path, task_text: str | None = None) -> list[str]:
    documents = (source / "TASK.md", source / ".flowbench" / "task.md")
    discovered: list[str] = []
    for document in documents:
        if not document.is_file():
            continue
        text = document.read_text(encoding="utf-8")
        for pattern in _BOUNDARY_PATTERNS:
            discovered.extend(pattern.findall(text))
    if task_text:
        for pattern in _BOUNDARY_PATTERNS:
            discovered.extend(pattern.findall(task_text))
    canonical = []
    for raw in discovered:
        value = PurePosixPath(raw.rstrip("/")).as_posix()
        if value.startswith(("../", "/")):
            continue
        if raw.endswith("/"):
            value = f"{value}/**"
        if value not in canonical:
            canonical.append(value)
    if canonical:
        return canonical

    baseline: list[str] = []
    for folder, directories, files in os.walk(source, followlinks=False):
        relative_folder = Path(folder).relative_to(source)
        kept_directories: list[str] = []
        for name in directories:
            relative = (relative_folder / name).as_posix()
            path = Path(folder, name)
            if any(
                relative == prefix or relative.startswith(f"{prefix}/")
                for prefix in PROTECTED_PREFIXES
            ):
                continue
            if path.is_symlink():
                baseline.append(relative)
            else:
                kept_directories.append(name)
        directories[:] = kept_directories
        for name in files:
            relative = (relative_folder / name).as_posix()
            if not any(
                relative == prefix or relative.startswith(f"{prefix}/")
                for prefix in PROTECTED_PREFIXES
            ):
                baseline.append(relative)
    return sorted(set(baseline))


def discover_evaluator_command(source: Path, task_text: str | None = None) -> list[str]:
    texts = [task_text] if task_text else []
    for document in (source / "TASK.md", source / ".flowbench" / "task.md"):
        if document.is_file():
            texts.append(document.read_text(encoding="utf-8"))
    for text in texts:
        if text is None:
            continue
        match = _EVALUATOR_PATTERN.search(text)
        if match:
            return shlex.split(match.group(1))
    return []


def path_allowed(path: str, allowed_paths: list[str]) -> bool:
    canonical = PurePosixPath(path).as_posix()
    if canonical == ".git" or any(
        canonical == prefix or canonical.startswith(f"{prefix}/")
        for prefix in PROTECTED_PREFIXES
    ):
        return False
    return any(
        pattern in {"**", canonical} or fnmatch.fnmatchcase(canonical, pattern)
        for pattern in allowed_paths
    )


def changed_paths(repository: Path, base: str, head: str) -> list[str]:
    result = cast(
        "subprocess.CompletedProcess[bytes]",
        git("diff", "--name-only", "-z", base, head, cwd=repository, text=False),
    )
    return [os.fsdecode(value) for value in result.stdout.split(b"\0") if value]


def validate_changed_paths(
    repository: Path, base: str, head: str, allowed_paths: list[str]
) -> list[str]:
    paths = changed_paths(repository, base, head)
    rejected = [path for path in paths if not path_allowed(path, allowed_paths)]
    if rejected:
        raise ValueError(f"changes exceed the frozen task boundary: {rejected}")
    return paths


def inspect_workspace_stats(source: Path) -> dict[str, int]:
    regular_files = 0
    total = 0
    for folder, directories, files in os.walk(source):
        for name in [*directories, *files]:
            path = Path(folder, name)
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                regular_files += 1
                total += info.st_size
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                raise ValueError(f"workspace contains unsupported special file: {path}")
    return {"regular_files": regular_files, "total_bytes": total}


def _copy_source(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for entry in source.iterdir():
        if entry.name == ".git":
            continue
        target = destination / entry.name
        if entry.is_symlink():
            target.symlink_to(entry.readlink())
        elif entry.is_dir():
            shutil.copytree(entry, target, symlinks=True)
        else:
            shutil.copy2(entry, target, follow_symlinks=False)


def initialize_shadow_repository(
    paths: GitRunPaths, source: Path, *, lanes: tuple[str, ...] = LANES
) -> str:
    _copy_source(source, paths.planning)
    git("init", "-b", "main", cwd=paths.planning)
    configure_clone(paths.planning, lane="planning", run_root=paths.root)
    git("add", "--all", cwd=paths.planning)
    git(
        "commit",
        "--allow-empty",
        "-m",
        "chore: freeze run baseline",
        cwd=paths.planning,
    )
    git("init", "--bare", "--initial-branch=main", str(paths.central))
    git("remote", "add", "origin", str(paths.central), cwd=paths.planning)
    git("push", "origin", "main", cwd=paths.planning)

    for lane in lanes:
        git("clone", "--no-hardlinks", str(paths.central), str(paths.lane(lane)))
        configure_clone(paths.lane(lane), lane=lane, run_root=paths.root)
    git("clone", "--no-hardlinks", str(paths.central), str(paths.integration))
    configure_clone(paths.integration, lane="orchestrator", run_root=paths.root)

    hook = paths.central / "hooks" / "pre-receive"
    shutil.copy2(paths.bin / "pfc-pre-receive", hook)
    hook.chmod(0o755)
    paths.system_reports.mkdir(parents=True, exist_ok=True)
    paths.evaluation_artifacts.mkdir(parents=True, exist_ok=True)
    paths.object_store.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        paths.system_report(lane).touch()
    return main_sha(paths.central)


def initialize_run(
    paths: GitRunPaths, source: Path, *, run_id: str, lanes: tuple[str, ...] = LANES
) -> str:
    for directory in (paths.reports, paths.checkpoints, paths.private):
        directory.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        paths.artifact_root(lane).mkdir(parents=True, exist_ok=True)
        paths.report(lane).touch()
    baseline = initialize_shadow_repository(paths, source, lanes=lanes)
    objective = paths.objective.read_text(encoding="utf-8")
    store = paths.store()
    store.initialize(
        run_id=run_id,
        git_pr_enabled=True,
        global_knowledge_enabled=False,
        experiment_memory_enabled=False,
        lanes=lanes,
        allowed_paths=discover_allowed_paths(source, objective),
        trusted_evaluator_command=discover_evaluator_command(source, objective),
    )
    store.record_telemetry(
        "branch_protection_installed",
        {
            "repository": str(paths.central),
            "hook": str(paths.central / "hooks/pre-receive"),
        },
    )
    return baseline


def validate_run(
    paths: GitRunPaths, *, run_id: str, lanes: tuple[str, ...] = LANES
) -> None:
    directories = (
        paths.root,
        paths.shared,
        paths.private,
        paths.reports,
        paths.artifacts,
        paths.checkpoints,
        paths.central,
        paths.planning,
        paths.integration,
        paths.bin,
        paths.system_reports,
        paths.evaluation_artifacts,
        paths.object_store,
        *(paths.lane(lane) for lane in lanes),
        *(paths.artifact_root(lane) for lane in lanes),
    )
    required = (
        *(paths.bin / name for name in TOOLS),
        paths.database,
        paths.events,
        paths.central / "hooks" / "pre-receive",
        *(paths.report(lane) for lane in lanes),
        *(paths.system_report(lane) for lane in lanes),
    )
    optional = (
        paths.objective,
        paths.manifest,
        paths.state_mirror,
        paths.workspace_map,
        paths.leaderboard,
        paths.official_ledger,
    )
    for path in directories:
        try:
            info = path.lstat()
        except OSError as why:
            raise RuntimeError(f"runtime directory is missing: {path}") from why
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"runtime directory was replaced or linked: {path}")
    for path in (*required, *optional):
        try:
            info = path.lstat()
        except OSError as why:
            if path in optional:
                continue
            raise RuntimeError(f"runtime file is missing: {path}") from why
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"runtime file was replaced or linked: {path}")
    store = paths.store()
    if store.meta("run_id") != run_id:
        raise RuntimeError("coordination database belongs to another run")
    if store.meta("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("coordination database is incompatible with this runtime")
    git("fsck", "--no-dangling", cwd=paths.central)


def main_sha(repository: Path) -> str:
    return cast(
        "subprocess.CompletedProcess[str]",
        git("rev-parse", "refs/heads/main", cwd=repository),
    ).stdout.strip()


def commit_message(repository: Path, commit_sha: str) -> str:
    return cast(
        "subprocess.CompletedProcess[str]",
        git("show", "-s", "--format=%B", commit_sha, cwd=repository),
    ).stdout


def pr_trailer(repository: Path, commit_sha: str) -> str | None:
    message = commit_message(repository, commit_sha)
    matches = re.findall(r"^PFC-PR:\s*(PR\d{6})\s*$", message, re.MULTILINE)
    return matches[-1] if matches else None


def commit_parents(repository: Path, commit_sha: str) -> list[str]:
    line = cast(
        "subprocess.CompletedProcess[str]",
        git("rev-list", "--parents", "-n", "1", commit_sha, cwd=repository),
    ).stdout.strip()
    return line.split()[1:]


def create_fast_path_merge(
    paths: GitRunPaths, *, pr_id: str, prior_sha: str, head_sha: str
) -> str:
    git("fetch", "origin", cwd=paths.integration)
    current = main_sha(paths.central)
    if current != prior_sha:
        raise RuntimeError("authoritative main changed during fast-path selection")
    tree_sha = cast(
        "subprocess.CompletedProcess[str]",
        git("rev-parse", f"{head_sha}^{{tree}}", cwd=paths.integration),
    ).stdout.strip()
    message = f"Merge receipt-verified candidate {pr_id}\n\nPFC-PR: {pr_id}\n"
    commit = subprocess.run(
        [
            "git",
            "commit-tree",
            tree_sha,
            "-p",
            prior_sha,
            "-p",
            head_sha,
        ],
        cwd=paths.integration,
        input=message,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git("push", "origin", f"{commit}:refs/heads/main", cwd=paths.integration)
    return commit


def _blob(repository: Path, commit_sha: str, path: str) -> bytes | None:
    result = git(
        "show", f"{commit_sha}:{path}", cwd=repository, check=False, text=False
    )
    return cast("bytes", result.stdout) if result.returncode == 0 else None


def _mode(repository: Path, commit_sha: str, path: str) -> int | None:
    result = cast(
        "subprocess.CompletedProcess[str]",
        git("ls-tree", commit_sha, "--", path, cwd=repository, check=False),
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return int(result.stdout.split(maxsplit=1)[0], 8)


def _matches_old_source(path: Path, expected: bytes | None, mode: int | None) -> bool:
    if expected is None:
        return not path.exists() and not path.is_symlink()
    if mode is not None and stat.S_IFMT(mode) == stat.S_IFLNK:
        return path.is_symlink() and os.fsencode(path.readlink()) == expected
    return path.is_file() and not path.is_symlink() and path.read_bytes() == expected


def _atomic_blob(path: Path, data: bytes, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is not None and stat.S_IFMT(mode) == stat.S_IFLNK:
        temporary = path.with_name(f".{path.name}.pfc-link")
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        temporary.symlink_to(os.fsdecode(data))
        temporary.replace(path)
        return
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.pfc-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o755 if mode is not None and mode & 0o111 else 0o644)
        temporary.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def publish_main(
    repository: Path,
    source: Path,
    *,
    prior_sha: str,
    merge_sha: str,
) -> list[str]:
    paths = changed_paths(repository, prior_sha, merge_sha)
    for relative in paths:
        target = source / relative
        old = _blob(repository, prior_sha, relative)
        old_mode = _mode(repository, prior_sha, relative)
        new = _blob(repository, merge_sha, relative)
        new_mode = _mode(repository, merge_sha, relative)
        if not _matches_old_source(target, old, old_mode) and not _matches_old_source(
            target, new, new_mode
        ):
            raise RuntimeError(f"source changed outside Git/PR publication: {relative}")
    for relative in paths:
        target = source / relative
        new = _blob(repository, merge_sha, relative)
        mode = _mode(repository, merge_sha, relative)
        if new is None:
            if target.is_dir() and not target.is_symlink():
                raise RuntimeError(f"approved file path became a directory: {relative}")
            target.unlink(missing_ok=True)
        else:
            _atomic_blob(target, new, mode)
    return paths


def observe_main(paths: GitRunPaths, source: Path, *, prior: str) -> dict[str, Any]:
    current = main_sha(paths.central)
    if current == prior:
        return {"main": current}
    parents = commit_parents(paths.central, current)
    pr_id = pr_trailer(paths.central, current)
    if pr_id is None or len(parents) != MERGE_PARENT_COUNT or parents[0] != prior:
        raise RuntimeError("protected main advanced with an invalid merge structure")
    store = paths.store()
    if parents[1] != store.pr(pr_id)["head_sha"]:
        raise RuntimeError("protected main does not merge the frozen PR head")
    changed = validate_changed_paths(
        paths.central, prior, current, cast("list[str]", store.meta("allowed_paths"))
    )
    publish_main(paths.central, source, prior_sha=prior, merge_sha=current)
    return {"main": current, "pr_id": pr_id, "changed": changed}


def merge_candidate(
    paths: GitRunPaths, *, pr_id: str, prior_sha: str, head_sha: str
) -> str:
    allowed = cast("list[str]", paths.store().meta("allowed_paths"))
    validate_changed_paths(paths.central, prior_sha, head_sha, allowed)
    return create_fast_path_merge(
        paths, pr_id=pr_id, prior_sha=prior_sha, head_sha=head_sha
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def receipt_score(paths: GitRunPaths, pr_id: str) -> dict[str, object]:
    store = paths.store()
    pr = store.pr(pr_id)
    receipt_id = pr["provisional_receipt_id"]
    if not isinstance(receipt_id, str):
        raise ValueError("PR has no provisional receipt")
    receipt = store.receipt(receipt_id)
    expected_command = cast("list[str]", store.meta("trusted_evaluator_command"))
    recorded_command = json.loads(cast("str", receipt["command_json"]))
    if expected_command and recorded_command != expected_command:
        raise ValueError("receipt did not use the frozen evaluator command")
    if (
        receipt["kind"] != "provisional"
        or int(cast("int", receipt["exit_code"])) != 0
        or receipt["commit_sha"] != pr["head_sha"]
    ):
        raise ValueError("receipt is not a successful frozen-head evaluation")
    tree = cast(
        "subprocess.CompletedProcess[str]",
        git("rev-parse", f"{pr['head_sha']}^{{tree}}", cwd=paths.central),
    ).stdout.strip()
    if receipt["tree_sha"] != tree:
        raise ValueError("receipt tree does not match its PR head")
    evaluation_root = paths.evaluation_artifacts.resolve()
    artifacts: dict[str, Path] = {}
    for path_field, hash_field in (
        ("stdout_path", "stdout_sha256"),
        ("stderr_path", "stderr_sha256"),
    ):
        path = Path(cast("str", receipt[path_field]))
        resolved = path.resolve(strict=True)
        if (
            not resolved.is_relative_to(evaluation_root)
            or path.is_symlink()
            or not path.is_file()
            or _sha256(path) != receipt[hash_field]
        ):
            raise ValueError("receipt artifact integrity check failed")
        artifacts[path_field] = path
    output = artifacts["stdout_path"].read_text(encoding="utf-8", errors="replace")
    matches = CYCLES_PATTERN.findall(output)
    if not matches:
        raise ValueError("official evaluator output has no CYCLES value")
    return {"score": int(matches[-1]), "receipt_id": receipt_id}


def _resolve_artifact(root: Path, raw: str) -> tuple[Path, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("deliverable artifact root was replaced or linked")
    resolved_root = root.resolve(strict=True)
    relative = Path(raw)
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except OSError as why:
        raise ValueError(f"deliverable artifact is missing: {raw}") from why
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"deliverable artifact escapes its lane root: {raw}")
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"deliverable artifact must be a regular file: {raw}")
    if not path.is_file():
        raise ValueError(f"deliverable artifact must be a regular file: {raw}")
    return path, resolved.relative_to(resolved_root).as_posix()


def validate_deliverable(
    root: Path, declared: list[dict[str, str]]
) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in declared:
        path, canonical = _resolve_artifact(root, item["path"])
        if canonical in seen:
            raise ValueError(f"deliverable repeats artifact: {canonical}")
        seen.add(canonical)
        size = path.stat().st_size
        if size > ARTIFACT_FILE_LIMIT:
            raise ValueError(
                f"deliverable artifact exceeds {ARTIFACT_FILE_LIMIT} bytes: {canonical}"
            )
        artifacts.append(
            {
                "path": canonical,
                "description": item["description"],
                "size": size,
                "sha256": _sha256(path),
            }
        )
    return artifacts


def append_jsonl(path: Path, value: object) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, default=str) + "\n").encode()
    if len(encoded) > JSONL_LINE_LIMIT:
        raise ValueError(f"JSONL record exceeds {JSONL_LINE_LIMIT} bytes")
    if path.is_symlink():
        raise ValueError(f"refusing to append through a linked JSONL file: {path}")
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "ab") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def deliveries(
    path: Path,
    offset: object,
    *,
    source: str,
    health: str,
    batch: tuple[int, int],
) -> tuple[list[dict[str, object]], int]:
    """Up to one batch of the JSONL records after `offset`, and where the batch ends."""
    limit, budget = batch
    start = (
        offset
        if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0
        else 0
    )
    if path.stat().st_size < start:
        start = 0
    found: list[dict[str, object]] = []
    end = start
    used = 0
    with path.open("rb") as handle:
        handle.seek(start)
        while len(found) < limit and used < budget:
            at = handle.tell()
            line = handle.readline(JSONL_LINE_LIMIT + 1)
            if not line or not line.endswith(b"\n"):
                break
            if found and used + len(line) > budget:
                break
            end = handle.tell()
            used += len(line)
            envelope: dict[str, object] = {
                "report_id": f"{source}:{at}:{hashlib.sha256(line).hexdigest()[:16]}",
                "source_lane": source,
            }
            if len(line) > JSONL_LINE_LIMIT:
                found.append(
                    {**envelope, "health": f"oversized_{health}", "bytes": len(line)}
                )
                continue
            try:
                loaded: object = json.loads(line)
            except json.JSONDecodeError as why:
                found.append(
                    {**envelope, "health": f"invalid_{health}_json", "error": why.msg}
                )
                continue
            if not isinstance(loaded, dict):
                found.append({**envelope, "health": f"invalid_{health}_shape"})
                continue
            found.append({**envelope, "report": loaded})
    return found, end


def unread(paths: GitRunPaths, lane: str, cursors: dict[str, Any]) -> dict[str, object]:
    reports: list[dict[str, object]] = []
    acknowledgements: dict[str, int] = {}
    for source in LANES:
        if source == lane:
            continue
        found, acknowledgements[source] = deliveries(
            paths.report(source),
            cursors.get(source, 0),
            source=source,
            health="report",
            batch=REPORT_BATCH,
        )
        reports.extend(found)
    found, acknowledgements["system"] = deliveries(
        paths.system_report(lane),
        cursors.get("system", 0),
        source="system",
        health="system_report",
        batch=SYSTEM_REPORT_BATCH,
    )
    return {"reports": [*reports, *found], "acknowledgements": acknowledgements}


def checkpoint(paths: GitRunPaths, lane: str) -> dict[str, object]:
    path = paths.checkpoint(lane)
    try:
        if path.is_symlink() or not path.is_file():
            return {"fingerprint": None, "text": None}
        info = path.stat()
        if info.st_size > CHECKPOINT_FILE_LIMIT:
            return {
                "fingerprint": [info.st_size, info.st_mtime_ns, "oversized"],
                "text": None,
            }
        data = path.read_bytes()
    except OSError:
        return {"fingerprint": None, "text": None}
    return {
        "fingerprint": [
            info.st_size,
            info.st_mtime_ns,
            hashlib.sha256(data).hexdigest(),
        ],
        "text": data.decode("utf-8", errors="replace"),
    }


def poll(paths: GitRunPaths, *, after: int) -> dict[str, object]:
    store = paths.store()
    receipts, end = store.receipts_after(after)
    return {
        "receipts": receipts,
        "end": end,
        "main": main_sha(paths.central),
        "active": store.active_review(),
        "ready": store.prs(status="ready"),
    }


def _command_stats(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    del paths
    return inspect_workspace_stats(arguments.source)


def _command_init(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    return {
        "baseline": initialize_run(paths, arguments.source, run_id=arguments.run_id)
    }


def _command_check(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    validate_run(paths, run_id=arguments.run_id)
    store = paths.store()
    return {"ledger": store.ledger(), "prs": store.prs()}


def _command_store(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    method: Callable[..., object] = getattr(paths.store(), arguments.method)
    return method(**json.loads(arguments.arguments))


def _command_poll(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    return poll(paths, after=arguments.after)


def _command_score(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    try:
        return receipt_score(paths, arguments.pr_id)
    except (KeyError, OSError, ValueError) as why:
        raise Refused(why) from why


def _command_merge(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    try:
        merged = merge_candidate(
            paths,
            pr_id=arguments.pr_id,
            prior_sha=arguments.prior,
            head_sha=arguments.head,
        )
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as why:
        raise Refused(why) from why
    return {"merge_sha": merged}


def _command_observe(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    return observe_main(paths, arguments.source, prior=arguments.prior)


def _command_artifacts(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    try:
        return validate_deliverable(
            paths.artifact_root(arguments.lane), json.loads(arguments.declared)
        )
    except (OSError, ValueError) as why:
        raise Refused(why) from why


def _command_report(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    said = json.loads(arguments.said)
    try:
        append_jsonl(paths.report(arguments.lane), said["record"])
    except (OSError, ValueError) as why:
        raise Refused(why) from why
    paths.store().record_telemetry(said["kind"], said["payload"], lane=arguments.lane)
    return None


def _command_system(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    said = json.loads(arguments.said)
    targets = [target for target in said["targets"] if target in LANES]
    for target in targets:
        append_jsonl(paths.system_report(target), said["record"])
    paths.store().record_telemetry(
        "system_report_published",
        {
            "report_id": said["record"]["report_id"],
            "targets": targets,
            "kind": said["record"]["kind"],
        },
    )
    return None


def _command_unread(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    return unread(paths, arguments.lane, json.loads(arguments.cursors))


def _command_checkpoint(paths: GitRunPaths, arguments: argparse.Namespace) -> object:
    return checkpoint(paths, arguments.lane)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="pfc-runtime")
    root.add_argument("--root", required=True, type=Path)
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("stats").add_argument("source", type=Path)
    init = commands.add_parser("init")
    init.add_argument("source", type=Path)
    init.add_argument("--run-id", required=True)
    commands.add_parser("check").add_argument("--run-id", required=True)
    store = commands.add_parser("store")
    store.add_argument("method", choices=sorted(STORE_METHODS))
    store.add_argument("arguments", nargs="?", default="{}")
    commands.add_parser("poll").add_argument("--after", required=True, type=int)
    commands.add_parser("score").add_argument("pr_id")
    merge = commands.add_parser("merge")
    merge.add_argument("pr_id")
    merge.add_argument("prior")
    merge.add_argument("head")
    observe = commands.add_parser("observe")
    observe.add_argument("source", type=Path)
    observe.add_argument("--prior", required=True)
    artifacts = commands.add_parser("artifacts")
    artifacts.add_argument("lane", choices=LANES)
    artifacts.add_argument("declared")
    report = commands.add_parser("report")
    report.add_argument("lane", choices=LANES)
    report.add_argument("said")
    commands.add_parser("system").add_argument("said")
    unread_command = commands.add_parser("unread")
    unread_command.add_argument("lane", choices=LANES)
    unread_command.add_argument("cursors")
    commands.add_parser("checkpoint").add_argument("lane", choices=LANES)
    return root


COMMANDS: dict[str, Callable[[GitRunPaths, argparse.Namespace], object]] = {
    "stats": _command_stats,
    "init": _command_init,
    "check": _command_check,
    "store": _command_store,
    "poll": _command_poll,
    "score": _command_score,
    "merge": _command_merge,
    "observe": _command_observe,
    "artifacts": _command_artifacts,
    "report": _command_report,
    "system": _command_system,
    "unread": _command_unread,
    "checkpoint": _command_checkpoint,
}


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        answer = COMMANDS[arguments.command](GitRunPaths(arguments.root), arguments)
    except Refused as why:
        print(str(why) or type(why.__cause__).__name__, file=sys.stderr)
        return REFUSED
    print(json.dumps(answer, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
