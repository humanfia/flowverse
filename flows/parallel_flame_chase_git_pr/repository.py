"""Run-owned shadow repository, protected refs, and source publication."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .storage import CoordinationStore

PROTECTED_PREFIXES = (".git", ".flowbench", ".pfc")
_BOUNDARY_PATTERNS = (
    re.compile(r"MUST NOT modify any file except `([^`]+)`", re.IGNORECASE),
    re.compile(r"MUST (?:only )?modify `([^`]+)`", re.IGNORECASE),
)
_EVALUATOR_PATTERN = re.compile(r"MUST run `([^`]+)`", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class GitRunPaths:
    """All Git-owned paths for one run."""

    root: Path

    @property
    def shared(self) -> Path:
        return self.root / "shared"

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
    def knowledge_json(self) -> Path:
        return self.shared / "knowledge-index.json"

    @property
    def knowledge_markdown(self) -> Path:
        return self.shared / "knowledge.md"

    @property
    def system_reports(self) -> Path:
        return self.shared / "system-reports"

    @property
    def evaluation_artifacts(self) -> Path:
        return self.shared / "evaluations"

    @property
    def object_store(self) -> Path:
        return self.shared / "objects"

    def lane(self, lane: str) -> Path:
        return self.root / "private" / lane


def git(
    *arguments: str,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run Git without a shell and retain stderr for protocol failures."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=check,
        capture_output=True,
        text=text,
    )


def configure_clone(path: Path, *, lane: str, run_root: Path) -> None:
    """Give a clone stable attribution and discoverable run context."""
    git("config", "user.name", f"PFC {lane}", cwd=path)
    git("config", "user.email", f"{lane}@parallel-flame-chase.invalid", cwd=path)
    git("config", "pfc.run-root", str(run_root), cwd=path)
    git("config", "pfc.lane", lane, cwd=path)
    git("config", "advice.detachedHead", "false", cwd=path)


def discover_allowed_paths(source: Path, task_text: str | None = None) -> list[str]:
    """Extract explicit task boundaries, otherwise freeze baseline file paths."""
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
    """Freeze an explicitly mandated evaluator command when the task provides one."""
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
    """Apply the frozen allowlist while always protecting control/evaluator files."""
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
    """Return every path changed between two commits."""
    result = cast(
        "subprocess.CompletedProcess[bytes]",
        git("diff", "--name-only", "-z", base, head, cwd=repository, text=False),
    )
    return [os.fsdecode(value) for value in result.stdout.split(b"\0") if value]


def validate_changed_paths(
    repository: Path, base: str, head: str, allowed_paths: list[str]
) -> list[str]:
    """Reject a ready/merge diff that exceeds the task's frozen boundary."""
    paths = changed_paths(repository, base, head)
    rejected = [path for path in paths if not path_allowed(path, allowed_paths)]
    if rejected:
        raise ValueError(f"changes exceed the frozen task boundary: {rejected}")
    return paths


def _copy_source(source: Path, destination: Path) -> None:
    """Copy the baseline without importing the user's Git identity."""
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
    paths: GitRunPaths,
    source: Path,
    *,
    cli_source: Path,
    storage_source: Path,
    hook_source: Path,
    lanes: tuple[str, ...] = ("lane-1", "lane-2", "lane-3"),
) -> str:
    """Freeze source into main and create isolated lane/integration clones."""
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
    configure_clone(paths.integration, lane="orchestrateor", run_root=paths.root)

    paths.bin.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cli_source, paths.bin / "pfc")
    shutil.copy2(storage_source, paths.bin / "pfc_storage.py")
    (paths.bin / "pfc").chmod(0o755)
    hook = paths.central / "hooks" / "pre-receive"
    shutil.copy2(hook_source, hook)
    hook.chmod(0o755)
    paths.system_reports.mkdir(parents=True, exist_ok=True)
    paths.evaluation_artifacts.mkdir(parents=True, exist_ok=True)
    paths.object_store.mkdir(parents=True, exist_ok=True)
    for lane in lanes:
        (paths.system_reports / f"{lane}.jsonl").touch()
    return main_sha(paths.central)


def validate_shadow_repository(
    paths: GitRunPaths,
    lanes: tuple[str, ...] = ("lane-1", "lane-2", "lane-3"),
) -> None:
    """Reject partial or replaced Git state on resume."""
    directories = (
        paths.central,
        paths.planning,
        paths.integration,
        paths.system_reports,
        paths.evaluation_artifacts,
        paths.object_store,
        *(paths.lane(lane) for lane in lanes),
    )
    files = (
        paths.bin / "pfc",
        paths.bin / "pfc_storage.py",
        paths.database,
        paths.events,
        paths.central / "hooks" / "pre-receive",
        *(paths.system_reports / f"{lane}.jsonl" for lane in lanes),
    )
    for path in directories:
        try:
            info = path.lstat()
        except OSError as why:
            raise RuntimeError(f"resumable Git directory is missing: {path}") from why
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"resumable Git directory was replaced: {path}")
    for path in files:
        try:
            info = path.lstat()
        except OSError as why:
            raise RuntimeError(f"resumable Git file is missing: {path}") from why
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"resumable Git file was replaced: {path}")
    git("fsck", "--no-dangling", cwd=paths.central)


def main_sha(repository: Path) -> str:
    """Resolve the authoritative central main ref."""
    return cast(
        "subprocess.CompletedProcess[str]",
        git("rev-parse", "refs/heads/main", cwd=repository),
    ).stdout.strip()


def commit_message(repository: Path, commit_sha: str) -> str:
    """Read an exact commit message."""
    return cast(
        "subprocess.CompletedProcess[str]",
        git("show", "-s", "--format=%B", commit_sha, cwd=repository),
    ).stdout


def pr_trailer(repository: Path, commit_sha: str) -> str | None:
    """Read the mandatory PFC-PR trailer from a merge commit."""
    message = commit_message(repository, commit_sha)
    matches = re.findall(r"^PFC-PR:\s*(PR\d{6})\s*$", message, re.MULTILINE)
    return matches[-1] if matches else None


def commit_parents(repository: Path, commit_sha: str) -> list[str]:
    """Return commit parents in Git order."""
    line = cast(
        "subprocess.CompletedProcess[str]",
        git("rev-list", "--parents", "-n", "1", commit_sha, cwd=repository),
    ).stdout.strip()
    return line.split()[1:]


def create_fast_path_merge(
    paths: GitRunPaths, *, pr_id: str, prior_sha: str, head_sha: str
) -> str:
    """Publish a deterministic merge whose tree is exactly the evaluated PR head."""
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
    return result.stdout if result.returncode == 0 else None


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
    """Idempotently publish approved changes while rejecting unrelated drift."""
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


def write_branch_protection_context(
    paths: GitRunPaths, store: CoordinationStore
) -> None:
    """Persist observability for the server-side hook and human inspection."""
    store.record_telemetry(
        "branch_protection_installed",
        {
            "repository": str(paths.central),
            "hook": str(paths.central / "hooks/pre-receive"),
        },
    )


__all__ = [
    "PROTECTED_PREFIXES",
    "GitRunPaths",
    "changed_paths",
    "commit_parents",
    "configure_clone",
    "create_fast_path_merge",
    "discover_allowed_paths",
    "discover_evaluator_command",
    "git",
    "initialize_shadow_repository",
    "main_sha",
    "path_allowed",
    "pr_trailer",
    "publish_main",
    "validate_changed_paths",
    "validate_shadow_repository",
    "write_branch_protection_context",
]
