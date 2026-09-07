from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from hmz.flows import configures, drives, held, offered, resumes
from hmz.flows.skills import brought

FLOW = Path(__file__).parents[1] / "flows" / "recursive_lean_prover"
sys.path.insert(0, str(FLOW))

from __init__ import WorktreeRlcrConfig, _nested_rlcr_config
from _recursive_lean.models import Decomposition, NodeRecord, SolveResult, Subproblem
from _recursive_lean.prompts import RLCR_LEAN_TASK
from _recursive_lean.runtime import Runtime, _WorkspaceAgent
from _recursive_lean.store import Store


def git(cwd: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


class FakeSession:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    def __call__(
        self, prompt: str, *, suppress: bool = False, schema: Any = None
    ) -> tuple[str, Path, bool, Any]:
        return prompt, self.cwd, suppress, schema


class FakeAgent:
    def __init__(self) -> None:
        self.epic: Any = None
        self.effort = "max"
        self.backend = "codex"
        self.config = SimpleNamespace(
            model="gpt-5.6-sol",
            effort="max",
            service_tier="default",
            permission="auto",
            web_search=True,
            provider="",
            overrides=(),
        )
        self.opened_in: list[Path] = []

    def new(self, cwd: str | os.PathLike[str] | None = None) -> FakeSession:
        where = Path(cwd or Path.cwd()).resolve()
        self.opened_in.append(where)
        return FakeSession(where)

    def clone(self, **_: Any) -> FakeAgent:
        return FakeAgent()


class WorktreeTests(unittest.TestCase):
    def test_nested_rlcr_does_not_enable_generic_code_review(self) -> None:
        config = WorktreeRlcrConfig(
            plan_file="/tmp/immutable-plan.md",
            base_branch="frozen-post-overlay-base",
        )

        forwarded = _nested_rlcr_config(config)

        self.assertEqual(config.base_branch, "frozen-post-overlay-base")
        self.assertEqual(forwarded["base_branch"], "")
        self.assertFalse(forwarded["skip_impl"])

    def test_public_recursive_lean_flow_contract(self) -> None:
        base = FLOW / "__init__.py"

        self.assertEqual(drives(base), ("worker", "reviewer"))
        self.assertTrue(resumes(base))
        config = configures(base)
        self.assertIsNotNone(config)
        self.assertEqual(config.__name__, "Config")
        self.assertEqual([flow.name for flow in held(base)], ["", "worktree-rlcr"])
        self.assertEqual(
            [skill.name for skill in brought(FLOW)], ["recursive-lean-proof"]
        )
        self.assertIn("recursive_lean_prover", offered(FLOW.parent))

    def test_proved_and_accepted_nodes_reject_regressive_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "run", root / "wiki", "monotone fixture")
            proved = store.ensure(
                "root.proved-a1",
                parent=None,
                depth=0,
                title="Proved",
                statement="A proved theorem",
            )
            store.update(
                proved.id,
                "proved",
                "accepted",
                candidate_commit="proved-candidate",
                theorems=["Submission.proved"],
            )
            store.update(proved.id, "natural-proof", "must be ignored")
            self.assertEqual(proved.status, "proved")
            self.assertEqual(proved.message, "accepted")

            integrating = store.ensure(
                "root.integrating-a1",
                parent=None,
                depth=0,
                title="Integrating",
                statement="An accepted theorem",
            )
            store.update(
                integrating.id,
                "integrating",
                "accepted candidate",
                candidate_commit="candidate",
            )
            store.update(integrating.id, "planning", "must be ignored")
            self.assertEqual(integrating.status, "integrating")
            self.assertEqual(integrating.message, "accepted candidate")

    def test_mermaid_arrows_point_from_dependent_to_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(root / "run", root / "wiki", "diagram fixture")
            store.ensure(
                "root",
                parent=None,
                depth=0,
                title="Root",
                statement="Root theorem",
            )
            store.ensure(
                "root.base-a1",
                parent="root",
                depth=1,
                title="Base",
                statement="Base theorem",
            )
            store.ensure(
                "root.after-a1",
                parent="root",
                depth=1,
                title="After",
                statement="Dependent theorem",
                depends_on=["root.base-a1"],
            )

            diagram = (root / "run" / "dag.mmd").read_text()
            self.assertIn("Every solid arrow A --&gt; B means A depends on B", diagram)
            self.assertIn("n_root --> n_root_base_a1", diagram)
            self.assertIn("n_root_after_a1 --> n_root_base_a1", diagram)
            self.assertNotIn("n_root_base_a1 --> n_root_after_a1", diagram)
            self.assertNotIn("-.->", diagram)

    def test_workspace_agent_binds_new_sessions(self) -> None:
        wanted = Path("/tmp/isolated-node").resolve()
        base = FakeAgent()
        agent = _WorkspaceAgent(base, wanted)

        result = agent("prove it", suppress=True, schema=dict)

        self.assertEqual(result, ("prove it", wanted, True, dict))
        self.assertEqual(base.opened_in, [wanted])
        self.assertEqual(agent.new("/tmp/override").cwd, wanted)
        self.assertIsInstance(agent.clone(), _WorkspaceAgent)

    def test_rlcr_process_has_real_worktree_cwd(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "process_problem"
            project.mkdir()
            worktree = Path(temporary) / "node_worktree"
            worktree.mkdir()
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                    rlcr_rounds=20,
                )
                agents = SimpleNamespace(worker=FakeAgent(), reviewer=FakeAgent())
                runtime = Runtime(agents, "process fixture", config, {})
                node = NodeRecord(
                    id="root.process-a1",
                    title="Process leaf",
                    statement="True",
                    attempts=1,
                )
                plan = project / "plan.md"
                plan.write_text("# Plan\n")
                with (
                    patch(
                        "_recursive_lean.runtime.shutil.which",
                        return_value="/usr/bin/hmz",
                    ),
                    patch("_recursive_lean.runtime.subprocess.run") as launched,
                ):
                    launched.return_value = SimpleNamespace(returncode=0)
                    passed, log = runtime._run_rlcr_process(
                        node,
                        worktree,
                        plan,
                        "prove the node",
                        "frozen-post-overlay-base",
                    )

                self.assertTrue(passed)
                self.assertTrue(log.is_file())
                self.assertEqual(launched.call_args.kwargs["cwd"], worktree)
                command = launched.call_args.args[0]
                self.assertIn(":worktree-rlcr", command[3])
                self.assertEqual(command[-1], "prove the node")
                self.assertTrue(any("web_search=on" in part for part in command))
                rlcr_config = json.loads(
                    (
                        runtime._node_dir(node)
                        / f"rlcr-config-v{node.attempts}.json"
                    ).read_text()
                )
                self.assertEqual(
                    rlcr_config["base_branch"], "frozen-post-overlay-base"
                )
            finally:
                os.chdir(original)

    def test_nested_plan_cannot_reopen_accepted_decomposition(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "selected_node_problem"
            project.mkdir()
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                    lean_target="Submission.lean",
                    comparator_success="Your solution is okay!",
                )
                runtime = Runtime(None, "selected node fixture", config, {})
                node = NodeRecord(
                    id="root.selected-a1",
                    title="Selected theorem",
                    statement="True",
                    lean_name="selected",
                    lean_statement="True",
                    attempts=1,
                )
                scaffold = project / "one-time-plan.md"
                scaffold.write_text("# Historical speculative decomposition\n")
                natural = project / "natural-proof.md"
                natural.write_text("# Accepted mathematical proof\n")
                with patch.object(
                    runtime,
                    "_review_command",
                    return_value="bash exact-comparator.sh",
                ):
                    implementation = runtime._implementation_plan(
                        node,
                        accepted_plan=scaffold,
                        natural_path=natural,
                        children="- `Submission.child`: True",
                    ).read_text()

                self.assertIn("Authoritative selected-node contract", implementation)
                self.assertIn("override the current DAG", implementation)
                self.assertIn(
                    "DAG shape, extra certification interface", implementation
                )
                self.assertIn("authoritative implementation boundary", RLCR_LEAN_TASK)
                self.assertIn("do not reopen planning or decomposition", RLCR_LEAN_TASK)
                self.assertIn("Frozen proof-base commit", RLCR_LEAN_TASK)
                self.assertIn("empty list does not ban proof-base helpers", RLCR_LEAN_TASK)
            finally:
                os.chdir(original)

    def test_node_commit_is_isolated_then_integrated(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "example_problem"
            project.mkdir()
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Flow Test")
            git(project, "config", "user.email", "flow-test@example.invalid")
            (project / ".gitignore").write_text(
                ".humanize/\n.lake/\n/lake-manifest.json\n"
            )
            (project / "Submission.lean").write_text(
                "namespace Submission\nend Submission\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize fixture")
            (project / ".lake" / "packages").mkdir(parents=True)
            (project / "lake-manifest.json").write_text(
                '{"version": "1.1.0", "packages": []}\n'
            )

            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "fixture theorem", config, {})
                node = NodeRecord(
                    id="root.leaf-a1",
                    title="Leaf",
                    statement="True",
                    attempts=1,
                )

                worktree = runtime._node_worktree(node)
                before = runtime._git_head(worktree)
                self.assertEqual(worktree.name, project.name)
                self.assertEqual(runtime._git_toplevel(worktree), worktree)
                self.assertEqual(
                    git(worktree, "branch", "--show-current"),
                    runtime._node_branch(node),
                )
                original_branch = node.proof_branch
                original_base = node.proof_base_commit
                node.attempts += 1
                self.assertEqual(runtime._node_worktree(node), worktree)
                self.assertEqual(node.proof_branch, original_branch)
                self.assertEqual(node.proof_base_commit, original_base)
                self.assertTrue(runtime._git_clean(worktree))
                self.assertTrue((worktree / ".lake" / "packages").is_symlink())
                self.assertEqual(
                    (worktree / "lake-manifest.json").read_text(),
                    (project / "lake-manifest.json").read_text(),
                )

                # Reusing an already-recorded worktree also repairs disposable
                # ignored Lake inputs that vanished between process invocations.
                (worktree / "lake-manifest.json").unlink()
                self.assertEqual(runtime._node_worktree(node), worktree)
                self.assertTrue((worktree / "lake-manifest.json").is_file())

                proof = worktree / "Leaf.lean"
                proof.write_text("theorem leaf : True := by trivial\n")
                git(worktree, "add", "Leaf.lean")
                git(worktree, "commit", "-m", "feat: prove leaf")
                after = runtime._git_head(worktree)

                self.assertNotEqual(before, after)
                self.assertFalse((project / "Leaf.lean").exists())
                integrated, feedback = runtime._integrate_candidate(
                    worktree, before, after
                )
                self.assertTrue(integrated, feedback)
                self.assertTrue((project / "Leaf.lean").is_file())
                self.assertTrue(runtime._git_clean(project))
            finally:
                os.chdir(original)

    def test_node_worktree_finds_manifest_in_primary_git_worktree(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            primary = root / "primary"
            supervisor = root / "supervisor"
            primary.mkdir()
            git(primary, "init", "-b", "main")
            git(primary, "config", "user.name", "Flow Test")
            git(primary, "config", "user.email", "flow-test@example.invalid")
            (primary / ".gitignore").write_text(
                ".humanize/\n.lake/\n/lake-manifest.json\n"
            )
            (primary / "Submission.lean").write_text(
                "theorem seed : True := by trivial\n"
            )
            git(primary, "add", ".gitignore", "Submission.lean")
            git(primary, "commit", "-m", "test: initialize linked-worktree fixture")
            expected = '{"version": "1.1.0", "packages": []}\n'
            (primary / "lake-manifest.json").write_text(expected)
            git(
                primary,
                "worktree",
                "add",
                "-b",
                "supervisor",
                str(supervisor),
                "main",
            )

            try:
                os.chdir(supervisor)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "linked manifest fixture", config, {})
                node = NodeRecord(
                    id="root.linked_manifest-a1",
                    title="Linked manifest",
                    statement="True",
                    attempts=1,
                )

                worktree = runtime._node_worktree(node)

                self.assertFalse((supervisor / "lake-manifest.json").exists())
                self.assertEqual(
                    (worktree / "lake-manifest.json").read_text(), expected
                )
                self.assertTrue(runtime._git_clean(worktree))
            finally:
                os.chdir(original)

    def test_accepted_candidate_retries_only_integration(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "integration_retry_problem"
            project.mkdir()
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "integration retry fixture", config, {})
                node = runtime.store.ensure(
                    "root.accepted-a1",
                    parent="root",
                    depth=1,
                    title="Accepted theorem",
                    statement="True",
                )
                node.status = "integrating"
                node.natural_proof = "accepted-natural-proof.md"
                node.worktree = "/tmp/accepted-proof-worktree"
                node.proof_branch = "humanize-recursive/accepted"
                node.proof_base_commit = "base"
                node.candidate_commit = "candidate"

                with (
                    patch.object(
                        runtime,
                        "_integrate_candidate",
                        side_effect=[
                            (False, "combined history failed"),
                            (True, "agent-reconciled and integrated"),
                        ],
                    ) as integrate,
                    patch("_recursive_lean.runtime.time.sleep") as pause,
                ):
                    accepted, feedback = runtime._integrate_reviewed_candidate(
                        project,
                        "base",
                        "candidate",
                        node=node,
                        lean_files=["Submission.lean"],
                    )

                self.assertTrue(accepted)
                self.assertEqual(feedback, "agent-reconciled and integrated")
                self.assertEqual(integrate.call_count, 2)
                pause.assert_called_once_with(1.0)
                record = runtime.store.nodes[node.id]
                self.assertEqual(record.status, "integrating")
                self.assertIn("accepted proof retained", record.message)
                self.assertEqual(record.natural_proof, "accepted-natural-proof.md")
                self.assertEqual(record.worktree, "/tmp/accepted-proof-worktree")
                self.assertEqual(record.proof_branch, "humanize-recursive/accepted")
                self.assertEqual(record.proof_base_commit, "base")
                self.assertEqual(record.candidate_commit, "candidate")
            finally:
                os.chdir(original)

    def test_overlong_recorded_worktree_is_moved_to_short_path(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            deep = root
            for number in range(3):
                deep /= f"long-experiment-component-{number}-" + "x" * 36
            project = deep / "short_problem"
            project.mkdir(parents=True)
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Flow Test")
            git(project, "config", "user.email", "flow-test@example.invalid")
            (project / ".gitignore").write_text(".humanize/\n.lake/\n")
            (project / "Submission.lean").write_text(
                "theorem original : True := by trivial\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize long-path fixture")

            shortened: Path | None = None
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "long-path fixture", config, {})
                node = NodeRecord(
                    id="root.long_path_leaf-a1",
                    title="Long path leaf",
                    statement="True",
                    attempts=1,
                )
                branch = runtime._node_branch(node)
                old = (
                    project.parent
                    / ".recursive-lean-node-worktrees"
                    / runtime.run_root.name
                    / "long-path-leaf"
                    / "attempt-1"
                    / project.name
                )
                old.parent.mkdir(parents=True)
                git(project, "worktree", "add", "-b", branch, str(old), "HEAD")
                node.worktree = str(old)
                node.proof_branch = branch
                node.proof_base_commit = runtime._git_head(project)

                shortened = runtime._node_worktree(node)

                self.assertLessEqual(len(str(shortened)), 180)
                self.assertEqual(shortened.name, project.name)
                self.assertFalse(old.exists())
                self.assertEqual(runtime._git_toplevel(shortened), shortened)
            finally:
                if shortened is not None and shortened.exists():
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(shortened)],
                        cwd=project,
                        capture_output=True,
                        check=False,
                    )
                os.chdir(original)

    def test_two_ready_leaf_histories_integrate_from_parallel_worktrees(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "parallel_problem"
            project.mkdir()
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Flow Test")
            git(project, "config", "user.email", "flow-test@example.invalid")
            (project / ".gitignore").write_text(".humanize/\n.lake/\n")
            (project / "Submission.lean").write_text(
                "namespace Submission\nend Submission\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize parallel fixture")

            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "parallel fixture", config, {})
                nodes = [
                    NodeRecord(
                        id=f"root.leaf_{number}-a1",
                        title=f"Leaf {number}",
                        statement="True",
                        attempts=1,
                    )
                    for number in (1, 2)
                ]
                worktrees = [runtime._node_worktree(node) for node in nodes]
                bases = [runtime._git_head(worktree) for worktree in worktrees]
                heads: list[str] = []
                for number, worktree in enumerate(worktrees, 1):
                    proof = worktree / f"Leaf{number}.lean"
                    proof.write_text(f"theorem leaf{number} : True := by trivial\n")
                    git(worktree, "add", proof.name)
                    git(worktree, "commit", "-m", f"feat: prove leaf {number}")
                    heads.append(runtime._git_head(worktree))

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            runtime._integrate_candidate,
                            worktree,
                            before,
                            after,
                        )
                        for worktree, before, after in zip(
                            worktrees, bases, heads, strict=True
                        )
                    ]
                    results = [future.result() for future in futures]

                self.assertTrue(all(passed for passed, _ in results), results)
                self.assertTrue((project / "Leaf1.lean").is_file())
                self.assertTrue((project / "Leaf2.lean").is_file())
                self.assertTrue(runtime._git_clean(project))
            finally:
                os.chdir(original)

    def test_divergent_integration_supplies_its_own_committer_identity(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "identity_problem"
            project.mkdir()
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Fixture Author")
            git(project, "config", "user.email", "fixture@example.invalid")
            (project / ".gitignore").write_text(".humanize/\n.lake/\n")
            (project / "Submission.lean").write_text(
                "namespace Submission\n\nend Submission\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize identity fixture")

            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "identity fixture", config, {})
                node = NodeRecord(
                    id="root.identity_leaf-a1",
                    title="Identity leaf",
                    statement="True",
                    attempts=1,
                )
                worktree = runtime._node_worktree(node)
                before = runtime._git_head(worktree)
                (worktree / "Candidate.lean").write_text(
                    "theorem candidate : True := by trivial\n"
                )
                git(worktree, "add", "Candidate.lean")
                git(worktree, "commit", "-m", "feat: add candidate")
                after = runtime._git_head(worktree)

                (project / "Canonical.lean").write_text(
                    "theorem canonical : True := by trivial\n"
                )
                git(project, "add", "Canonical.lean")
                git(project, "commit", "-m", "feat: advance canonical")
                subprocess.run(
                    ["git", "config", "--unset-all", "user.name"],
                    cwd=project,
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "--unset-all", "user.email"],
                    cwd=project,
                    check=True,
                )

                with patch.dict(
                    os.environ,
                    {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
                ):
                    integrated, feedback = runtime._integrate_candidate(
                        worktree, before, after
                    )

                self.assertTrue(integrated, feedback)
                self.assertIn("rebased", feedback)
                self.assertTrue((project / "Candidate.lean").is_file())
                self.assertTrue((project / "Canonical.lean").is_file())
            finally:
                os.chdir(original)

    def test_stale_proof_base_skips_commits_already_on_canonical(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "stale_base_problem"
            project.mkdir()
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Flow Test")
            git(project, "config", "user.email", "flow-test@example.invalid")
            (project / ".gitignore").write_text(".humanize/\n.lake/\n")
            (project / "Submission.lean").write_text(
                "namespace Submission\n\nend Submission\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize stale-base fixture")

            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "stale-base fixture", config, {})
                node = NodeRecord(
                    id="root.stale_base-a1",
                    title="Stale-base leaf",
                    statement="True",
                    attempts=1,
                )
                worktree = runtime._node_worktree(node)
                stale_before = runtime._git_head(worktree)

                (project / "Canonical.lean").write_text(
                    "theorem canonical : True := by trivial\n"
                )
                git(project, "add", "Canonical.lean")
                git(project, "commit", "-m", "feat: advance canonical")
                canonical = runtime._git_head(project)

                # Model an official worker updating its long-lived branch while the
                # controller still retains the original proof-base checkpoint.
                git(worktree, "merge", "--ff-only", canonical)
                (worktree / "Candidate.lean").write_text(
                    "theorem candidate : True := by trivial\n"
                )
                git(worktree, "add", "Candidate.lean")
                git(worktree, "commit", "-m", "feat: prove candidate")
                after = runtime._git_head(worktree)

                integrated, feedback = runtime._integrate_candidate(
                    worktree, stale_before, after
                )

                self.assertTrue(integrated, feedback)
                self.assertTrue((project / "Canonical.lean").is_file())
                self.assertTrue((project / "Candidate.lean").is_file())
            finally:
                os.chdir(original)

    def test_duplicate_patch_is_an_accepted_empty_cherry_pick(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "duplicate_patch_problem"
            project.mkdir()
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Flow Test")
            git(project, "config", "user.email", "flow-test@example.invalid")
            (project / ".gitignore").write_text(".humanize/\n.lake/\n")
            (project / "Submission.lean").write_text(
                "namespace Submission\n\nend Submission\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize duplicate fixture")

            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "duplicate-patch fixture", config, {})
                node = NodeRecord(
                    id="root.duplicate-a1",
                    title="Duplicate leaf",
                    statement="True",
                    attempts=1,
                )
                worktree = runtime._node_worktree(node)
                before = runtime._git_head(worktree)
                duplicate = "theorem duplicate : True := by trivial\n"
                (worktree / "Duplicate.lean").write_text(duplicate)
                git(worktree, "add", "Duplicate.lean")
                git(worktree, "commit", "-m", "feat: candidate copy")
                after = runtime._git_head(worktree)

                (project / "Duplicate.lean").write_text(duplicate)
                git(project, "add", "Duplicate.lean")
                git(project, "commit", "-m", "feat: canonical copy")

                integrated, feedback = runtime._integrate_candidate(
                    worktree, before, after
                )

                self.assertTrue(integrated, feedback)
                self.assertEqual((project / "Duplicate.lean").read_text(), duplicate)
                self.assertTrue(runtime._git_clean(project))
            finally:
                os.chdir(original)

    def test_parallel_same_file_leaf_additions_are_union_integrated(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "same_file_problem"
            project.mkdir()
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Flow Test")
            git(project, "config", "user.email", "flow-test@example.invalid")
            (project / ".gitignore").write_text(".humanize/\n.lake/\n")
            (project / "Submission.lean").write_text(
                "namespace Submission\n\nend Submission\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize same-file fixture")

            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                )
                runtime = Runtime(None, "same-file fixture", config, {})
                nodes = [
                    NodeRecord(
                        id=f"root.same_{number}-a1",
                        title=f"Same-file leaf {number}",
                        statement="True",
                        attempts=1,
                    )
                    for number in (1, 2)
                ]
                worktrees = [runtime._node_worktree(node) for node in nodes]
                bases = [runtime._git_head(worktree) for worktree in worktrees]
                heads: list[str] = []
                for number, worktree in enumerate(worktrees, 1):
                    submission = worktree / "Submission.lean"
                    submission.write_text(
                        "namespace Submission\n\n"
                        f"theorem same{number} : True := by trivial\n\n"
                        "end Submission\n"
                    )
                    git(worktree, "add", "Submission.lean")
                    git(
                        worktree, "commit", "-m", f"feat: prove same-file leaf {number}"
                    )
                    heads.append(runtime._git_head(worktree))

                subprocess.run(
                    ["git", "config", "--unset-all", "user.name"],
                    cwd=project,
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "--unset-all", "user.email"],
                    cwd=project,
                    check=True,
                )
                with patch.dict(
                    os.environ,
                    {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
                ):
                    first = runtime._integrate_candidate(
                        worktrees[0], bases[0], heads[0]
                    )
                    second = runtime._integrate_candidate(
                        worktrees[1], bases[1], heads[1]
                    )

                self.assertTrue(first[0], first)
                self.assertTrue(second[0], second)
                combined = (project / "Submission.lean").read_text()
                self.assertIn("theorem same1", combined)
                self.assertIn("theorem same2", combined)
                self.assertTrue(runtime._git_clean(project))
            finally:
                os.chdir(original)

    def test_child_frontier_refills_without_waiting_for_slow_sibling(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "frontier_problem"
            project.mkdir()
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                    max_nodes=10,
                    max_parallel_children=4,
                )
                runtime = Runtime(None, "frontier fixture", config, {})
                parent = runtime.store.ensure(
                    "root",
                    parent=None,
                    depth=0,
                    title="Root",
                    statement="True",
                )
                decomposition = Decomposition(
                    should_split=True,
                    rationale="three-node dependency fixture",
                    subproblems=[
                        Subproblem(
                            key="slow",
                            title="Slow leaf",
                            statement="A slow independent theorem",
                            lean_statement="True",
                            lean_name="slow",
                            depends_on=[],
                        ),
                        Subproblem(
                            key="fast",
                            title="Fast leaf",
                            statement="A fast independent theorem",
                            lean_statement="True",
                            lean_name="fast",
                            depends_on=[],
                        ),
                        Subproblem(
                            key="after_fast",
                            title="Fast dependent",
                            statement="A theorem depending only on fast",
                            lean_statement="True",
                            lean_name="after_fast",
                            depends_on=["fast"],
                        ),
                    ],
                )
                moments: dict[str, float] = {}
                lock = threading.Lock()

                def solve(node: NodeRecord) -> SolveResult:
                    key = node.id.rsplit(".", 1)[-1].rsplit("-a", 1)[0]
                    with lock:
                        moments[f"start:{key}"] = time.monotonic()
                    time.sleep(0.25 if key == "slow" else 0.02)
                    with lock:
                        moments[f"end:{key}"] = time.monotonic()
                    return SolveResult(ok=True, node_id=node.id)

                runtime._solve = solve  # type: ignore[method-assign]
                results = runtime._solve_children(parent, decomposition, 1)

                self.assertTrue(all(result.ok for result in results))
                self.assertLess(moments["start:after_fast"], moments["end:slow"])
            finally:
                os.chdir(original)

    def test_redecomposition_reuses_proved_theorem_instead_of_creating_a2(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "reuse_problem"
            project.mkdir()
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                    max_nodes=10,
                    max_parallel_children=4,
                )
                runtime = Runtime(None, "reuse fixture", config, {})
                parent = runtime.store.ensure(
                    "root",
                    parent=None,
                    depth=0,
                    title="Root",
                    statement="Root theorem",
                )
                child = runtime.store.ensure(
                    "root.lemma-a1",
                    parent="root",
                    depth=1,
                    title="Lemma",
                    statement="Original statement",
                    lean_statement="True",
                    lean_name="stable_lemma",
                )
                child.status = "proved"
                child.theorems = ["Submission.stable_lemma"]
                decomposition = Decomposition(
                    should_split=True,
                    rationale="retry with the same theorem identity",
                    subproblems=[
                        Subproblem(
                            key="renamed_key",
                            title="Same lemma",
                            statement="A revised prose description",
                            lean_statement="True",
                            lean_name="stable_lemma",
                            depends_on=[],
                        ),
                        Subproblem(
                            key="second",
                            title="Second lemma",
                            statement="A second independent theorem",
                            lean_statement="True",
                            lean_name="second_lemma",
                            depends_on=[],
                        ),
                    ],
                )

                seen: list[str] = []

                def solve(node: NodeRecord) -> SolveResult:
                    seen.append(node.id)
                    node.status = "proved"
                    node.theorems = [f"Submission.{node.lean_name}"]
                    return SolveResult(
                        ok=True,
                        node_id=node.id,
                        theorems=runtime._checkpoint_theorems(node),
                    )

                runtime._solve = solve  # type: ignore[method-assign]
                results = runtime._solve_children(parent, decomposition, 9)

                self.assertTrue(all(result.ok for result in results))
                self.assertNotIn("root.lemma-a1", seen)
                self.assertIn("root.second-a1", seen)
                self.assertEqual(parent.children, ["root.lemma-a1", "root.second-a1"])
                self.assertFalse(
                    any(node_id.endswith("-a2") for node_id in runtime.store.nodes)
                )
            finally:
                os.chdir(original)

    def test_integrating_child_unlocks_its_dependent_without_reproving(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "accepted_frontier_problem"
            project.mkdir()
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                    max_nodes=10,
                    max_parallel_children=4,
                )
                runtime = Runtime(None, "accepted frontier fixture", config, {})
                parent = runtime.store.ensure(
                    "root",
                    parent=None,
                    depth=0,
                    title="Root",
                    statement="Root theorem",
                )
                accepted = runtime.store.ensure(
                    "root.accepted-a1",
                    parent="root",
                    depth=1,
                    title="Accepted child",
                    statement="An accepted child theorem",
                    lean_statement="True",
                    lean_name="accepted_child",
                )
                accepted.status = "integrating"
                accepted.candidate_commit = "candidate"
                accepted.theorems = ["Submission.accepted_child"]
                decomposition = Decomposition(
                    should_split=True,
                    rationale="one accepted prerequisite and its dependent",
                    subproblems=[
                        Subproblem(
                            key="accepted",
                            title="Accepted child",
                            statement="An accepted child theorem",
                            lean_statement="True",
                            lean_name="accepted_child",
                            depends_on=[],
                        ),
                        Subproblem(
                            key="dependent",
                            title="Dependent child",
                            statement="A theorem using the accepted child",
                            lean_statement="True",
                            lean_name="dependent_child",
                            depends_on=["accepted"],
                        ),
                    ],
                )
                started: list[str] = []

                def solve(node: NodeRecord) -> SolveResult:
                    started.append(node.id)
                    node.status = "proved"
                    node.theorems = [f"Submission.{node.lean_name}"]
                    return SolveResult(
                        ok=True,
                        node_id=node.id,
                        theorems=runtime._checkpoint_theorems(node),
                    )

                runtime._solve = solve  # type: ignore[method-assign]
                with patch.object(runtime, "_submit_resumed_integration") as promote:
                    results = runtime._solve_children(parent, decomposition, 5)

                self.assertTrue(all(result.ok for result in results))
                promote.assert_called_once_with(accepted)
                self.assertEqual(started, ["root.dependent-a1"])
            finally:
                os.chdir(original)

    def test_parent_worktree_overlays_accepted_child_candidate(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "accepted_overlay_problem"
            project.mkdir()
            git(project, "init", "-b", "main")
            git(project, "config", "user.name", "Flow Test")
            git(project, "config", "user.email", "flow-test@example.invalid")
            (project / ".gitignore").write_text(".humanize/\n.lake/\n")
            (project / "Submission.lean").write_text(
                "namespace Submission\nend Submission\n"
            )
            git(project, "add", ".gitignore", "Submission.lean")
            git(project, "commit", "-m", "test: initialize overlay fixture")
            try:
                os.chdir(project)
                config = SimpleNamespace(
                    artifact_dir=".humanize/recursive-lean-prover",
                    wiki_dir=".humanize/math-wiki",
                    max_parallel_children=4,
                )
                runtime = Runtime(None, "accepted overlay fixture", config, {})
                parent = runtime.store.ensure(
                    "root",
                    parent=None,
                    depth=0,
                    title="Root",
                    statement="Root theorem",
                )
                child = runtime.store.ensure(
                    "root.child-a1",
                    parent="root",
                    depth=1,
                    title="Child",
                    statement="Child theorem",
                    lean_name="accepted_child",
                )
                child.attempts = 1
                child_worktree = runtime._node_worktree(child)
                child_base = child.proof_base_commit
                (child_worktree / "Child.lean").write_text(
                    "theorem accepted_child : True := by trivial\n"
                )
                git(child_worktree, "add", "Child.lean")
                git(child_worktree, "commit", "-m", "feat: prove accepted child")
                child.status = "integrating"
                child.candidate_commit = runtime._git_head(child_worktree)
                child.theorems = ["Submission.accepted_child"]
                self.assertEqual(child.proof_base_commit, child_base)

                parent.attempts = 1
                parent_worktree = runtime._node_worktree(parent)
                passed, feedback = runtime._overlay_accepted_children(
                    parent, parent_worktree
                )

                self.assertTrue(passed, feedback)
                self.assertTrue((parent_worktree / "Child.lean").is_file())
                self.assertEqual(
                    (parent_worktree / "Child.lean").read_text(),
                    "theorem accepted_child : True := by trivial\n",
                )
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
