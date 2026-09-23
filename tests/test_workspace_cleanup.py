from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _workspace_cleanup import Cleaned, Config, cleaning, guard, loop, tree
from hmz.coganchor.agents import AgentBase, AgentConfig, Event, SessionBase
from hmz.flows import Budget, Stopped


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A task repository with history, an ignored environment and a work path."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("value = 1\n")
    (root / "README.md").write_text("task\n")
    (root / ".gitignore").write_text(".venv/\n")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "site.py").write_text("# installed\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "task")
    return root


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    path.mkdir()
    return path


def test_listing_honours_gitignore_and_leaves_git_out(repo: Path) -> None:
    (repo / "link").symlink_to("src")

    assert tree.listed(repo) == [".gitignore", "README.md", "link", "src/main.py"]


def test_measure_counts_strays_notes_and_work_path_comments(repo: Path) -> None:
    manifest = set(tree.listed(repo))
    (repo / "src" / "new.py").write_text("value = 2  # design intent\n")
    (repo / "scratch.txt").write_text("discard me\n")
    (repo / ".venv" / "lib" / "more.py").write_text("# ignored, never a stray\n")
    (repo / "NEXT.md").write_text("try another design\n")

    found = tree.measure(repo, manifest, ("src",))

    assert found.strays == ["scratch.txt"]
    assert found.notes_lines == 1
    assert found.comment_count == 1


def test_repair_prompt_names_places_not_paths() -> None:
    strays = [f"build/obj/{index}.o" for index in range(30)]
    strays += [f"tmp/{index}.log" for index in range(6)] + ["scratch.txt"]
    held = Config(work_paths=("src",))
    overs = cleaning.overages(tree.Measure(strays, 12, 0), held)
    prompt = cleaning.repair_prompt(overs)

    assert "37 stray file(s) in build/ (30), tmp/ (6), scratch.txt" in prompt
    assert "NEXT.md has 12 lines, cap 10" in prompt
    assert "obj" not in prompt
    many = cleaning.places([f"d{index}/f" for index in range(12)])
    assert many.endswith("and 4 more places")


def test_revert_point_recovers_an_interrupted_epoch_and_spares_ignored_files(
    repo: Path, store: Path
) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    saved = tree.save_tree(repo, store)
    assert not (saved / ".venv").exists()
    (repo / "src" / "main.py").write_text("partly cleaned\n")
    (repo / "stray" / "deep").mkdir(parents=True)
    (repo / "stray" / "deep" / "x.txt").write_text("partial\n")
    (repo / ".venv" / "lib" / "site.py").write_text("# rebuilt\n")

    assert tree.save_tree(repo, store) == saved

    assert (repo / "src" / "main.py").read_text() == "value = 1\n"
    assert not (repo / "stray").exists()
    assert (repo / ".venv" / "lib" / "site.py").read_text() == "# rebuilt\n"
    assert _git(repo, "rev-parse", "HEAD") == head


class Scripted:
    """A cleaner whose turns are functions of the prompt, run in the repository."""

    def __init__(self, *turns: Callable[[str], Any]) -> None:
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.budget = None

    def new(self, cwd: str) -> Scripted:
        return self

    def spent(self) -> SimpleNamespace:
        return SimpleNamespace(total=len(self.prompts))

    def interject(self, _text: str) -> None:
        pass

    def __call__(self, prompt: str, **_kwargs: Any) -> Any:
        self.prompts.append(prompt)
        return self.turns.pop(0)(prompt)


def _cleaned(*_args: Any) -> Cleaned:
    return Cleaned(
        deleted=["scratch"], kept=["src"], check_ran=False, check_passed=False
    )


def test_an_epoch_replaces_history_and_archives_the_one_it_replaced(
    repo: Path, store: Path, tmp_path: Path
) -> None:
    manifest = set(tree.listed(repo))
    original = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "main.py").write_text("value = 2  # tried 3 variants\n")
    (repo / "scratch.txt").write_text("notes from turn 2\n")

    def clean(_prompt: str) -> Cleaned:
        (repo / "src" / "main.py").write_text("value = 2\n")
        (repo / "scratch.txt").unlink()
        (repo / "NEXT.md").write_text("try a lookup table\n")
        return _cleaned()

    cleaner = Scripted(clean)
    cleaning.clean_epoch(cleaner, Config(work_paths=("src",)), repo, manifest, store, 1)

    assert len(cleaner.prompts) == 1
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "log", "-1", "--format=%s") == "epoch 1: distilled tree"
    assert ".venv/lib/site.py" not in _git(repo, "ls-files")
    assert (repo / ".venv" / "lib" / "site.py").exists()
    assert not (store / "revert").exists()

    at = ("--git-dir", str(tmp_path / "history.git"))
    ref = "refs/runs/store/epoch-001"
    assert _git(repo, *at, "log", "--format=%s", ref) == (
        "epoch 1: the tree before cleaning\ntask"
    )
    assert _git(repo, *at, "rev-parse", f"{ref}~1") == original
    assert _git(repo, *at, "rev-parse", f"{ref}.refs/heads/main") == original
    before = _git(repo, *at, "show", f"{ref}:src/main.py")
    assert before == "value = 2  # tried 3 variants"
    assert _git(repo, *at, "show", f"{ref}:scratch.txt") == "notes from turn 2"
    assert _git(repo, *at, "log", "--format=%s", f"{ref}.distilled") == (
        "epoch 1: distilled tree\nepoch 1: the tree before cleaning\ntask"
    )
    assert _git(repo, *at, "rev-parse", f"{ref}.distilled") == _git(
        repo, "rev-parse", "HEAD"
    )


def test_the_archive_reads_as_one_history_across_epochs_and_runs(
    repo: Path, tmp_path: Path
) -> None:
    manifest = set(tree.listed(repo))
    held = Config(work_paths=("src",))
    first, second = tmp_path / "run-a", tmp_path / "run-b"
    first.mkdir()
    second.mkdir()
    for store, epoch in ((first, 1), (first, 2), (second, 1)):
        (repo / "src" / "main.py").write_text(f"value = {store.name} {epoch}\n")
        cleaning.clean_epoch(Scripted(_cleaned), held, repo, manifest, store, epoch)

    at = ("--git-dir", str(tmp_path / "history.git"))
    assert _git(
        repo, *at, "log", "--format=%s", "refs/runs/run-a/epoch-002.distilled"
    ) == (
        "epoch 2: distilled tree\n"
        "epoch 2: the tree before cleaning\n"
        "epoch 1: distilled tree\n"
        "epoch 1: the tree before cleaning\n"
        "task"
    )
    assert _git(repo, *at, "rev-parse", "--verify", "refs/runs/run-b/epoch-001")
    assert not (tmp_path / "run-a" / "history").exists()


def test_an_interrupted_epoch_puts_the_tree_back(repo: Path, store: Path) -> None:
    manifest = set(tree.listed(repo))
    head = _git(repo, "rev-parse", "HEAD")

    def stopped(_prompt: str) -> Any:
        (repo / "src" / "main.py").unlink()
        (repo / "half.txt").write_text("half-cleaned\n")
        raise Stopped("allowance spent")

    with pytest.raises(Stopped):
        cleaning.clean_epoch(
            Scripted(stopped), Config(work_paths=("src",)), repo, manifest, store, 1
        )

    assert (repo / "src" / "main.py").read_text() == "value = 1\n"
    assert not (repo / "half.txt").exists()
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not (store / "revert").exists()
    assert not tree.history_repo(store).exists()


def test_a_failed_check_reverts_the_cleaning_and_keeps_its_log(
    repo: Path, store: Path
) -> None:
    manifest = set(tree.listed(repo))

    def clean(_prompt: str) -> Cleaned:
        (repo / "src" / "main.py").write_text("broken\n")
        return _cleaned()

    held = Config(work_paths=("src",), check_command="echo checking; exit 3")
    cleaning.clean_epoch(Scripted(clean), held, repo, manifest, store, 1)

    assert (repo / "src" / "main.py").read_text() == "value = 1\n"
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "log", "-1", "--format=%s").startswith(
        "epoch 1: the tree the coding turns left; the check failed"
    )
    assert "checking" in (store / "checks" / "epoch-001.log").read_text()
    assert _git(
        repo,
        "--git-dir",
        str(tree.history_repo(store)),
        "rev-parse",
        "--verify",
        "refs/runs/store/epoch-001",
    )


def test_gitignore_files_hold_for_the_whole_epoch(repo: Path, store: Path) -> None:
    manifest = set(tree.listed(repo))
    (repo / "out").mkdir()
    (repo / "out" / ".gitignore").write_text("*.log\n")
    (repo / "out" / "run.log").write_text("kept out of git\n")

    def clean(_prompt: str) -> Cleaned:
        (repo / ".gitignore").unlink()
        (repo / "out" / ".gitignore").unlink()
        (repo / "src" / ".gitignore").write_text("*.py\n")
        return _cleaned()

    cleaning.clean_epoch(
        Scripted(clean), Config(work_paths=("src",)), repo, manifest, store, 1
    )

    assert (repo / ".gitignore").read_text() == ".venv/\n"
    assert (repo / "out" / ".gitignore").read_text() == "*.log\n"
    assert not (repo / "src" / ".gitignore").exists()
    tracked = _git(repo, "ls-files").splitlines()
    assert "src/main.py" in tracked
    assert not any(path.startswith(".venv") for path in tracked)
    assert "out/run.log" not in tracked
    assert (repo / ".venv" / "lib" / "site.py").read_text() == "# installed\n"
    assert (repo / "out" / "run.log").exists()


def test_a_restore_spares_what_the_saved_gitignore_ignored(
    repo: Path, store: Path
) -> None:
    saved = tree.save_tree(repo, store)
    (repo / ".gitignore").unlink()

    tree.restore_tree(repo, saved)

    assert (repo / ".gitignore").read_text() == ".venv/\n"
    assert (repo / ".venv" / "lib" / "site.py").read_text() == "# installed\n"


def test_a_restore_puts_back_a_file_that_became_a_directory(
    repo: Path, store: Path
) -> None:
    (repo / ".gitignore").write_text(".venv/\nbuild/\n")
    (repo / "build").write_text("script\n")
    saved = tree.save_tree(repo, store)
    (repo / "build").unlink()
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("ignored\n")

    tree.restore_tree(repo, saved)

    assert (repo / "build").read_text() == "script\n"


def test_a_dropped_revert_point_never_reads_as_in_flight(
    repo: Path, store: Path
) -> None:
    saved = tree.save_tree(repo, store)
    (saved / "locked").mkdir()
    (saved / "locked" / "f").write_text("x\n")
    (saved / "locked").chmod(0o555)

    tree.drop_saved(saved)

    assert not os.path.lexists(saved)
    assert not (store / "revert.dropping").exists()
    (store / "revert.dropping" / "left").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("value = 5\n")
    tree.save_tree(repo, store)
    assert (repo / "src" / "main.py").read_text() == "value = 5\n"
    assert not (store / "revert.dropping").exists()


def test_a_tracked_file_counts_whatever_gitignore_says(
    repo: Path, store: Path, tmp_path: Path
) -> None:
    (repo / ".venv" / "lib" / "pinned.py").write_text("pinned = 1\n")
    _git(repo, "add", "-f", ".venv/lib/pinned.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pin")
    manifest = set(tree.listed(repo))
    assert ".venv/lib/pinned.py" in manifest

    cleaning.clean_epoch(
        Scripted(_cleaned), Config(work_paths=("src",)), repo, manifest, store, 1
    )

    tracked = _git(repo, "ls-files").splitlines()
    assert ".venv/lib/pinned.py" in tracked
    assert ".venv/lib/site.py" not in tracked
    at = ("--git-dir", str(tmp_path / "history.git"))
    archived = _git(
        repo, *at, "ls-tree", "-r", "--name-only", "refs/runs/store/epoch-001"
    )
    assert ".venv/lib/pinned.py" in archived.splitlines()


def test_a_git_that_will_not_finish_is_a_failed_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def hangs(*_args: Any, **_kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("git", tree.GIT_SECONDS)

    monkeypatch.setattr(tree.subprocess, "run", hangs)

    done = tree._git("status")

    assert done.returncode != 0


def test_epochs_store_what_they_share_once(repo: Path, store: Path) -> None:
    (repo / "data").mkdir()
    for index in range(50):
        (repo / "data" / f"{index}.bin").write_bytes(os.urandom(8192))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "data")
    manifest = set(tree.listed(repo))
    held = Config(work_paths=("src",))
    for epoch in (1, 2, 3):
        (repo / "src" / "main.py").write_text(f"value = {epoch}\n")
        cleaning.clean_epoch(Scripted(_cleaned), held, repo, manifest, store, epoch)

    counted = _git(
        repo, "--git-dir", str(tree.history_repo(store)), "count-objects", "-v"
    )
    sizes = dict(line.split(": ") for line in counted.splitlines())
    stored = int(sizes["size"]) + int(sizes["size-pack"])
    assert stored < 2 * 50 * 8


def test_a_check_log_keeps_only_its_end(
    repo: Path, store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tree, "CHECK_LOG_BYTES", 100)
    log = store / "checks" / "epoch-001.log"

    assert tree.run_check(repo, "seq 1 1000", log)

    kept = log.read_text()
    assert kept.startswith("[earlier output cut]")
    assert kept.endswith("1000\n")
    assert len(kept) < 130


def test_large_files_and_nested_repositories_stay_out_of_git(
    repo: Path, store: Path, tmp_path: Path
) -> None:
    manifest = set(tree.listed(repo))
    (repo / "src" / "weights.bin").write_bytes(b"\0" * (2 * tree.MIB))
    (repo / "src" / "deps").mkdir()
    _git(repo / "src" / "deps", "init", "-q")
    held = Config(work_paths=("src",), max_tracked_file_mb=1)

    cleaning.clean_epoch(Scripted(_cleaned), held, repo, manifest, store, 1)

    tracked = _git(repo, "ls-files").splitlines()
    assert "src/weights.bin" not in tracked
    assert not any(path.startswith("src/deps") for path in tracked)
    assert (repo / "src" / "weights.bin").exists()
    assert _git(repo, "status", "--porcelain") == ""
    at = ("--git-dir", str(tmp_path / "history.git"))
    ref = "refs/runs/store/epoch-001"
    archived = _git(repo, *at, "ls-tree", "-r", "--name-only", ref).splitlines()
    assert "src/weights.bin" not in archived
    assert "src/main.py" in archived
    assert "src/weights.bin (2.0 MB)" in _git(
        repo, *at, "log", "-1", "--format=%B", ref
    )

    (repo / "src" / "big.dat").write_bytes(b"\1" * (2 * tree.MIB))
    _git(repo, "add", "src/big.dat")
    agent = ("-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qm", "big")
    refused = subprocess.run(
        ["git", *agent], cwd=repo, capture_output=True, text=True, check=False
    )
    assert refused.returncode
    assert "refused: src/big.dat" in refused.stderr
    _git(repo, "reset", "-q")
    (repo / "src" / "small.py").write_text("value = 3\n")
    _git(repo, "add", "src/small.py")
    _git(repo, *agent)
    assert "src/small.py" in _git(repo, "ls-files")


def test_an_unreadable_history_is_kept_whole(
    repo: Path, store: Path, tmp_path: Path
) -> None:
    manifest = set(tree.listed(repo))
    shutil.rmtree(repo / ".git")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("not a repository\n")

    cleaning.clean_epoch(
        Scripted(_cleaned), Config(work_paths=("src",)), repo, manifest, store, 1
    )

    assert (tmp_path / "unreadable-store-epoch-001.git" / "HEAD").exists()
    at = ("--git-dir", str(tmp_path / "history.git"))
    assert _git(repo, *at, "rev-list", "--count", "refs/runs/store/epoch-001") == "1"


def test_measured_overages_go_back_as_repairs_then_the_flow_cuts(
    repo: Path, store: Path
) -> None:
    manifest = set(tree.listed(repo))

    def leave_junk(_prompt: str) -> Cleaned:
        (repo / "junk").mkdir()
        for index in range(3):
            (repo / "junk" / f"{index}.log").write_text("x\n")
        return _cleaned()

    cleaner = Scripted(leave_junk, lambda _prompt: "tried", lambda _prompt: "tried")
    cleaning.clean_epoch(cleaner, Config(work_paths=("src",)), repo, manifest, store, 1)

    assert len(cleaner.prompts) == 3
    assert "3 stray file(s) in junk/ (3)" in cleaner.prompts[1]
    assert not list((repo / "junk").iterdir())


def test_a_cleaner_the_clock_ended_gets_no_repairs(repo: Path, store: Path) -> None:
    manifest = set(tree.listed(repo))

    def slow(_prompt: str) -> None:
        (repo / "junk.txt").write_text("x\n")
        time.sleep(0.15)

    cleaner = Scripted(slow)
    held = Config(
        work_paths=("src",),
        session_timeout_minutes=0.001,
        idle_timeout_minutes=0,
        stop_grace_minutes=0,
    )
    cleaning.clean_epoch(cleaner, held, repo, manifest, store, 1)

    assert len(cleaner.prompts) == 1
    assert not (repo / "junk.txt").exists()


class Quiet:
    """A session that spends nothing until told to, recording what it is told."""

    def __init__(self, *, moves_when_told: bool = False) -> None:
        self.budget: Budget | None = None
        self.said: list[str] = []
        self.tokens = 0
        self.moves_when_told = moves_when_told

    def spent(self) -> SimpleNamespace:
        return SimpleNamespace(total=self.tokens)

    def interject(self, text: str) -> None:
        self.said.append(text)
        if self.moves_when_told:
            self.tokens += 1


def _held(session: Any, *, wall: float, idle: float, grace: float) -> Any:
    return guard.guarded(
        session,
        session_timeout_minutes=wall,
        idle_timeout_minutes=idle,
        stop_grace_minutes=grace,
        label="test",
    )


def test_the_clock_asks_for_a_wrap_up_and_sets_the_cut_off() -> None:
    session = Quiet()
    with _held(session, wall=0.001, idle=0, grace=0.002) as watch:
        time.sleep(0.15)

    assert watch.timed_out
    assert session.budget is not None
    assert session.budget.seconds == pytest.approx(0.18)
    assert (session.budget.when, session.budget.then) == ("immediately", "end")
    assert len(session.said) == 1
    assert "within 0.002 minutes" in session.said[0]


def test_a_tighter_clock_is_kept_and_a_timeout_always_lands() -> None:
    session = Quiet()
    session.budget = Budget(output=100, seconds=5, then="fail")
    with _held(session, wall=1, idle=0, grace=0):
        pass
    assert session.budget == Budget(
        output=100, seconds=5, when="immediately", then="end"
    )

    session.budget = Budget(output=100)
    with _held(session, wall=1, idle=0, grace=1):
        pass
    assert session.budget == Budget(
        output=100, seconds=120, when="immediately", then="end"
    )


def test_idle_reminders_rearm_after_progress_and_never_end_a_turn() -> None:
    session = Quiet(moves_when_told=True)
    with _held(session, wall=0, idle=0.001, grace=0) as watch:
        time.sleep(0.2)

    assert not watch.timed_out
    assert session.budget is None
    assert len(session.said) >= 2
    assert all("carry on" in said for said in session.said)


def test_one_idle_reminder_per_idle_stretch() -> None:
    session = Quiet()
    with _held(session, wall=0, idle=0.001, grace=0):
        time.sleep(0.2)

    assert len(session.said) == 1


def test_nothing_is_watched_when_both_limits_are_off() -> None:
    session = Quiet()
    before = threading.active_count()
    with _held(session, wall=0, idle=0, grace=0) as watch:
        assert threading.active_count() == before
    assert session.budget is None
    assert not watch.timed_out


class Slow(SessionBase):
    """A humanize session whose turn runs until humanize cuts it off."""

    def _stream(self, prompt: str, *, schema: Any = None) -> Iterator[Event]:
        for step in range(200):
            # Where a real backend's process would be ended by the cut-off.
            if self._cutting():
                yield Event(kind="result", text=f"cut at step {step}")
                return
            time.sleep(0.01)
            yield Event(kind="text", text=".")
        yield Event(kind="result", text="finished")


class SlowAgent(AgentBase):
    def new(self, cwd: Any = None) -> Slow:
        return Slow(self)


def test_humanize_cuts_a_long_turn_off_and_the_turn_counts(tmp_path: Path) -> None:
    held = Config(
        work_paths=("src",),
        session_timeout_minutes=0.002,
        idle_timeout_minutes=0,
        stop_grace_minutes=0.002,
    )
    began = time.monotonic()

    landed = loop.coding_turn(
        SlowAgent(AgentConfig(model="m", effort="high")), "task", tmp_path, held, "t"
    )

    assert landed
    assert time.monotonic() - began < 1.5
