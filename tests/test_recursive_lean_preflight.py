from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

FLOW = Path(__file__).parents[1] / "flows" / "recursive_lean_prover"
sys.path.insert(0, str(FLOW))

from _recursive_lean.models import FetchedProblem, NaturalProof, SolveResult
from _recursive_lean.preflight import (
    REFERENCE_SOURCES,
    ReferenceBundle,
    ReferenceLibrary,
    infer_problem_id,
)
from _recursive_lean.prompts import (
    DECOMPOSE,
    DECOMPOSITION_AUDIT,
    INTEGRATION_AUDIT,
    INTEGRATION_REPAIR,
    LEAN_AUDIT,
    NATURAL_AUDIT,
    NATURAL_PROOF,
    PLAN_DRAFT,
    RLCR_LEAN_TASK,
)
from _recursive_lean.runtime import Runtime


def problem_markdown(problem_id: str = "mihailescu") -> str:
    return f"""# Mihăilescu's theorem

> Source: [Lean AI formalization leaderboard](https://lean-lang.org/eval/problems/{problem_id}/)
> Crawled: 2026-09-08
> Leaderboard data generated: 2026-09-08T10:02:54Z

## Leaderboard entry

| Field | Value |
| --- | --- |
| Problem id | `{problem_id}` |
| Group | `formalization-evaluation` |
| Statement revision | `1` |
| Module | `LeanEval.NumberTheory.Mihailescu` |

## Problem

The one selected mathematical problem.

## Data limitations

- None relevant to this fixture.
"""


def fetched_problem() -> FetchedProblem:
    return FetchedProblem(
        problem_id="mihailescu",
        title="Mihăilescu's theorem",
        source_url="https://lean-lang.org/eval/problems/mihailescu/",
        data_url=(
            "https://lean-lang.org/eval/site-data/v2/problems/mihailescu.json"
        ),
        generated_at="2026-09-08T10:02:54Z",
        statement_revision=1,
        module="LeanEval.NumberTheory.Mihailescu",
        markdown=problem_markdown(),
    )


def problem_site_data() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "generated_at": "2026-09-08T10:02:54Z",
        "problem": {
            "id": "mihailescu",
            "title": "Mihăilescu's theorem",
            "statement_revision": 1,
            "module": "LeanEval.NumberTheory.Mihailescu",
            "stable_url": "problems/mihailescu/",
        },
    }


def reference_use() -> list[dict[str, Any]]:
    return [
        {
            "source": source,
            "queries": ["catalan cyclotomic"],
            "files": [f"/references/{source}/README.md"],
            "conclusion": "searched and recorded a relevant or explicit no-match result",
        }
        for source in ("TauCeti", "lean-pool", "mathlib-internal")
    ]


def runtime_config() -> SimpleNamespace:
    return SimpleNamespace(
        artifact_dir=".humanize/recursive-lean-prover",
        wiki_dir=".humanize/math-wiki",
        reference_dir=".humanize/math-reference-library",
        huggingface_token_env="HF_TOKEN",
        problem_id="mihailescu",
        problem_fetch_attempts=3,
        max_parallel_children=1,
        lean_target="Submission.lean",
    )


class ProblemSession:
    def __init__(self, result: FetchedProblem) -> None:
        self.result = result
        self.calls: list[tuple[str, Any]] = []

    def __call__(
        self, prompt: str, *, suppress: bool = False, schema: Any = None
    ) -> FetchedProblem:
        self.calls.append((prompt, schema))
        return self.result.model_copy(deep=True)


class FetchAgent:
    def __init__(self, session: ProblemSession) -> None:
        self.session = session
        self.config = SimpleNamespace(web_search=True)
        self.clone_names: list[str | None] = []
        self.new_calls = 0

    def clone(self, *, name: str | None = None, **_: Any) -> FetchAgent:
        self.clone_names.append(name)
        return self

    def new(self, _: Path | None = None) -> ProblemSession:
        self.new_calls += 1
        return self.session


class PreflightTests(unittest.TestCase):
    def test_fetched_problem_schema_rejects_a_collection_or_second_problem(self) -> None:
        baseline = fetched_problem().model_dump()
        with self.assertRaisesRegex(ValueError, "canonical leaf URL"):
            FetchedProblem.model_validate(
                baseline | {"source_url": "https://lean-lang.org/eval/problems/"}
            )
        with self.assertRaisesRegex(ValueError, "exactly one top-level heading"):
            FetchedProblem.model_validate(
                baseline | {"markdown": problem_markdown() + "\n# A second problem\n"}
            )
        with self.assertRaisesRegex(ValueError, "Problem id rows that all match"):
            FetchedProblem.model_validate(
                baseline
                | {
                    "markdown": problem_markdown()
                    + "\n| Problem id | `dimitrov` |\n"
                }
            )

    def test_reference_aware_outputs_require_all_three_sources(self) -> None:
        NaturalProof(
            reference_use=reference_use(),
            proof="A sufficiently detailed numbered proof for the fixture.",
            key_steps=["Conclude the fixture."],
            unresolved=[],
        )
        incomplete = reference_use()[:2]
        with self.assertRaisesRegex(ValueError, "at least 3 items"):
            NaturalProof(
                reference_use=incomplete,
                proof="A sufficiently detailed numbered proof for the fixture.",
                key_steps=["Conclude the fixture."],
                unresolved=[],
            )

    def test_problem_id_is_resolved_before_the_agent_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "unexpected-worktree-name"
            project.mkdir()
            (project / "README.md").write_text(
                "# Fixture\n\n- Problem ID: `mihailescu`\n",
                encoding="utf-8",
            )
            self.assertEqual(infer_problem_id(project, "", "prove it"), "mihailescu")
            self.assertEqual(
                infer_problem_id(project, "mihailescu", "prove it"), "mihailescu"
            )
            with self.assertRaisesRegex(RuntimeError, "conflicting Lean-Eval problem ids"):
                infer_problem_id(
                    project,
                    "dimitrov",
                    "use https://lean-lang.org/eval/problems/mihailescu/",
                )

    def test_reference_download_is_complete_pinned_and_token_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "references"
            helper = Path(temporary) / "askpass.sh"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            helper.chmod(0o700)
            clone_calls: list[tuple[list[str], dict[str, str]]] = []
            checkout_sources: dict[Path, Any] = {}
            status_calls = 0

            def fake_run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
                nonlocal status_calls
                if "clone" in arguments:
                    destination = Path(arguments[-1])
                    destination.mkdir(parents=True)
                    (destination / ".git").mkdir()
                    source = next(
                        one for one in REFERENCE_SOURCES if one.url in arguments
                    )
                    checkout_sources[destination] = source
                    for sentinel in source.sentinels:
                        path = destination / sentinel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("fixture\n", encoding="utf-8")
                    clone_calls.append((arguments, kwargs["env"]))
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if arguments[1:] == ["remote", "get-url", "origin"]:
                    checkout = Path(kwargs["cwd"])
                    source = checkout_sources.get(checkout) or next(
                        one
                        for one in REFERENCE_SOURCES
                        if one.directory == checkout.name
                    )
                    return SimpleNamespace(
                        returncode=0,
                        stdout=source.url + "\n",
                        stderr="",
                    )
                if arguments[1:] == ["status", "--porcelain"]:
                    status_calls += 1
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")

            with (
                patch.dict(os.environ, {"TEST_HF_TOKEN": "top-secret-token"}),
                patch("_recursive_lean.preflight.subprocess.run", side_effect=fake_run),
            ):
                bundle = ReferenceLibrary(
                    root,
                    huggingface_token_env="TEST_HF_TOKEN",
                    askpass_script=helper,
                ).prepare()

            self.assertEqual(set(bundle.paths), {one.name for one in REFERENCE_SOURCES})
            self.assertTrue(bundle.manifest.is_file())
            first_manifest = bundle.manifest.read_text()
            self.assertNotIn("top-secret-token", first_manifest)
            self.assertEqual(len(clone_calls), 3)
            self.assertEqual(status_calls, 6)
            self.assertTrue(
                all("top-secret-token" not in " ".join(call[0]) for call in clone_calls)
            )
            self.assertTrue(
                all("TEST_HF_TOKEN" not in call[1] for call in clone_calls)
            )
            self.assertTrue(
                all("HUMANIZE_HF_TOKEN" not in call[1] for call in clone_calls[:2])
            )
            private_environment = clone_calls[-1][1]
            self.assertEqual(private_environment["HUMANIZE_HF_TOKEN"], "top-secret-token")
            self.assertTrue(
                all(
                    not path.stat().st_mode & 0o222
                    for checkout in bundle.paths.values()
                    for path in [checkout, *checkout.rglob("*")]
                    if not path.is_symlink()
                )
            )
            with patch(
                "_recursive_lean.preflight.subprocess.run", side_effect=fake_run
            ):
                resumed = ReferenceLibrary(
                    root,
                    huggingface_token_env="TEST_HF_TOKEN",
                    askpass_script=helper,
                ).prepare()
            self.assertEqual(resumed.commits, bundle.commits)
            self.assertEqual(status_calls, 6)
            self.assertEqual(bundle.manifest.read_text(), first_manifest)
            context = bundle.prompt_context()
            for source in ("TauCeti", "lean-pool", "mathlib-internal"):
                self.assertIn(source, context)

    def test_dedicated_session_writes_and_reuses_one_problem_markdown(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "mihailescu"
            project.mkdir()
            try:
                os.chdir(project)
                session = ProblemSession(fetched_problem())
                worker = FetchAgent(session)
                runtime = Runtime(
                    SimpleNamespace(worker=worker, reviewer=worker),
                    "prove the configured theorem",
                    runtime_config(),
                    {},
                )
                reference_root = project / ".humanize/math-reference-library"
                runtime.reference_bundle = ReferenceBundle(
                    root=reference_root,
                    manifest=reference_root / "manifest.json",
                    paths={
                        source.name: reference_root / source.directory
                        for source in REFERENCE_SOURCES
                    },
                    commits={source.name: "a" * 40 for source in REFERENCE_SOURCES},
                )
                runtime.problem_id = "mihailescu"

                with patch.object(
                    runtime, "_problem_site_data", return_value=problem_site_data()
                ):
                    first = runtime._fetched_problem()
                    (runtime.run_root / "problem.json").unlink()
                    second = runtime._fetched_problem()

                self.assertEqual(first, second)
                self.assertTrue((runtime.run_root / "problem.json").is_file())
                self.assertEqual(worker.new_calls, 1)
                self.assertEqual(
                    worker.clone_names, ["lean-eval-single-problem-fetcher"]
                )
                self.assertEqual(len(session.calls), 1)
                self.assertIs(session.calls[0][1], FetchedProblem)
                written = runtime.problem_path.read_text()
                self.assertEqual(len(re.findall(r"(?m)^# ", written)), 1)
                self.assertIn("only permitted problem id: `mihailescu`", session.calls[0][0])
            finally:
                os.chdir(original)

    def test_problem_candidate_is_checked_against_controller_site_data(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "mihailescu"
            project.mkdir()
            try:
                os.chdir(project)
                runtime = Runtime(None, "prove it", runtime_config(), {})
                runtime.problem_id = "mihailescu"
                data = problem_site_data()
                self.assertEqual(
                    runtime._problem_authority_feedback(fetched_problem(), data), ""
                )
                data["problem"]["title"] = "A different authoritative title"
                self.assertIn(
                    "title must be",
                    runtime._problem_authority_feedback(fetched_problem(), data),
                )
            finally:
                os.chdir(original)

    def test_digest_identity_resumes_when_latest_points_to_another_task(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "mihailescu"
            project.mkdir()
            try:
                os.chdir(project)
                first = Runtime(None, "prove it", runtime_config(), {})
                other = Runtime(None, "prove a different theorem", runtime_config(), {})
                latest = project / runtime_config().artifact_dir / "LATEST"
                latest.parent.mkdir(parents=True, exist_ok=True)
                latest.write_text(
                    str(other.run_root.relative_to(project)) + "\n",
                    encoding="utf-8",
                )

                resumed = Runtime(None, "prove it", runtime_config(), {})

                self.assertEqual(resumed.run_root, first.run_root)
            finally:
                os.chdir(original)

    def test_untrusted_hmz_run_dir_cannot_escape_artifact_root(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "mihailescu"
            project.mkdir()
            try:
                os.chdir(project)
                task = "prove it"
                digest = hashlib.sha256(f"mihailescu\0{task}".encode()).hexdigest()
                runtime = Runtime(
                    None,
                    task,
                    runtime_config(),
                    {"version": 1, "task_digest": digest, "run_dir": "."},
                )
                artifact_root = project / runtime_config().artifact_dir
                self.assertTrue(runtime.run_root.is_relative_to(artifact_root))
                self.assertNotEqual(runtime.run_root, project)
            finally:
                os.chdir(original)

    def test_reference_evidence_must_point_inside_each_snapshot(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "mihailescu"
            project.mkdir()
            try:
                os.chdir(project)
                runtime = Runtime(None, "prove it", runtime_config(), {})
                reference_root = project / ".humanize/math-reference-library"
                paths: dict[str, Path] = {}
                for source in REFERENCE_SOURCES:
                    path = reference_root / source.directory
                    path.mkdir(parents=True)
                    (path / "README.md").write_text("fixture\n", encoding="utf-8")
                    paths[source.name] = path
                runtime.reference_bundle = ReferenceBundle(
                    root=reference_root,
                    manifest=reference_root / "manifest.json",
                    paths=paths,
                    commits={source.name: "a" * 40 for source in REFERENCE_SOURCES},
                )
                valid = NaturalProof(
                    reference_use=[
                        {
                            "source": source.name,
                            "queries": ["fixture"],
                            "files": [str(paths[source.name] / "README.md")],
                            "conclusion": "fixture lookup",
                        }
                        for source in REFERENCE_SOURCES
                    ],
                    proof="A sufficiently detailed numbered proof for the fixture.",
                    key_steps=["Conclude the fixture."],
                    unresolved=[],
                )
                self.assertEqual(runtime._reference_use_problem(valid), "")
                invalid_data = valid.model_dump()
                invalid_data["reference_use"][0]["files"] = ["/tmp/not-in-snapshot"]
                invalid = NaturalProof.model_validate(invalid_data)
                self.assertIn("did not cite", runtime._reference_use_problem(invalid))
            finally:
                os.chdir(original)

    def test_bootstrap_finishes_before_the_root_solver_starts(self) -> None:
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "mihailescu"
            project.mkdir()
            try:
                os.chdir(project)
                runtime = Runtime(None, "prove it", runtime_config(), {})
                events: list[str] = []

                def bootstrap() -> FetchedProblem:
                    events.append("bootstrap")
                    return fetched_problem()

                def solve(_: Any) -> SolveResult:
                    events.append("solve")
                    return SolveResult(ok=True, node_id="root")

                with (
                    patch.object(runtime, "_require_git"),
                    patch.object(runtime, "_require_comparator"),
                    patch.object(runtime, "_bootstrap", side_effect=bootstrap),
                    patch.object(runtime, "_solve", side_effect=solve),
                ):
                    runtime.execute()

                self.assertEqual(events, ["bootstrap", "solve"])
            finally:
                os.chdir(original)

    def test_every_reasoning_prompt_receives_problem_and_reference_context(self) -> None:
        prompts = (
            PLAN_DRAFT,
            NATURAL_PROOF,
            NATURAL_AUDIT,
            DECOMPOSE,
            DECOMPOSITION_AUDIT,
            RLCR_LEAN_TASK,
            LEAN_AUDIT,
            INTEGRATION_REPAIR,
            INTEGRATION_AUDIT,
        )
        for prompt in prompts:
            self.assertIn("{problem_context}", prompt)
            self.assertIn("{reference_context}", prompt)

    def test_child_formalization_removes_unproved_inherited_placeholders(self) -> None:
        self.assertIn("remove that placeholder declaration before comparison", RLCR_LEAN_TASK)
        self.assertIn("Never replace it with a fake", RLCR_LEAN_TASK)
        self.assertIn(
            "Do not recover or inspect them through Git objects/history", RLCR_LEAN_TASK
        )
        for field in ("{node_id}", "{node_title}", "{lean_name}", "{lean_statement}"):
            self.assertIn(field, NATURAL_AUDIT)


if __name__ == "__main__":
    unittest.main()
