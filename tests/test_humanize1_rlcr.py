"""Focused contracts for composing the official Humanize RLCR flow."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
FLOW = ROOT / "flows" / "humanize1"
sys.path[:0] = [str(FLOW), str(FLOW.parent)]

import humanize1  # noqa: E402


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def test_skip_code_review_overrides_automatic_main_detection(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--initial-branch=main")
    _git(tmp_path, "config", "user.name", "Humanize Test")
    _git(tmp_path, "config", "user.email", "humanize-test@example.invalid")
    (tmp_path / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    _git(tmp_path, "add", "fixture.txt")
    _git(tmp_path, "commit", "-m", "fixture")

    assert humanize1._base(tmp_path, "") == "main"
    assert (
        humanize1._review_base(
            tmp_path,
            humanize1.Rlcr(base_branch="", skip_code_review=True),
        )
        == ""
    )
    assert (
        humanize1._review_base(
            tmp_path,
            humanize1.Rlcr(base_branch="", skip_code_review=False),
        )
        == "main"
    )
