from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from hmz.flows import (
    AgentCollection,
    Budget,
    CostExceeded,
    DurationExceeded,
    EnvCollection,
    EnvCommandTimeout,
    EnvError,
    FlowContext,
    FlowParams,
    Usage,
    flow,
)
from hmz.runtime.flowing.fakes import FakeAgentDriver, run_fake

from tests.cleanup_env import LocalDir, local

FLOW = Path(__file__).parents[1] / "flows" / "flame_chase_agent_cleanup"
sys.path[:0] = [str(FLOW), str(FLOW.parent)]

from _flame_chase_agent_cleanup import (  # noqa: E402
    Cleaned,
    Config,
    Worker,
    Workspace,
    cleaning,
    guard,
    loop,
    tree,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
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
def env(repo: Path) -> Any:
    return local(repo)


@pytest.fixture
def store(tmp_path: Path) -> PurePosixPath:
    path = tmp_path / "store"
    path.mkdir()
    return PurePosixPath(path)


@pytest.fixture(autouse=True)
def no_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "PAUSE", 0.0)


@pytest.mark.asyncio
async def test_listing_honours_gitignore_and_leaves_git_out(
    repo: Path, env: Any
) -> None:
    (repo / "link").symlink_to("src")

    listed = await tree.listed(env)

    assert list(listed) == [".gitignore", "README.md", "link", "src/main.py"]
    assert listed["link"] == "l"
    assert listed["src/main.py"] == "f"


@pytest.mark.asyncio
async def test_measure_counts_strays_notes_and_work_path_comments(
    repo: Path, env: Any
) -> None:
    manifest = set(await tree.listed(env))
    (repo / "src" / "new.py").write_text("value = 2  # design intent\n")
    (repo / "scratch.txt").write_text("discard me\n")
    (repo / ".venv" / "lib" / "more.py").write_text("# ignored, never a stray\n")
    (repo / "NEXT.md").write_text("try another design\n")

    found = await tree.measure(env, manifest, ("src",))

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


@pytest.mark.asyncio
async def test_revert_point_recovers_an_interrupted_epoch_and_spares_ignored_files(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    head = _git(repo, "rev-parse", "HEAD")
    saved = await tree.save_tree(env, store)
    assert not Path(saved, ".venv").exists()
    (repo / "src" / "main.py").write_text("partly cleaned\n")
    (repo / "stray" / "deep").mkdir(parents=True)
    (repo / "stray" / "deep" / "x.txt").write_text("partial\n")
    (repo / ".venv" / "lib" / "site.py").write_text("# rebuilt\n")

    assert await tree.save_tree(env, store) == saved

    assert (repo / "src" / "main.py").read_text() == "value = 1\n"
    assert not (repo / "stray").exists()
    assert (repo / ".venv" / "lib" / "site.py").read_text() == "# rebuilt\n"
    assert _git(repo, "rev-parse", "HEAD") == head


class Session:
    def __init__(self, agent: Scripted) -> None:
        self.agent = agent

    @property
    def usage(self) -> Usage:
        return Usage(output_tokens=self.agent.tokens)


class Scripted:
    def __init__(self, *turns: Callable[[str], Any]) -> None:
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.budgets: list[Budget | None] = []
        self.steered: list[str] = []
        self.tokens = 0
        self.moves_when_told = False

    async def spawn(self, *, env: Any) -> Session:
        return Session(self)

    async def steer(self, text: str, *, session: Any, queued: bool = True) -> None:
        self.steered.append(text)
        if self.moves_when_told:
            self.tokens += 1

    async def run(
        self,
        prompt: str,
        *,
        session: Any,
        output_schema: Any = None,
        budget: Budget | None = None,
    ) -> Any:
        self.prompts.append(prompt)
        self.budgets.append(budget)
        said = self.turns.pop(0)(prompt)
        if inspect.isawaitable(said):
            said = await said
        return said


def _cleaned(*_args: Any) -> Cleaned:
    return Cleaned(
        deleted=["scratch"], kept=["src"], check_ran=False, check_passed=False
    )


@pytest.mark.asyncio
async def test_an_epoch_replaces_history_and_archives_the_one_it_replaced(
    repo: Path, env: Any, store: PurePosixPath, tmp_path: Path
) -> None:
    manifest = set(await tree.listed(env))
    original = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "main.py").write_text("value = 2  # tried 3 variants\n")
    (repo / "scratch.txt").write_text("notes from turn 2\n")

    def clean(_prompt: str) -> Cleaned:
        (repo / "src" / "main.py").write_text("value = 2\n")
        (repo / "scratch.txt").unlink()
        (repo / "NEXT.md").write_text("try a lookup table\n")
        return _cleaned()

    cleaner = Scripted(clean)
    await cleaning.clean_epoch(
        cleaner, Config(work_paths=("src",)), env, manifest, store, 1
    )

    assert len(cleaner.prompts) == 1
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "log", "-1", "--format=%s") == "epoch 1: distilled tree"
    assert ".venv/lib/site.py" not in _git(repo, "ls-files")
    assert (repo / ".venv" / "lib" / "site.py").exists()
    assert not Path(store, "revert").exists()

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


@pytest.mark.asyncio
async def test_the_archive_reads_as_one_history_across_epochs_and_runs(
    repo: Path, env: Any, tmp_path: Path
) -> None:
    manifest = set(await tree.listed(env))
    held = Config(work_paths=("src",))
    first, second = tmp_path / "run-a", tmp_path / "run-b"
    first.mkdir()
    second.mkdir()
    for store, epoch in ((first, 1), (first, 2), (second, 1)):
        (repo / "src" / "main.py").write_text(f"value = {store.name} {epoch}\n")
        await cleaning.clean_epoch(
            Scripted(_cleaned), held, env, manifest, PurePosixPath(store), epoch
        )

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


@pytest.mark.asyncio
async def test_an_interrupted_epoch_puts_the_tree_back(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    manifest = set(await tree.listed(env))
    head = _git(repo, "rev-parse", "HEAD")

    def stopped(_prompt: str) -> Any:
        (repo / "src" / "main.py").unlink()
        (repo / "half.txt").write_text("half-cleaned\n")
        raise CostExceeded("allowance spent")

    with pytest.raises(CostExceeded):
        await cleaning.clean_epoch(
            Scripted(stopped), Config(work_paths=("src",)), env, manifest, store, 1
        )

    assert (repo / "src" / "main.py").read_text() == "value = 1\n"
    assert not (repo / "half.txt").exists()
    assert _git(repo, "rev-parse", "HEAD") == head
    assert not Path(store, "revert").exists()
    assert not Path(tree.history_repo(store)).exists()


@pytest.mark.asyncio
async def test_a_cancelled_epoch_puts_the_tree_back(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    manifest = set(await tree.listed(env))
    began = asyncio.Event()

    async def hangs(_prompt: str) -> Any:
        (repo / "src" / "main.py").write_text("half-cleaned\n")
        began.set()
        await asyncio.sleep(60)

    epoch = asyncio.ensure_future(
        cleaning.clean_epoch(
            Scripted(hangs), Config(work_paths=("src",)), env, manifest, store, 1
        )
    )
    await began.wait()
    epoch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await epoch

    assert (repo / "src" / "main.py").read_text() == "value = 1\n"
    assert not Path(store, "revert").exists()


@pytest.mark.asyncio
async def test_a_failed_check_reverts_the_cleaning_and_keeps_its_log(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    manifest = set(await tree.listed(env))

    def clean(_prompt: str) -> Cleaned:
        (repo / "src" / "main.py").write_text("broken\n")
        return _cleaned()

    held = Config(work_paths=("src",), check_command="echo checking; exit 3")
    await cleaning.clean_epoch(Scripted(clean), held, env, manifest, store, 1)

    assert (repo / "src" / "main.py").read_text() == "value = 1\n"
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo, "log", "-1", "--format=%s").startswith(
        "epoch 1: the tree the coding turns left; the check failed"
    )
    assert "checking" in Path(store, "checks", "epoch-001.log").read_text()
    assert _git(
        repo,
        "--git-dir",
        str(tree.history_repo(store)),
        "rev-parse",
        "--verify",
        "refs/runs/store/epoch-001",
    )


@pytest.mark.asyncio
async def test_gitignore_files_hold_for_the_whole_epoch(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    manifest = set(await tree.listed(env))
    (repo / "out").mkdir()
    (repo / "out" / ".gitignore").write_text("*.log\n")
    (repo / "out" / "run.log").write_text("kept out of git\n")

    def clean(_prompt: str) -> Cleaned:
        (repo / ".gitignore").unlink()
        (repo / "out" / ".gitignore").unlink()
        (repo / "src" / ".gitignore").write_text("*.py\n")
        return _cleaned()

    await cleaning.clean_epoch(
        Scripted(clean), Config(work_paths=("src",)), env, manifest, store, 1
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


@pytest.mark.asyncio
async def test_a_restore_spares_what_the_saved_gitignore_ignored(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    saved = await tree.save_tree(env, store)
    (repo / ".gitignore").unlink()

    await tree.restore_tree(env, saved)

    assert (repo / ".gitignore").read_text() == ".venv/\n"
    assert (repo / ".venv" / "lib" / "site.py").read_text() == "# installed\n"


@pytest.mark.asyncio
async def test_a_restore_puts_back_a_file_that_became_a_directory(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    (repo / ".gitignore").write_text(".venv/\nbuild/\n")
    (repo / "build").write_text("script\n")
    saved = await tree.save_tree(env, store)
    (repo / "build").unlink()
    (repo / "build").mkdir()
    (repo / "build" / "out.o").write_text("ignored\n")

    await tree.restore_tree(env, saved)

    assert (repo / "build").read_text() == "script\n"


@pytest.mark.asyncio
async def test_a_dropped_revert_point_never_reads_as_in_flight(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    saved = await tree.save_tree(env, store)
    (Path(saved) / "locked").mkdir()
    (Path(saved) / "locked" / "f").write_text("x\n")
    (Path(saved) / "locked").chmod(0o555)

    await tree.drop_saved(env, saved)

    assert not os.path.lexists(saved)
    assert not Path(store, "revert.dropping").exists()
    Path(store, "revert.dropping", "left").mkdir(parents=True)
    (repo / "src" / "main.py").write_text("value = 5\n")
    await tree.save_tree(env, store)
    assert (repo / "src" / "main.py").read_text() == "value = 5\n"
    assert not Path(store, "revert.dropping").exists()


@pytest.mark.asyncio
async def test_a_tracked_file_counts_whatever_gitignore_says(
    repo: Path, env: Any, store: PurePosixPath, tmp_path: Path
) -> None:
    (repo / ".venv" / "lib" / "pinned.py").write_text("pinned = 1\n")
    _git(repo, "add", "-f", ".venv/lib/pinned.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "pin")
    manifest = set(await tree.listed(env))
    assert ".venv/lib/pinned.py" in manifest

    await cleaning.clean_epoch(
        Scripted(_cleaned), Config(work_paths=("src",)), env, manifest, store, 1
    )

    tracked = _git(repo, "ls-files").splitlines()
    assert ".venv/lib/pinned.py" in tracked
    assert ".venv/lib/site.py" not in tracked
    at = ("--git-dir", str(tmp_path / "history.git"))
    archived = _git(
        repo, *at, "ls-tree", "-r", "--name-only", "refs/runs/store/epoch-001"
    )
    assert ".venv/lib/pinned.py" in archived.splitlines()


class Hangs:
    async def exec(self, argv: Any, *, timeout: float) -> tuple[int, str, str]:
        raise EnvCommandTimeout(f"{argv!r} ran past {timeout}s")


@pytest.mark.asyncio
async def test_a_git_that_will_not_finish_is_a_failed_step() -> None:
    done, _, err = await tree.git(Hangs(), "status")

    assert done != 0
    assert "ran past" in err


@pytest.mark.asyncio
async def test_epochs_store_what_they_share_once(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    (repo / "data").mkdir()
    for index in range(50):
        (repo / "data" / f"{index}.bin").write_bytes(os.urandom(8192))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "data")
    manifest = set(await tree.listed(env))
    held = Config(work_paths=("src",))
    for epoch in (1, 2, 3):
        (repo / "src" / "main.py").write_text(f"value = {epoch}\n")
        await cleaning.clean_epoch(
            Scripted(_cleaned), held, env, manifest, store, epoch
        )

    counted = _git(
        repo, "--git-dir", str(tree.history_repo(store)), "count-objects", "-v"
    )
    sizes = dict(line.split(": ") for line in counted.splitlines())
    stored = int(sizes["size"]) + int(sizes["size-pack"])
    assert stored < 2 * 50 * 8


@pytest.mark.asyncio
async def test_a_check_log_keeps_only_its_end(
    env: Any, store: PurePosixPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tree, "CHECK_LOG_BYTES", 100)
    log = store / "checks" / "epoch-001.log"

    assert await tree.run_check(env, "seq 1 1000", log)

    kept = Path(log).read_text()
    assert kept.startswith("[earlier output cut]")
    assert kept.endswith("1000\n")
    assert len(kept) < 130


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="reads /proc")
@pytest.mark.asyncio
async def test_what_a_check_leaves_running_is_killed(
    env: Any, repo: Path, store: PurePosixPath
) -> None:
    log = store / "checks" / "epoch-001.log"
    started = "sh -c 'trap \"\" TERM; exec sleep 300' & echo $! > left.pid"

    assert await tree.run_check(env, started, log)

    pid = int((repo / "left.pid").read_text())
    for _ in range(100):
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists() or stat.read_text().split(") ")[1].startswith("Z"):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail(f"process {pid} outlived the check")


@pytest.mark.asyncio
async def test_a_tree_that_cannot_be_listed_or_sized_is_not_guessed_at(
    env: Any, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = await tree.listed(env)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "missing"))

    with pytest.raises(RuntimeError, match="could not measure"):
        await tree.footprint(env)
    with pytest.raises(RuntimeError):
        await tree.left_out(env, entries, tree.MIB)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads what is unreadable")
@pytest.mark.asyncio
async def test_a_history_neither_readable_nor_copyable_is_not_replaced(
    repo: Path, env: Any, store: PurePosixPath, tmp_path: Path
) -> None:
    saved = Path(store, "revert")
    shutil.copytree(repo, saved, symlinks=True)
    shutil.rmtree(saved / ".git")
    (saved / ".git").mkdir()
    (saved / ".git" / "HEAD").write_text("not a repository\n")
    (saved / ".git" / "secret").write_text("x\n")
    (saved / ".git" / "secret").chmod(0)

    try:
        archived = await tree.archive_history(
            env, PurePosixPath(saved), store, 1, tree.MIB
        )
    finally:
        (saved / ".git" / "secret").chmod(0o644)

    assert archived is None


class Unstartable(LocalDir):
    async def exec(self, argv: Any, *, timeout: float) -> tuple[int, str, str]:
        if isinstance(argv, str) and "sh -c" in argv:
            raise EnvError("the check's shell could not be started")
        return await super().exec(argv, timeout=timeout)


@pytest.mark.asyncio
async def test_a_check_that_cannot_start_fails_and_says_so(
    repo: Path, store: PurePosixPath
) -> None:
    log = store / "checks" / "epoch-001.log"

    assert not await tree.run_check(Unstartable(repo), "true", log)

    assert "the check could not start" in Path(log).read_text()


@pytest.mark.asyncio
async def test_a_check_that_runs_too_long_fails_and_says_so(
    env: Any, store: PurePosixPath, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tree, "CHECK_SECONDS", 0.5)
    log = store / "checks" / "epoch-001.log"

    assert not await tree.run_check(env, "echo started; sleep 30", log)

    assert "the check ran past 0.5 seconds" in Path(log).read_text()


@pytest.mark.asyncio
async def test_large_files_and_nested_repositories_stay_out_of_git(
    repo: Path, env: Any, store: PurePosixPath, tmp_path: Path
) -> None:
    manifest = set(await tree.listed(env))
    (repo / "src" / "weights.bin").write_bytes(b"\0" * (2 * tree.MIB))
    (repo / "src" / "deps").mkdir()
    _git(repo / "src" / "deps", "init", "-q")
    held = Config(work_paths=("src",), max_tracked_file_mb=1)

    await cleaning.clean_epoch(Scripted(_cleaned), held, env, manifest, store, 1)

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


@pytest.mark.asyncio
async def test_an_unreadable_history_is_kept_whole(
    repo: Path, env: Any, store: PurePosixPath, tmp_path: Path
) -> None:
    manifest = set(await tree.listed(env))
    shutil.rmtree(repo / ".git")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("not a repository\n")

    await cleaning.clean_epoch(
        Scripted(_cleaned), Config(work_paths=("src",)), env, manifest, store, 1
    )

    assert (tmp_path / "unreadable-store-epoch-001.git" / "HEAD").exists()
    at = ("--git-dir", str(tmp_path / "history.git"))
    assert _git(repo, *at, "rev-list", "--count", "refs/runs/store/epoch-001") == "1"


@pytest.mark.asyncio
async def test_measured_overages_go_back_as_repairs_then_the_flow_cuts(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    manifest = set(await tree.listed(env))

    def leave_junk(_prompt: str) -> Cleaned:
        (repo / "junk").mkdir()
        for index in range(3):
            (repo / "junk" / f"{index}.log").write_text("x\n")
        return _cleaned()

    cleaner = Scripted(leave_junk, lambda _prompt: "tried", lambda _prompt: "tried")
    await cleaning.clean_epoch(
        cleaner, Config(work_paths=("src",)), env, manifest, store, 1
    )

    assert len(cleaner.prompts) == 3
    assert "3 stray file(s) in junk/ (3)" in cleaner.prompts[1]
    assert not list((repo / "junk").iterdir())


@pytest.mark.asyncio
async def test_a_repair_that_never_lands_is_retried_then_the_flow_cuts(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    manifest = set(await tree.listed(env))

    def leave_junk(_prompt: str) -> Cleaned:
        (repo / "junk.txt").write_text("x\n")
        return _cleaned()

    silent = [lambda _prompt: ""] * cleaning.DELIVERY_TRIES
    cleaner = Scripted(leave_junk, *silent)
    await cleaning.clean_epoch(
        cleaner, Config(work_paths=("src",)), env, manifest, store, 1
    )

    assert len(cleaner.prompts) == 1 + cleaning.DELIVERY_TRIES
    assert not (repo / "junk.txt").exists()


@pytest.mark.asyncio
async def test_a_cleaner_the_clock_ended_gets_no_repairs(
    repo: Path, env: Any, store: PurePosixPath
) -> None:
    manifest = set(await tree.listed(env))

    async def slow(_prompt: str) -> None:
        (repo / "junk.txt").write_text("x\n")
        await asyncio.sleep(0.15)

    cleaner = Scripted(slow)
    held = Config(
        work_paths=("src",),
        session_timeout_minutes=0.001,
        idle_timeout_minutes=0,
        stop_grace_minutes=0,
    )
    await cleaning.clean_epoch(cleaner, held, env, manifest, store, 1)

    assert len(cleaner.prompts) == 1
    assert not (repo / "junk.txt").exists()


async def _guarded(
    agent: Scripted, *, wall: float, idle: float, grace: float
) -> tuple[Any, bool]:
    return await guard.guarded(
        agent,
        await agent.spawn(env=None),
        "task",
        session_timeout_minutes=wall,
        idle_timeout_minutes=idle,
        stop_grace_minutes=grace,
        label="test",
    )


def _sleeps(seconds: float) -> Callable[[str], Any]:
    async def sleep(_prompt: str, **_context: Any) -> str:
        await asyncio.sleep(seconds)
        return "done"

    return sleep


@pytest.mark.asyncio
async def test_the_clock_asks_for_a_wrap_up_and_sets_the_cut_off() -> None:
    agent = Scripted(_sleeps(0.15))

    said, timed_out = await _guarded(agent, wall=0.001, idle=0, grace=0.002)

    assert (said, timed_out) == ("done", True)
    budget = agent.budgets[0]
    assert budget is not None
    assert budget.duration == dt.timedelta(seconds=0.18)
    assert budget.graceful is False
    assert len(agent.steered) == 1
    assert "within 0.002 minutes" in agent.steered[0]


@pytest.mark.asyncio
async def test_idle_reminders_rearm_after_progress_and_never_end_a_turn() -> None:
    agent = Scripted(_sleeps(0.2))
    agent.moves_when_told = True

    said, timed_out = await _guarded(agent, wall=0, idle=0.001, grace=0)

    assert (said, timed_out) == ("done", False)
    assert agent.budgets == [None]
    assert len(agent.steered) >= 2
    assert all("carry on" in said for said in agent.steered)


@pytest.mark.asyncio
async def test_one_idle_reminder_per_idle_stretch() -> None:
    agent = Scripted(_sleeps(0.2))

    await _guarded(agent, wall=0, idle=0.001, grace=0)

    assert len(agent.steered) == 1


@pytest.mark.asyncio
async def test_nothing_is_watched_when_both_limits_are_off() -> None:
    before = len(asyncio.all_tasks())
    seen: list[int] = []

    def count(_prompt: str) -> str:
        seen.append(len(asyncio.all_tasks()))
        return "done"

    agent = Scripted(count)
    said, timed_out = await _guarded(agent, wall=0, idle=0, grace=0)

    assert (said, timed_out) == ("done", False)
    assert seen == [before]
    assert agent.budgets == [None]


class Agents(AgentCollection):
    coder: Worker


class Envs(EnvCollection):
    workspace: Workspace


class Turn(FlowParams):
    wall: float = 0.002
    grace: float = 0.002


@flow(agents=Agents, envs=Envs, params=Turn)
async def one_turn(
    task: str, *, agents: Agents, envs: Envs, params: Turn, ctx: FlowContext
) -> bool:
    held = Config(
        work_paths=("src",),
        session_timeout_minutes=params.wall,
        idle_timeout_minutes=0,
        stop_grace_minutes=params.grace,
    )
    return await loop.coding_turn(agents["coder"], task, envs["workspace"], held, "t")


@pytest.mark.asyncio
async def test_humanize_cuts_a_long_turn_off_and_the_turn_counts(
    env: Any,
) -> None:
    async def long(_prompt: str, *, session: Any, output_schema: Any) -> str:
        await session.until_steered()
        deadline = session.requests[-1].limits.deadline
        await asyncio.sleep(max(deadline - time.monotonic(), 0.0) + 0.05)
        return "still going"

    coder = FakeAgentDriver(reply=long)
    began = time.monotonic()

    landed = await run_fake(one_turn, "task", agents={"coder": coder}, local=env)

    assert landed is True
    session = coder.sessions[0]
    limits = session.requests[0].limits
    assert limits.graceful is False
    assert limits.deadline is not None
    assert limits.deadline - began == pytest.approx(0.24, abs=0.1)
    assert session.steered[0][0].startswith("You have been working for")
    assert time.monotonic() - began < 1.5


@pytest.mark.asyncio
async def test_a_tighter_flow_budget_wins_and_is_not_taken_for_the_clock(
    env: Any,
) -> None:
    coder = FakeAgentDriver(reply=_sleeps(5.0))

    with pytest.raises(DurationExceeded):
        await run_fake(
            one_turn,
            "task",
            agents={"coder": coder},
            params={"wall": 1, "grace": 1},
            budget=Budget(duration=dt.timedelta(seconds=0.2), graceful=False),
            local=env,
        )

    limits = coder.sessions[0].requests[0].limits
    assert limits.deadline is not None
    assert limits.graceful is False
