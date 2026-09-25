from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "humanize1"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Humanize Test",
    "GIT_AUTHOR_EMAIL": "humanize-test@example.invalid",
    "GIT_COMMITTER_NAME": "Humanize Test",
    "GIT_COMMITTER_EMAIL": "humanize-test@example.invalid",
}


def git(at: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=at,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def repository(at: Path, *, branch: str = "main") -> Path:
    at.mkdir(parents=True, exist_ok=True)
    git(at, "init", f"--initial-branch={branch}")
    (at / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    git(at, "add", "fixture.txt")
    git(at, "commit", "-m", "fixture")
    return at
