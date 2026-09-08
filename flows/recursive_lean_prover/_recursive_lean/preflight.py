"""Single-problem acquisition and local reference-library preparation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import fcntl

from .store import atomic_text, now

if TYPE_CHECKING:
    from collections.abc import Mapping


PROBLEM_COLLECTION_URL = "https://lean-lang.org/eval/problems/"
PROBLEM_DATA_URL = "https://lean-lang.org/eval/site-data/v2/problems/{problem_id}.json"
PROBLEM_PAGE_URL = "https://lean-lang.org/eval/problems/{problem_id}/"
PROBLEM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class ReferenceSource:
    """One required Git-backed source used by every reasoning stage."""

    name: str
    url: str
    directory: str
    private: bool = False
    sentinels: tuple[str, ...] = ("README.md",)


REFERENCE_SOURCES = (
    ReferenceSource(
        name="TauCeti",
        url="https://github.com/TauCetiProject/TauCeti.git",
        directory="TauCeti",
        sentinels=("README.md", "TauCeti.lean"),
    ),
    ReferenceSource(
        name="lean-pool",
        url="https://github.com/Vilin97/lean-pool.git",
        directory="lean-pool",
        sentinels=("README.md", "LeanPool.lean"),
    ),
    ReferenceSource(
        name="mathlib-internal",
        url="https://huggingface.co/datasets/humanfia-lab/mathlib-internal",
        directory="mathlib-internal",
        private=True,
        sentinels=(
            "README.md",
            "scripts/search_wiki.py",
            "wiki/indexes/problems.md",
        ),
    ),
)


@dataclass(frozen=True)
class ReferenceBundle:
    """Pinned paths and commits for one prepared reference library."""

    root: Path
    manifest: Path
    paths: Mapping[str, Path]
    commits: Mapping[str, str]

    def prompt_context(self) -> str:
        """Render the mandatory, stage-independent retrieval contract."""
        lines = [
            "Mandatory local reference library (downloaded before problem acquisition):",
            "Before answering this stage, search every source below. Cite the exact local files",
            "or search commands that informed the answer. If a source has no relevant hit, say",
            "which query you tried; do not silently omit any of the three sources.",
            "For structured output, fill `reference_use` with exactly one entry per source;",
            "for a Markdown plan or RLCR summary, include that evidence under `Reference use`.",
        ]
        for source in REFERENCE_SOURCES:
            path = self.paths[source.name]
            commit = self.commits[source.name]
            if source.name == "mathlib-internal":
                hint = (
                    f'run `python3 {path / "scripts/search_wiki.py"} '
                    '"<mathematical or Lean terms>" --limit 20` and inspect reported files'
                )
            else:
                hint = f"search `{path}` with `rg` and inspect relevant Markdown/Lean files"
            lines.append(
                f"- {source.name}: `{path}` at `{commit}`; {hint}."
            )
        lines.extend(
            [
                f"- Snapshot manifest: `{self.manifest}`.",
                "These sources are evidence and reusable examples, not authority to weaken the",
                "selected theorem or copy a proof without checking compatibility and provenance.",
            ]
        )
        return "\n".join(lines)


class ReferenceLibrary:
    """Clone each mandatory source once and reuse its exact snapshot on resume."""

    def __init__(
        self,
        root: Path,
        *,
        huggingface_token_env: str,
        askpass_script: Path,
    ) -> None:
        self.root = root.resolve()
        self.huggingface_token_env = huggingface_token_env
        self.askpass_script = askpass_script.resolve()

    def prepare(self) -> ReferenceBundle:
        """Make all three repositories locally readable or fail before planning."""
        self.root.mkdir(parents=True, exist_ok=True)
        with self._prepare_lock():
            return self._prepare_locked()

    def _prepare_locked(self) -> ReferenceBundle:
        """Prepare or verify the immutable cache while holding its process lock."""
        recorded = self._recorded_commits()
        paths: dict[str, Path] = {}
        commits: dict[str, str] = {}
        for source in REFERENCE_SOURCES:
            destination = self.root / source.directory
            if destination.exists():
                self._validate_checkout(
                    source,
                    destination,
                    expected_commit=recorded.get(source.name) if recorded else None,
                    require_read_only=bool(recorded),
                )
            else:
                if recorded:
                    raise RuntimeError(
                        f"pinned reference {source.name} is missing from {destination}"
                    )
                self._clone(source, destination)
                self._validate_checkout(source, destination)
            paths[source.name] = destination.resolve()
            commits[source.name] = self._commit(destination)
        if recorded and commits != recorded:
            raise RuntimeError("reference HEADs do not match the original manifest")
        if not recorded:
            for path in paths.values():
                self._make_read_only(path)
            for source in REFERENCE_SOURCES:
                self._validate_checkout(
                    source,
                    paths[source.name],
                    expected_commit=commits[source.name],
                    require_read_only=True,
                )
        manifest = self.root / "manifest.json"
        if not recorded:
            atomic_text(
                manifest,
                json.dumps(
                    {
                        "prepared_at": now(),
                        "sources": [
                            {
                                "name": source.name,
                                "url": source.url,
                                "path": str(paths[source.name]),
                                "commit": commits[source.name],
                            }
                            for source in REFERENCE_SOURCES
                        ],
                    },
                    indent=2,
                )
                + "\n",
            )
        return ReferenceBundle(
            root=self.root,
            manifest=manifest.resolve(),
            paths=paths,
            commits=commits,
        )

    @contextmanager
    def _prepare_lock(self):
        """Serialize clone and first-manifest publication across supervisors."""
        lock_path = self.root / ".prepare.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _recorded_commits(self) -> dict[str, str]:
        """Read the first manifest strictly; it is the cache's immutable pin set."""
        manifest = self.root / "manifest.json"
        if not manifest.is_file():
            return {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            entries = data["sources"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"invalid reference manifest: {manifest}") from error
        if not isinstance(entries, list) or len(entries) != len(REFERENCE_SOURCES):
            raise RuntimeError(f"invalid reference manifest source set: {manifest}")
        recorded: dict[str, str] = {}
        expected = {source.name: source for source in REFERENCE_SOURCES}
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("name") not in expected:
                raise RuntimeError(f"invalid reference manifest entry: {manifest}")
            source = expected[entry["name"]]
            commit = entry.get("commit")
            expected_path = str((self.root / source.directory).resolve())
            if (
                entry.get("url") != source.url
                or entry.get("path") != expected_path
                or not isinstance(commit, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit)
                or source.name in recorded
            ):
                raise RuntimeError(f"reference manifest does not match {source.name}")
            recorded[source.name] = commit
        if set(recorded) != set(expected):
            raise RuntimeError(f"invalid reference manifest source set: {manifest}")
        return recorded

    def _clone(self, source: ReferenceSource, destination: Path) -> None:
        """Clone atomically so an interrupted download is never treated as ready."""
        environment = os.environ.copy()
        token = environment.pop(self.huggingface_token_env, "").strip()
        environment.pop("HUMANIZE_HF_TOKEN", None)
        if source.private:
            if not token:
                raise RuntimeError(
                    f"{self.huggingface_token_env} is required to download "
                    "humanfia-lab/mathlib-internal; set it in the hmz environment"
                )
            if not self.askpass_script.is_file() or not os.access(
                self.askpass_script, os.X_OK
            ):
                raise RuntimeError(
                    f"Hugging Face Git askpass helper is not executable: {self.askpass_script}"
                )
            environment.update(
                GIT_ASKPASS=str(self.askpass_script),
                GIT_TERMINAL_PROMPT="0",
                HUMANIZE_HF_TOKEN=token,
            )
        temporary_root = Path(
            tempfile.mkdtemp(prefix=f".{source.directory}-", dir=self.root)
        )
        temporary_checkout = temporary_root / "checkout"
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-c",
                    "credential.helper=",
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--no-tags",
                    source.url,
                    str(temporary_checkout),
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                details = self._redact(
                    (completed.stderr or completed.stdout).strip(),
                    environment.get("HUMANIZE_HF_TOKEN", ""),
                )
                raise RuntimeError(
                    f"could not download required reference {source.name}: "
                    f"{details or 'git clone failed'}"
                )
            self._validate_checkout(source, temporary_checkout)
            temporary_checkout.replace(destination)
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    @staticmethod
    def _validate_checkout(
        source: ReferenceSource,
        destination: Path,
        *,
        expected_commit: str | None = None,
        require_read_only: bool = False,
    ) -> None:
        if not (destination / ".git").is_dir():
            raise RuntimeError(
                f"reference path exists but is not a Git checkout: {destination}"
            )
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=destination,
            capture_output=True,
            text=True,
            check=False,
        )
        if remote.returncode != 0 or remote.stdout.strip() != source.url:
            raise RuntimeError(
                f"reference {source.name} does not have the required origin {source.url}"
            )
        missing = [one for one in source.sentinels if not (destination / one).is_file()]
        if missing:
            raise RuntimeError(
                f"reference {source.name} is incomplete at {destination}; missing: "
                + ", ".join(missing)
            )
        if expected_commit is not None and require_read_only:
            actual = ReferenceLibrary._commit(destination)
            if actual != expected_commit:
                raise RuntimeError(
                    f"reference {source.name} HEAD differs from its pinned manifest"
                )
            boundaries = [
                destination,
                destination / ".git",
                *(destination / one for one in source.sentinels),
            ]
            writable = next(
                (
                    path
                    for path in boundaries
                    if not path.is_symlink() and path.stat().st_mode & 0o222
                ),
                None,
            )
            if writable is not None:
                raise RuntimeError(
                    f"reference {source.name} is not read-only: {writable}"
                )
            return
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=destination,
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise RuntimeError(f"reference {source.name} is not a clean snapshot")
        if require_read_only:
            writable = ReferenceLibrary._writable_path(destination)
            if writable is not None:
                raise RuntimeError(
                    f"reference {source.name} is not read-only: {writable}"
                )

    @staticmethod
    def _make_read_only(destination: Path) -> None:
        """Remove write bits without following repository symlinks."""
        paths = [destination]
        paths.extend(destination.rglob("*"))
        for path in reversed(paths):
            if path.is_symlink():
                continue
            path.chmod(path.stat().st_mode & ~0o222)

    @staticmethod
    def _writable_path(destination: Path) -> Path | None:
        for path in [destination, *destination.rglob("*")]:
            if not path.is_symlink() and path.stat().st_mode & 0o222:
                return path
        return None

    @staticmethod
    def _commit(destination: Path) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=destination,
            capture_output=True,
            text=True,
            check=False,
        )
        commit = completed.stdout.strip()
        if completed.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
            raise RuntimeError(f"could not resolve reference commit at {destination}")
        return commit

    @staticmethod
    def _redact(message: str, secret: str) -> str:
        if secret:
            message = message.replace(secret, "<redacted>")
        return re.sub(r"(https://)[^/@\s]+@", r"\1<redacted>@", message)


def infer_problem_id(project: Path, configured: str, task: str) -> str:
    """Resolve one problem id without letting the fetch session choose a collection item."""
    candidates: list[str] = []
    if configured.strip():
        candidates.append(configured.strip())
    candidates.extend(
        match.group(1)
        for match in re.finditer(
            r"https://lean-lang\.org/eval/problems/([A-Za-z0-9][A-Za-z0-9_-]*)/?",
            task,
        )
    )
    readme = project / "README.md"
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError:
        text = ""
    match = re.search(r"Problem ID:\s*`([^`]+)`", text, flags=re.IGNORECASE)
    if match:
        candidates.append(match.group(1).strip())
    invalid = [candidate for candidate in candidates if not PROBLEM_ID.fullmatch(candidate)]
    if invalid:
        raise RuntimeError(f"invalid Lean-Eval problem id: {invalid[0]!r}")
    selected = set(candidates)
    if len(selected) > 1:
        raise RuntimeError(
            "conflicting Lean-Eval problem ids before acquisition: "
            + ", ".join(sorted(selected))
        )
    if selected:
        return next(iter(selected))
    candidates.append(project.name)
    for candidate in candidates:
        if PROBLEM_ID.fullmatch(candidate) and candidate != "math-lean-flow":
            return candidate
    raise RuntimeError(
        "could not determine exactly one Lean-Eval problem; set problem_id in the flow config"
    )


def problem_context(problem_path: Path, problem_id: str) -> str:
    """Point every downstream stage at the one immutable fetched record."""
    return (
        "Official single-problem record:\n"
        f"- Problem id: `{problem_id}`\n"
        f"- Markdown artifact: `{problem_path.resolve()}`\n"
        f"- Controller-frozen v2 JSON: `{(problem_path.parent / 'problem-site-data.json').resolve()}`\n"
        f"- Source page: {PROBLEM_PAGE_URL.format(problem_id=problem_id)}\n"
        "Read the Markdown and its authoritative JSON before reasoning. They describe the only "
        "leaderboard problem selected for this run; do not fetch, blend in, or solve a second "
        "leaderboard problem."
    )
