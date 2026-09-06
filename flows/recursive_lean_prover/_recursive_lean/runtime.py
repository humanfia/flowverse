"""Recursive orchestration built from Humanize agent turns and nested RLCR flows."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hmz.flows import Stopped, load

from .models import (
    Decomposition,
    DecompositionAudit,
    LeanAudit,
    NaturalAudit,
    NaturalProof,
    NodeRecord,
    ProvedTheorem,
    SolveResult,
    Subproblem,
)
from .prompts import (
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
from .store import Store, atomic_text, now, slug

if TYPE_CHECKING:
    from collections.abc import Iterable


GEN_PLAN = "official/humanize1:gen-plan"
RLCR = "official/humanize1:rlcr"
WORKTREE_RLCR = f"{Path(__file__).resolve().parent.parent}:worktree-rlcr"
INTEGRATION_GIT = (
    "git",
    "-c",
    "user.name=Humanize Recursive Integrator",
    "-c",
    "user.email=humanize-recursive@example.invalid",
)


class _WorkspaceAgent:
    """Run every session cloned from one Humanize agent in a fixed worktree."""

    def __init__(self, agent: Any, cwd: Path) -> None:
        self._agent = agent
        self._cwd = cwd

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    @property
    def epic(self) -> Any:
        return self._agent.epic

    @epic.setter
    def epic(self, value: Any) -> None:
        self._agent.epic = value

    @property
    def effort(self) -> str:
        return self._agent.effort

    @effort.setter
    def effort(self, value: str) -> None:
        self._agent.effort = value

    def __call__(
        self,
        prompt: str,
        *,
        suppress: bool = False,
        schema: Any = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> Any:
        del cwd
        session = self.new()
        if schema is None:
            return session(prompt, suppress=suppress)
        return session(prompt, suppress=suppress, schema=schema)

    def new(self, cwd: str | os.PathLike[str] | None = None) -> Any:
        del cwd
        return self._agent.new(self._cwd)

    def clone(
        self,
        *,
        config: Any = None,
        name: str | None = None,
        skills: Any = None,
    ) -> _WorkspaceAgent:
        arguments: dict[str, Any] = {}
        if config is not None:
            arguments["config"] = config
        if name is not None:
            arguments["name"] = name
        if skills is not None:
            arguments["skills"] = skills
        return _WorkspaceAgent(self._agent.clone(**arguments), self._cwd)


class Runtime:
    """One resumable recursive proof run."""

    def __init__(
        self,
        agents: Any,
        task: str,
        config: Any,
        state: dict[str, Any] | None,
    ) -> None:
        self.agents = agents
        self.task = task.strip()
        self.config = config
        self.state = state if state is not None else {}
        self.project = Path.cwd().resolve()
        # The graph and short integration operations are synchronized. Lean workers and
        # both comparator passes run in per-node Git worktrees, so every ready leaf may
        # formalize concurrently without sharing source, HEAD, or comparator scratch files.
        self._graph_lock = threading.RLock()
        self._integration_lock = threading.Lock()
        self._revision_lock = threading.Lock()
        self._integration_futures_lock = threading.RLock()
        self._integration_executor = ThreadPoolExecutor(
            max_workers=max(1, getattr(self.config, "max_parallel_children", 4)),
            thread_name_prefix="accepted-integration",
        )
        self._integration_futures: dict[str, Any] = {}
        self.run_root = self._run_root()
        self.store = Store(
            self.run_root,
            self.project / self.config.wiki_dir,
            self.task,
        )

    def execute(self) -> None:
        """Validate the host project, solve the root, and retain state only if unfinished."""
        if not self.task:
            raise ValueError("recursive_lean_prover needs a mathematical problem")
        self._require_git()
        self._require_comparator()
        latest = self.project / self.config.artifact_dir / "LATEST"
        atomic_text(latest, str(self.run_root.relative_to(self.project)) + "\n")
        self.state.update(
            version=1,
            task_digest=self._task_digest(),
            run_dir=str(self.run_root.relative_to(self.project)),
        )
        root = self.store.ensure(
            "root",
            parent=None,
            depth=0,
            title="Main theorem",
            statement=self.task,
            lean_name=self._root_lean_name(),
        )
        if root.status not in {"queued", "proved", "failed"}:
            self.store.update(
                "root", "interrupted", "resuming an interrupted root node"
            )
        print(f"Live DAG: {self.run_root / 'DAG.md'}")
        print(f"Theorem wiki: {self.project / self.config.wiki_dir / 'README.md'}")
        result = (
            self._resume_existing_dag(root)
            if root.children and root.plan and root.natural_proof
            else self._solve(root)
        )
        if result.ok:
            print(
                f"Proved root theorem; {len(result.theorems)} theorem record(s) at root."
            )
            self.state.clear()
            return
        self.state.update(
            version=1,
            task_digest=self._task_digest(),
            run_dir=str(self.run_root.relative_to(self.project)),
            last_failure=result.feedback,
        )
        print(f"Root theorem not accepted: {result.feedback}")

    def _solve(self, node: NodeRecord) -> SolveResult:
        """Solve one node; child calls use this same method and can split again."""
        if node.status == "proved":
            return SolveResult(
                ok=True,
                node_id=node.id,
                theorems=self._checkpoint_theorems(node),
            )
        if node.status == "integrating" and node.candidate_commit:
            return self._resume_accepted_candidate(node)
        feedback = node.message if node.status == "failed" else "None."
        # Once a plan passes its independent gate it is a stable scaffold.  Subsequent
        # mathematical corrections iterate the natural-language proof from its latest
        # checkpoint; they do not generate a fresh plan on every outer attempt.
        plan = self._recorded_plan(node)
        if plan is None:
            # A stopped direct gen-plan may leave either its substantive output in the
            # atomic-write temporary file or only the controller's concrete input draft.
            # Both are sufficient as an immutable scaffold: mathematical correction
            # belongs to the NL-proof loop, never to another plan generation/review loop.
            plan = self._preserved_plan(node)
            if plan is not None:
                self.store.update(
                    node.id,
                    "natural-proof",
                    f"existing scaffold {plan.name} frozen; iterate only the NL proof",
                    plan=str(plan.relative_to(self.project)),
                )
        plan_attempted = plan is not None
        while True:
            node.attempts += 1
            attempt = node.attempts
            if plan is None:
                if plan_attempted:
                    break
                plan_attempted = True
                plan = self._accepted_plan(node, feedback)
                if plan is not None:
                    feedback = node.message
            if plan is None:
                feedback = (
                    "One-time direct plan generation produced no usable scaffold."
                )
                break
            natural = self._accepted_natural_proof(node, plan, feedback)
            if natural is None:
                feedback = "No complete natural-language proof survived review."
                continue
            decomposition = self._decompose(node, natural)
            if decomposition is None:
                feedback = node.message or (
                    "The proposed subproblem graph was invalid or cyclic."
                )
                continue
            children = self._solve_children(node, decomposition, attempt)
            failed = [one for one in children if not one.ok]
            if failed and self.config.stop_on_child_failure:
                feedback = "Required child failure(s): " + "; ".join(
                    f"{one.node_id}: {one.feedback}" for one in failed
                )
                continue
            result = self._formalize(node, plan, natural, children)
            if result.ok:
                return result
            feedback = result.feedback
            if node.parent:
                self._revise_parent(node, feedback)
        self.store.update(node.id, "failed", feedback)
        return SolveResult(ok=False, node_id=node.id, feedback=feedback)

    def _accepted_plan(self, node: NodeRecord, feedback: str) -> Path | None:
        """Generate one immutable scaffold directly with humanize1:gen-plan."""
        preserved = self._preserved_plan(node) if node.status == "interrupted" else None
        if preserved is not None:
            self.store.update(
                node.id,
                "natural-proof",
                f"preserved scaffold {preserved.name} frozen; iterate only NL proof",
                plan=str(preserved.relative_to(self.project)),
            )
            return preserved
        for _ in range(1):
            version = self._next_version(node, "plan")
            node_dir = self._node_dir(node)
            draft = node_dir / f"plan-draft-v{version}.md"
            output = node_dir / f"plan-v{version}.md"
            body = PLAN_DRAFT.format(
                statement=node.statement,
                node_id=node.id,
                lean_name=node.lean_name or "to be chosen",
                depth=node.depth,
                parent=node.parent or "none",
                lean_target=self.config.lean_target
                or "the repository's appropriate Lean file",
                comparator_command=self._render_command(node, []),
                comparator_success=self.config.comparator_success,
                feedback=feedback or "None.",
            )
            atomic_text(draft, body)
            self.store.update(
                node.id,
                "planning",
                f"direct one-time plan generation {version}",
                attempts=node.attempts,
            )
            try:
                planning_agents = (
                    self.agents.worker.clone(),
                    self.agents.reviewer.clone(),
                )
                load(GEN_PLAN, inherit_skills=True)(
                    planning_agents,
                    f"Plan a correct natural and Lean proof for DAG node {node.id}",
                    {
                        "input": str(draft.relative_to(self.project)),
                        "output": str(output.relative_to(self.project)),
                        "mode": "direct",
                        # Planning must never start Lean implementation here.  This runtime
                        # first requires an independently accepted natural-language proof and
                        # a validated recursive decomposition, then invokes RLCR explicitly in
                        # _formalize.
                        "auto_start_rlcr_if_converged": False,
                        "turn_timeout": self.config.plan_turn_timeout,
                        "total_timeout": self.config.plan_total_timeout,
                        "turn_retries": 1,
                    },
                )
            except Stopped as error:
                # Direct plan generation gets one invocation; freeze its concrete input
                # draft on interruption so the node still advances to NL proof.
                feedback = f"humanize1:gen-plan stopped: {error}"
                self.store.update(
                    node.id,
                    "natural-proof",
                    f"direct plan stopped; scaffold draft {version} frozen for NL proof",
                    plan=str(draft.relative_to(self.project)),
                )
                return draft
            except Exception as error:  # noqa: BLE001
                feedback = f"humanize1:gen-plan failed: {error}"
                self.store.update(
                    node.id,
                    "natural-proof",
                    f"direct plan unavailable; scaffold draft {version} frozen for NL proof",
                    plan=str(draft.relative_to(self.project)),
                )
                return draft
            if not output.is_file() or not output.read_text(encoding="utf-8").strip():
                feedback = "humanize1:gen-plan did not produce a plan file"
                self.store.update(
                    node.id,
                    "natural-proof",
                    f"direct plan had no output; scaffold draft {version} frozen for NL proof",
                    plan=str(draft.relative_to(self.project)),
                )
                return draft
            self.store.update(
                node.id,
                "natural-proof",
                f"one-time scaffold plan {version} generated and frozen",
                plan=str(output.relative_to(self.project)),
            )
            return output
        return None

    def _accepted_natural_proof(
        self, node: NodeRecord, plan_path: Path, outer_feedback: str = ""
    ) -> NaturalProof | None:
        """Run the author/reviewer RLCR loop on prose before Lean starts."""
        plan = plan_path.read_text(encoding="utf-8")
        prior_proof, feedback = self._latest_natural_checkpoint(node)
        if outer_feedback and outer_feedback not in {
            "None.",
            "No complete natural-language proof survived review.",
        }:
            feedback = outer_feedback
        while True:
            for _ in range(self.config.natural_proof_attempts):
                version = self._next_json_version(node, "natural-proof-draft")
                self.store.update(
                    node.id,
                    "natural-proof",
                    f"natural-language RLCR author revision {version}",
                )
                proof = self.agents.worker.clone().new()(
                    NATURAL_PROOF.format(
                        statement=node.statement,
                        plan=plan,
                        feedback=feedback,
                        prior_proof=prior_proof,
                    ),
                    suppress=True,
                    schema=NaturalProof,
                )
                if proof is None:
                    feedback = "The worker returned no structured proof."
                    continue
                draft_path = (
                    self._node_dir(node) / f"natural-proof-draft-v{version}.json"
                )
                atomic_text(draft_path, proof.model_dump_json(indent=2) + "\n")
                atomic_text(
                    self._node_dir(node) / f"natural-proof-draft-v{version}.md",
                    f"# Natural-language proof draft {version}\n\n"
                    f"{proof.proof.strip()}\n\n"
                    "## Reported unresolved points\n\n"
                    + (
                        "\n".join(f"- {one}" for one in proof.unresolved)
                        if proof.unresolved
                        else "- None reported by the author."
                    )
                    + "\n",
                )
                # Proof recovery is deliberately monotone: every rejected proof becomes
                # the input to the next revision, even after one configured review batch
                # is exhausted. A theorem must not become terminal merely because its
                # natural-language proof needed more review iterations.
                prior_proof = proof.proof
                if proof.unresolved:
                    feedback = "Unresolved proof gaps: " + "; ".join(proof.unresolved)
                    atomic_text(
                        self._node_dir(node) / f"natural-feedback-v{version}.txt",
                        feedback + "\n",
                    )
                    continue
                self.store.update(
                    node.id,
                    "natural-review",
                    f"natural-language RLCR reviewer round {version}",
                )
                audit = self.agents.reviewer.clone()(
                    NATURAL_AUDIT.format(statement=node.statement, proof=proof.proof),
                    suppress=True,
                    schema=NaturalAudit,
                )
                if audit is not None:
                    atomic_text(
                        self._node_dir(node) / f"natural-audit-v{version}.json",
                        audit.model_dump_json(indent=2) + "\n",
                    )
                if audit is not None and audit.passed:
                    path = self._node_dir(node) / f"natural-proof-v{version}.md"
                    atomic_text(
                        path,
                        f"# Natural-language proof\n\n{proof.proof.strip()}\n\n"
                        "## Key steps\n\n"
                        + "\n".join(
                            f"{at}. {step}"
                            for at, step in enumerate(proof.key_steps, 1)
                        )
                        + "\n",
                    )
                    self.store.update(
                        node.id,
                        "decomposing",
                        "natural-language proof accepted before Lean",
                        natural_proof=str(path.relative_to(self.project)),
                    )
                    return proof
                feedback = self._natural_feedback(audit)
                atomic_text(
                    self._node_dir(node) / f"natural-feedback-v{version}.txt",
                    feedback + "\n",
                )
            self.store.update(
                node.id,
                "natural-proof",
                (
                    "natural-language review batch exhausted; continuing from "
                    f"draft {version}"
                ),
            )

    def _decompose(self, node: NodeRecord, proof: NaturalProof) -> Decomposition | None:
        """Ask for a bounded DAG after the prose proof, validating dependencies locally."""
        if node.depth >= self.config.max_depth:
            return Decomposition(
                should_split=False,
                rationale="configured recursion depth reached",
                subproblems=[],
            )
        feedback = "None."
        for attempt in range(1, self.config.decomposition_attempts + 1):
            self.store.update(
                node.id,
                "decomposing",
                f"subproblem decomposition attempt {attempt}",
            )
            try:
                made = self.agents.worker.clone()(
                    DECOMPOSE.format(
                        max_children=self.config.max_children,
                        depth=node.depth,
                        max_depth=self.config.max_depth,
                        statement=node.statement,
                        proof=proof.proof,
                        feedback=feedback,
                    ),
                    suppress=True,
                    schema=Decomposition,
                )
            except Stopped as error:
                feedback = f"decomposition worker stopped: {error}"
                self.store.update(node.id, "decomposing", feedback)
                continue
            except Exception as error:  # noqa: BLE001
                feedback = f"decomposition worker failed: {error}"
                self.store.update(node.id, "decomposing", feedback)
                continue
            if made is None:
                feedback = "No valid structured decomposition was returned."
                self.store.update(node.id, "decomposing", feedback)
                continue
            atomic_text(
                self._node_dir(node) / f"decomposition-v{attempt}.json",
                made.model_dump_json(indent=2) + "\n",
            )
            if len(made.subproblems) > self.config.max_children:
                feedback = (
                    f"The split exceeded max_children={self.config.max_children}."
                )
                self.store.update(node.id, "decomposing", feedback)
                continue
            cycle = self._dependency_problem(made.subproblems)
            if cycle:
                feedback = cycle
                self.store.update(node.id, "decomposing", feedback)
                continue
            try:
                audit = self.agents.reviewer.clone()(
                    DECOMPOSITION_AUDIT.format(
                        statement=node.statement,
                        proof=proof.proof,
                        decomposition=made.model_dump_json(indent=2),
                    ),
                    suppress=True,
                    schema=DecompositionAudit,
                )
            except Stopped as error:
                feedback = f"decomposition reviewer stopped: {error}"
                self.store.update(node.id, "decomposing", feedback)
                continue
            except Exception as error:  # noqa: BLE001
                feedback = f"decomposition reviewer failed: {error}"
                self.store.update(node.id, "decomposing", feedback)
                continue
            expected_keys = [one.key for one in made.subproblems]
            audited_keys = [one.key for one in audit.nodes] if audit is not None else []
            if audit is None:
                feedback = "The reviewer returned no decomposition audit."
                self.store.update(node.id, "decomposing", feedback)
                continue
            atomic_text(
                self._node_dir(node) / f"decomposition-audit-v{attempt}.json",
                audit.model_dump_json(indent=2) + "\n",
            )
            if audited_keys != expected_keys:
                feedback = (
                    "The decomposition audit did not cover every child in order: "
                    f"expected {expected_keys}, received {audited_keys}."
                )
                self.store.update(node.id, "decomposing", feedback)
                continue
            if not audit.passed:
                rejected = [
                    f"{one.key}: {one.reason}"
                    for one in audit.nodes
                    if not one.acceptable
                ]
                feedback = "Required decomposition changes: " + "; ".join(
                    [*audit.required_changes, *rejected]
                    or ["reviewer verdict was internally inconsistent"]
                )
                self.store.update(node.id, "decomposing", feedback)
                continue
            return made
        self.store.update(node.id, "decomposing", feedback)
        return None

    def _solve_children(
        self,
        parent: NodeRecord,
        decomposition: Decomposition,
        parent_attempt: int,
    ) -> list[SolveResult]:
        """Activate or reuse theorem workers recursively in dependency order.

        A Lean theorem name is the stable identity of a child below one parent.  Outer
        retries may revise prose or decomposition, but they may not create ``-a2`` copies
        of an already accepted ``-a1`` theorem or send that theorem through proof stages
        again.
        """
        del parent_attempt
        if not decomposition.should_split:
            return []
        with self._graph_lock:
            existing_by_name: dict[str, NodeRecord] = {}
            candidates = sorted(
                (
                    one
                    for one in self.store.nodes.values()
                    if one.lean_name
                    and (one.parent == parent.id or self._accepted_checkpoint(one))
                ),
                key=lambda one: (
                    0
                    if one.status == "proved"
                    else 1
                    if one.status == "integrating" and one.candidate_commit
                    else 2,
                    one.id,
                ),
            )
            for candidate in candidates:
                existing_by_name.setdefault(candidate.lean_name, candidate)
            ids = {
                one.key: (
                    existing_by_name[one.lean_name].id
                    if one.lean_name in existing_by_name
                    else f"{parent.id}.{one.key}-a1"
                )
                for one in decomposition.subproblems
            }
            new_ids = {
                node_id for node_id in ids.values() if node_id not in self.store.nodes
            }
            remaining = self.config.max_nodes - len(self.store.nodes)
            if remaining < len(new_ids):
                return [
                    SolveResult(
                        ok=False,
                        node_id=parent.id,
                        feedback=(
                            f"node bound {self.config.max_nodes} leaves room for {remaining}, "
                            f"but decomposition needs {len(new_ids)} new node(s)"
                        ),
                    )
                ]
            made: dict[str, NodeRecord] = {}
            for one in decomposition.subproblems:
                made[one.key] = self.store.ensure(
                    ids[one.key],
                    parent=parent.id,
                    depth=parent.depth + 1,
                    title=one.title,
                    statement=one.statement,
                    lean_statement=one.lean_statement,
                    lean_name=one.lean_name,
                    depends_on=[ids[key] for key in one.depends_on],
                )
            retained_children = list(dict.fromkeys(ids.values()))
            if parent.children != retained_children:
                parent.children = retained_children
                self.store.render()
        self.store.update(
            parent.id,
            "waiting-children",
            f"activated {len(made)} recursive theorem workers",
        )
        results: dict[str, SolveResult] = {}
        by_key = {one.key: one for one in decomposition.subproblems}
        pending = set(by_key)
        workers = min(self.config.max_parallel_children, max(1, len(pending)))
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"recursive-{slug(parent.id)}",
        ) as executor:
            futures: dict[Any, str] = {}
            while pending or futures:
                progressed = False
                for key in sorted(pending):
                    child = by_key[key]
                    dependency_failure = next(
                        (
                            results[dependency]
                            for dependency in child.depends_on
                            if dependency in results and not results[dependency].ok
                        ),
                        None,
                    )
                    if dependency_failure is not None:
                        result = SolveResult(
                            ok=False,
                            node_id=made[key].id,
                            feedback=f"dependency {dependency_failure.node_id} failed",
                        )
                        self.store.update(made[key].id, "failed", result.feedback)
                        results[key] = result
                        pending.remove(key)
                        progressed = True
                        continue
                    if all(dependency in results for dependency in child.depends_on):
                        pending.remove(key)
                        checkpoint = made[key]
                        if checkpoint.status == "proved":
                            results[key] = SolveResult(
                                ok=True,
                                node_id=checkpoint.id,
                                theorems=self._checkpoint_theorems(checkpoint),
                            )
                        elif (
                            checkpoint.status == "integrating"
                            and checkpoint.candidate_commit
                        ):
                            theorems = self._checkpoint_theorems(checkpoint)
                            if not theorems:
                                result = SolveResult(
                                    ok=False,
                                    node_id=checkpoint.id,
                                    feedback=(
                                        "accepted checkpoint lacks durable reviewer metadata"
                                    ),
                                )
                                results[key] = result
                            else:
                                self._submit_resumed_integration(checkpoint)
                                results[key] = SolveResult(
                                    ok=True,
                                    node_id=checkpoint.id,
                                    theorems=theorems,
                                )
                        else:
                            futures[executor.submit(self._solve, checkpoint)] = key
                        progressed = True
                if not futures:
                    if pending and not progressed:
                        for key in sorted(pending):
                            result = SolveResult(
                                ok=False,
                                node_id=made[key].id,
                                feedback="no dependency-ready node in child DAG",
                            )
                            self.store.update(made[key].id, "failed", result.feedback)
                            results[key] = result
                        pending.clear()
                    continue
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    key = futures.pop(future)
                    try:
                        results[key] = future.result()
                    except Exception as error:  # noqa: BLE001
                        result = SolveResult(
                            ok=False,
                            node_id=made[key].id,
                            feedback=f"parallel child worker failed: {error}",
                        )
                        self.store.update(made[key].id, "failed", result.feedback)
                        results[key] = result
        return [results[one.key] for one in decomposition.subproblems]

    def _resume_existing_dag(self, root: NodeRecord) -> SolveResult:
        """Launch the entire dependency-ready frontier of an existing DAG.

        A resumed run must not descend through one parent at a time.  It snapshots every
        existing descendant, submits all currently ready nodes, and refills the worker pool
        whenever any result unlocks another node. Newly created descendants remain owned by
        the `_solve` call that created them, preventing duplicate scheduling.
        """
        # Follow the durable graph edges, not every historical record whose ``parent``
        # field happens to match.  This keeps obsolete pre-fix ``-a2`` duplicates out of
        # the runnable frontier after their parent has been rewired to the accepted node.
        managed = {root.id}
        frontier = [root.id]
        while frontier:
            node = self.store.nodes[frontier.pop()]
            for related in [*node.children, *node.depends_on]:
                if related in self.store.nodes and related not in managed:
                    managed.add(related)
                    frontier.append(related)
        scheduled: set[str] = set()
        running: dict[Any, str] = {}
        workers = min(self.config.max_parallel_children, max(1, len(managed)))

        if root.status == "integrating" and root.candidate_commit:
            for node_id in sorted(managed - {root.id}):
                node = self.store.nodes[node_id]
                if node.status == "integrating" and node.candidate_commit:
                    self._submit_resumed_integration(node)
            self._wait_for_integrations()
            return self._resume_accepted_candidate(root)

        def ready_nodes() -> list[NodeRecord]:
            ready: list[NodeRecord] = []
            for node_id in sorted(managed):
                if node_id in scheduled:
                    continue
                node = self.store.nodes[node_id]
                if node.status == "proved":
                    scheduled.add(node_id)
                    continue
                if node.status == "integrating" and node.candidate_commit:
                    self._submit_resumed_integration(node)
                    scheduled.add(node_id)
                    continue
                if any(
                    not self._accepted_checkpoint(self.store.nodes[dependency])
                    for dependency in node.depends_on
                ):
                    continue
                if node.children and any(
                    not self._accepted_checkpoint(self.store.nodes[child])
                    for child in node.children
                ):
                    continue
                ready.append(node)
            return ready

        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix=f"frontier-{slug(root.id)}",
        ) as executor:
            while self.store.nodes[root.id].status != "proved":
                for node in ready_nodes():
                    scheduled.add(node.id)
                    self.store.update(
                        node.id,
                        "queued",
                        "dependency-ready; launched in global DAG frontier",
                    )
                    future = executor.submit(
                        self._formalize_checkpoint_parent
                        if node.children
                        else self._solve,
                        node,
                    )
                    running[future] = node.id
                if not running:
                    blocked = [
                        self.store.nodes[node_id]
                        for node_id in sorted(managed)
                        if self.store.nodes[node_id].status != "proved"
                    ]
                    reason = "no dependency-ready node in existing DAG frontier"
                    if blocked:
                        reason += ": " + ", ".join(one.id for one in blocked)
                    return SolveResult(ok=False, node_id=root.id, feedback=reason)
                done, _ = wait(tuple(running), return_when=FIRST_COMPLETED)
                for future in done:
                    node_id = running.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:  # noqa: BLE001
                        result = SolveResult(
                            ok=False,
                            node_id=node_id,
                            feedback=f"global frontier worker failed: {error}",
                        )
                    if not result.ok:
                        return SolveResult(
                            ok=False,
                            node_id=root.id,
                            feedback=f"{node_id}: {result.feedback}",
                        )
        root_record = self.store.nodes[root.id]
        return SolveResult(
            ok=True,
            node_id=root.id,
            theorems=self._checkpoint_theorems(root_record),
        )

    def _formalize_checkpoint_parent(self, node: NodeRecord) -> SolveResult:
        """Finish a resumed parent after all of its existing children are proved."""
        plan = self._recorded_plan(node) or self._preserved_plan(node)
        if plan is None or not node.natural_proof:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback="resumed parent lacks a frozen plan or accepted NL proof",
            )
        natural_path = self.project / node.natural_proof
        try:
            proof = natural_path.read_text(encoding="utf-8").strip()
        except OSError as error:
            return SolveResult(ok=False, node_id=node.id, feedback=str(error))
        natural = NaturalProof(
            proof=proof,
            key_steps=["Use the preserved independently accepted natural proof."],
            unresolved=[],
        )
        children = [
            SolveResult(
                ok=True,
                node_id=child.id,
                theorems=self._checkpoint_theorems(child),
            )
            for child in (self.store.nodes[child_id] for child_id in node.children)
        ]
        while True:
            result = self._formalize(node, plan, natural, children)
            if result.ok:
                return result
            natural = self._accepted_natural_proof(node, plan, result.feedback)
            if natural is None:
                return result

    def _checkpoint_theorems(self, node: NodeRecord) -> list[ProvedTheorem]:
        """Rehydrate enough accepted child metadata for resumed parent formalization."""
        audit = self._latest_lean_audit(node)
        if audit is not None:
            return audit.theorems
        lean_file = (
            node.lean_files[0]
            if node.lean_files
            else getattr(self.config, "lean_target", "") or "Submission.lean"
        )
        statement = node.lean_statement or node.statement
        return [
            ProvedTheorem(
                name=name,
                statement=statement,
                lean_file=lean_file,
                natural_summary=(
                    f"Previously comparator-approved theorem from DAG node {node.id}."
                ),
            )
            for name in node.theorems
        ]

    @staticmethod
    def _accepted_checkpoint(node: NodeRecord) -> bool:
        """Whether a dependency has passed both isolated correctness gates."""
        return node.status == "proved" or (
            node.status == "integrating" and bool(node.candidate_commit)
        )

    def _latest_lean_audit(self, node: NodeRecord) -> LeanAudit | None:
        """Load the durable reviewer approval that created an accepted checkpoint."""
        candidates = sorted(
            self._node_dir(node).glob("lean-audit-v*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for candidate in candidates:
            try:
                audit = LeanAudit.model_validate_json(
                    candidate.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if audit.passed:
                return audit
        return None

    def _resume_accepted_candidate(self, node: NodeRecord) -> SolveResult:
        """Resume only integration for a comparator/reviewer-approved checkpoint."""
        theorems = self._checkpoint_theorems(node)
        if not theorems:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback="accepted checkpoint has no durable reviewer theorem record",
            )
        worktree = Path(node.worktree)
        if not worktree.is_dir():
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=f"accepted proof worktree is unavailable: {worktree}",
            )
        if not node.proof_base_commit or not node.candidate_commit:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback="accepted checkpoint lacks its Git base or candidate commit",
            )
        return self._complete_accepted_integration(
            node,
            worktree,
            node.proof_base_commit,
            node.candidate_commit,
            theorems,
        )

    def _submit_resumed_integration(self, node: NodeRecord) -> Any:
        """Ensure one retained accepted checkpoint has one background promotion."""
        theorems = self._checkpoint_theorems(node)
        worktree = Path(node.worktree)
        with self._integration_futures_lock:
            existing = self._integration_futures.get(node.id)
            if existing is not None:
                return existing
            future = self._integration_executor.submit(
                self._complete_accepted_integration,
                node,
                worktree,
                node.proof_base_commit,
                node.candidate_commit,
                theorems,
            )
            self._integration_futures[node.id] = future
            return future

    def _submit_accepted_integration(
        self,
        node: NodeRecord,
        worktree: Path,
        before: str,
        after: str,
        theorems: list[ProvedTheorem],
        comparator_log: str,
    ) -> Any:
        """Promote an accepted non-root proof while its parent starts immediately."""
        with self._integration_futures_lock:
            existing = self._integration_futures.get(node.id)
            if existing is not None:
                return existing
            future = self._integration_executor.submit(
                self._complete_accepted_integration,
                node,
                worktree,
                before,
                after,
                theorems,
                comparator_log,
            )
            self._integration_futures[node.id] = future
            return future

    def _wait_for_integrations(self) -> None:
        """Wait for every accepted descendant promotion before root acceptance."""
        while True:
            with self._integration_futures_lock:
                futures = list(self._integration_futures.values())
            unfinished = [future for future in futures if not future.done()]
            if not unfinished:
                for future in futures:
                    future.result()
                return
            wait(tuple(unfinished), return_when=FIRST_COMPLETED)

    def _complete_accepted_integration(
        self,
        node: NodeRecord,
        worktree: Path,
        before: str,
        after: str,
        theorems: list[ProvedTheorem],
        comparator_log: str = "",
    ) -> SolveResult:
        """Finish only the integration gate, retaining all accepted proof artifacts."""
        integrated, feedback = self._integrate_reviewed_candidate(
            worktree,
            before,
            after,
            node=node,
            lean_files=node.lean_files,
        )
        if not integrated:  # pragma: no cover - integration retries until success
            return SolveResult(ok=False, node_id=node.id, feedback=feedback)
        integrated_head = self._git_head(self.project)
        self.store.update(
            node.id,
            "integrating",
            feedback,
            candidate_commit=after,
            integrated_commit=integrated_head,
            theorems=[one.name for one in theorems],
        )
        self._publish_checkpoint(node, theorems, comparator_log=comparator_log)
        self.store.update(
            node.id,
            "proved",
            "retained comparator-approved proof; integration gate passed",
            candidate_commit=after,
            integrated_commit=integrated_head,
            theorems=[one.name for one in theorems],
        )
        return SolveResult(ok=True, node_id=node.id, theorems=theorems)

    def _publish_checkpoint(
        self,
        node: NodeRecord,
        theorems: list[ProvedTheorem],
        *,
        comparator_log: str = "",
    ) -> None:
        """Publish an accepted theorem from durable artifacts without reproving it."""
        plan_path = self._recorded_plan(node) or self._preserved_plan(node)
        natural_path = self.project / node.natural_proof if node.natural_proof else None
        try:
            plan = (
                plan_path.read_text(encoding="utf-8") if plan_path else "Unavailable."
            )
        except OSError:
            plan = "Unavailable."
        try:
            natural = (
                natural_path.read_text(encoding="utf-8")
                if natural_path is not None
                else "Unavailable."
            )
        except OSError:
            natural = "Unavailable."
        if not comparator_log:
            logs = sorted(
                self._node_dir(node).glob("comparator-v*.log"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            if logs:
                try:
                    comparator_log = logs[0].read_text(encoding="utf-8")
                except OSError:
                    comparator_log = "Comparator passed; log could not be reloaded."
        for theorem in theorems:
            self.store.publish(
                node,
                theorem,
                plan=plan,
                natural=natural,
                comparator_log=comparator_log or "Comparator passed.",
            )

    def _overlay_accepted_children(
        self, node: NodeRecord, worktree: Path
    ) -> tuple[bool, str]:
        """Put accepted child commits into a parent's speculative proof worktree.

        A child in ``integrating`` has already passed both isolated correctness gates.
        Its immutable candidate history may therefore be used by the parent before the
        serialized canonical-branch promotion finishes.  The parent's own comparator
        and reviewer validate the combined history again.
        """
        commits: list[str] = []
        seen: set[str] = set()
        prerequisite_ids = list(dict.fromkeys([*node.children, *node.depends_on]))
        current = self._git_head(worktree)
        for child_id in prerequisite_ids:
            child = self.store.nodes.get(child_id)
            if child is None or not self._accepted_checkpoint(child):
                continue
            if not child.candidate_commit or not child.proof_base_commit:
                continue
            listed = subprocess.run(
                [
                    "git",
                    "rev-list",
                    "--reverse",
                    f"{child.proof_base_commit}..{child.candidate_commit}",
                ],
                cwd=self.project,
                capture_output=True,
                text=True,
                check=False,
            )
            if listed.returncode:
                return (
                    False,
                    f"could not enumerate accepted child history for {child.id}",
                )
            for commit in listed.stdout.splitlines():
                if not commit or commit in seen:
                    continue
                already_present = (
                    subprocess.run(
                        ["git", "merge-base", "--is-ancestor", commit, current],
                        cwd=worktree,
                        capture_output=True,
                        check=False,
                    ).returncode
                    == 0
                )
                if not already_present:
                    commits.append(commit)
                    seen.add(commit)
        if not commits:
            return True, "all accepted child checkpoints already present"
        if not self._git_clean(worktree):
            return False, "parent worktree is dirty before accepted-child overlay"
        applied, unioned, detail = self._apply_candidate_commits(worktree, commits)
        if not applied:
            return (
                False,
                (
                    "could not overlay accepted child checkpoints without altering "
                    f"them: {detail}"
                ),
            )
        method = "Lean-unioned" if unioned else "cherry-picked"
        return True, f"{method} {len(commits)} accepted child commit(s)"

    def _formalize(
        self,
        node: NodeRecord,
        plan_path: Path,
        natural: NaturalProof,
        children: list[SolveResult],
    ) -> SolveResult:
        """Run RLCR and both reviews in an isolated node worktree, then integrate."""
        natural_path = self.project / node.natural_proof
        child_pages = [
            theorem for child in children if child.ok for theorem in child.theorems
        ]
        child_text = (
            "\n".join(
                f"- `{one.name}` in `{one.lean_file}`: {one.statement}"
                for one in child_pages
            )
            or "- None; this node is atomic."
        )
        accepted_plan = plan_path
        plan_path = self._implementation_plan(
            node,
            accepted_plan=accepted_plan,
            natural_path=natural_path,
            children=child_text,
        )
        try:
            worktree = self._node_worktree(node)
        except RuntimeError as error:
            return SolveResult(ok=False, node_id=node.id, feedback=str(error))
        before = node.proof_base_commit or self._git_head(worktree)
        overlaid, overlay_feedback = self._overlay_accepted_children(node, worktree)
        if not overlaid:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=overlay_feedback,
            )
        # Humanize RLCR otherwise discovers the repository's default branch (usually
        # ``main``) and reviews against that branch.  A recursive proof worktree can be
        # based on a different, frozen history, and accepted child commits may have just
        # been overlaid on top of it.  Anchor RLCR's code review to the exact post-overlay
        # commit so it reviews only this node's new implementation and never reopens
        # frozen child or unrelated default-branch history.
        review_base = self._git_head(worktree)
        self.store.update(
            node.id,
            "rlcr-lean",
            f"isolated humanize1:rlcr formalization in {worktree}",
            worktree=str(worktree),
            proof_branch=self._node_branch(node),
            proof_base_commit=before,
        )
        task = RLCR_LEAN_TASK.format(
            node_id=node.id,
            plan_path=plan_path,
            natural_path=natural_path,
            statement=node.statement,
            lean_statement=node.lean_statement
            or "Root declarations are fixed by Challenge.lean and the official comparator.",
            lean_name=node.lean_name or "choose a descriptive theorem name",
            proof_base_commit=before,
            lean_target=self.config.lean_target
            or "infer the repository's correct target .lean file",
            children=child_text,
            comparator_command=self._review_command(node, []),
            comparator_success=self.config.comparator_success,
        )
        try:
            rlcr_ok, rlcr_log = self._run_rlcr_process(
                node, worktree, plan_path, task, review_base
            )
        except OSError as error:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=f"could not launch isolated humanize1:rlcr: {error}",
            )
        if not rlcr_ok:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=f"isolated humanize1:rlcr failed; see {rlcr_log}",
            )
        after = self._git_head(worktree)
        if not self._git_clean(worktree):
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=f"RLCR left uncommitted participant changes in {worktree}",
            )
        lean_files = self._lean_files(before, after, worktree)
        if not lean_files:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback="RLCR completed without an identifiable Lean target",
            )
        self.store.update(
            node.id,
            "comparing",
            f"running independent machine comparator in {worktree}",
            lean_files=lean_files,
        )
        passed, log_path, log = self._compare(node, lean_files, worktree)
        if not passed:
            self.store.update(
                node.id,
                "natural-proof",
                "comparator rejected the theorem; revise latest NL proof",
            )
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=f"Comparator failed; see {log_path.relative_to(self.project)}",
            )
        self.store.update(
            node.id,
            "lean-review",
            f"fresh reviewer reruns comparator in {worktree}",
        )
        audit = _WorkspaceAgent(self.agents.reviewer.clone(), worktree)(
            LEAN_AUDIT.format(
                node_id=node.id,
                statement=node.statement,
                lean_statement=node.lean_statement
                or "Root declarations are fixed by Challenge.lean and the official comparator.",
                proof_base_commit=before,
                lean_files="\n".join(f"- {one}" for one in lean_files),
                comparator_command=self._review_command(node, lean_files),
                comparator_success=self.config.comparator_success,
                comparator_log=log[-12000:],
            ),
            suppress=True,
            schema=LeanAudit,
        )
        if audit is not None:
            audit_version = self._next_json_version(node, "lean-audit")
            atomic_text(
                self._node_dir(node) / f"lean-audit-v{audit_version}.json",
                audit.model_dump_json(indent=2) + "\n",
            )
        if audit is None or not audit.passed:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=self._lean_feedback(audit),
            )
        self.store.update(
            node.id,
            "integrating",
            f"all isolated gates passed; integrating commit {after[:12]}",
            candidate_commit=after,
            theorems=[one.name for one in audit.theorems],
        )
        self._publish_checkpoint(node, audit.theorems, comparator_log=log)
        if node.parent is not None:
            self._submit_accepted_integration(
                node,
                worktree,
                before,
                after,
                audit.theorems,
                log,
            )
            return SolveResult(ok=True, node_id=node.id, theorems=audit.theorems)
        self._wait_for_integrations()
        return self._complete_accepted_integration(
            node,
            worktree,
            before,
            after,
            audit.theorems,
            log,
        )

    def _revise_parent(self, child: NodeRecord, failure: str) -> None:
        """Route an incorrect child theorem into the parent's NL-proof loop."""
        if child.parent is None:
            return
        # Several siblings may fail in one parallel wave. Preserve concrete feedback while
        # leaving the accepted scaffold plan immutable.
        with self._revision_lock:
            parent = self.store.nodes[child.parent]
            self.store.update(
                parent.id,
                "waiting-children",
                f"revise latest natural proof after {child.id} failed: {failure}",
            )

    def _compare(
        self,
        node: NodeRecord,
        lean_files: list[str],
        cwd: Path | None = None,
        *,
        label: str = "",
    ) -> tuple[bool, Path, str]:
        """Run the comparator without a shell and require both exit zero and its marker."""
        rendered = self._render_command(node, lean_files)
        argv = shlex.split(rendered)
        environment = os.environ.copy()
        environment.update(
            HUMANIZE_NODE_ID=node.id,
            HUMANIZE_NODE_STATEMENT=node.statement,
            HUMANIZE_LEAN_FILES=os.pathsep.join(lean_files),
            HUMANIZE_RUN_DIR=str(self.run_root),
            HUMANIZE_WIKI_DIR=str(self.store.wiki),
        )
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd or self.project,
                env=environment,
                capture_output=True,
                text=True,
                timeout=self.config.comparator_timeout,
                check=False,
            )
            log = (
                f"command: {rendered}\nexit: {completed.returncode}\n\n"
                f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}\n"
            )
            passed = (
                completed.returncode == 0
                and self.config.comparator_success
                in completed.stdout + completed.stderr
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            log = f"command: {rendered}\ncomparator execution failed: {error}\n"
            passed = False
        suffix = f"-{slug(label)}" if label else ""
        path = self._node_dir(node) / f"comparator-v{node.attempts}{suffix}.log"
        atomic_text(path, log)
        return passed, path, log

    def _lean_files(
        self, before: str, after: str, cwd: Path | None = None
    ) -> list[str]:
        """Identify Lean files changed by this node, plus an explicitly configured target."""
        workspace = cwd or self.project
        found: set[str] = set()
        if before and after:
            completed = subprocess.run(
                ["git", "diff", "--name-only", f"{before}..{after}", "--", "*.lean"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode == 0:
                found.update(
                    one.strip() for one in completed.stdout.splitlines() if one.strip()
                )
        if self.config.lean_target and (workspace / self.config.lean_target).is_file():
            found.add(self.config.lean_target)
        return sorted(found)

    def _review_command(self, node: NodeRecord, lean_files: list[str]) -> str:
        """Render the comparator with explicit controller paths for isolated worktrees."""
        environment = (
            f"HUMANIZE_RUN_DIR={shlex.quote(str(self.run_root))} "
            f"HUMANIZE_WIKI_DIR={shlex.quote(str(self.store.wiki))}"
        )
        return f"env {environment} {self._render_command(node, lean_files)}"

    def _run_rlcr_process(
        self,
        node: NodeRecord,
        worktree: Path,
        plan_path: Path,
        task: str,
        review_base: str,
    ) -> tuple[bool, Path]:
        """Run official RLCR in a process whose real cwd is the node worktree.

        Humanize's RLCR intentionally derives its Git root from ``Path.cwd()``. Changing
        Python's cwd in a worker thread would race every other leaf, so process isolation is
        required in addition to binding the Codex sessions to the worktree.
        """
        node_dir = self._node_dir(node)
        config_path = node_dir / f"rlcr-config-v{node.attempts}.json"
        atomic_text(
            config_path,
            json.dumps(
                {
                    "plan_file": str(plan_path),
                    "max": self.config.rlcr_rounds,
                    "base_branch": review_base,
                    "track_plan_file": False,
                    "push_every_round": False,
                    "skip_impl": False,
                    "skip_quiz": True,
                    "privacy": True,
                    "agent_teams": False,
                    "claude_answer_codex": True,
                },
                indent=2,
            )
            + "\n",
        )
        executable = shutil.which("hmz")
        if executable is None:
            raise OSError("hmz executable not found")
        command = [
            executable,
            "exec",
            "-f",
            WORKTREE_RLCR,
            "-c",
            str(config_path),
            "-a",
            self._agent_spec(self.agents.worker),
            "-a",
            self._agent_spec(self.agents.reviewer),
            task,
        ]
        log_path = node_dir / f"rlcr-process-v{node.attempts}.log"
        with log_path.open("w", encoding="utf-8") as output:
            completed = subprocess.run(
                command,
                cwd=worktree,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        return completed.returncode == 0, log_path

    @staticmethod
    def _agent_spec(agent: Any) -> str:
        """Serialize a parent Humanize agent for an isolated ``hmz exec`` child."""
        config = agent.config
        fields = [
            f"cli={agent.backend}",
            f"model={config.model}",
            f"effort={config.effort}",
            f"service_tier={config.service_tier}",
            f"permission={config.permission}",
            f"web_search={'on' if config.web_search else 'off'}",
        ]
        if config.provider:
            fields.append(f"provider={config.provider}")
        fields.extend(
            f"config.{key}={value}" for key, value in getattr(config, "overrides", ())
        )
        return ",".join(fields)

    def _node_worktree(self, node: NodeRecord) -> Path:
        """Create or reuse a durable Git branch and worktree for one node attempt."""
        recorded = Path(node.worktree) if node.worktree else None
        recorded_valid = (
            recorded is not None and self._git_toplevel(recorded) == recorded
        )
        if recorded_valid and len(str(recorded)) <= 180:
            self._prepare_lake_workspace(recorded)
            return recorded
        path = self._node_worktree_path(node)
        if recorded_valid:
            if self._git_toplevel(path) == path:
                node.worktree = str(path)
                self._prepare_lake_workspace(path)
                return path
            if path.exists() and any(path.iterdir()):
                raise RuntimeError(
                    f"short node worktree path exists but is not a Git worktree: {path}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._integration_lock:
                moved = subprocess.run(
                    ["git", "worktree", "move", str(recorded), str(path)],
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if moved.returncode:
                detail = (moved.stderr or moved.stdout).strip()
                raise RuntimeError(f"could not shorten node worktree path: {detail}")
            node.worktree = str(path)
            self._prepare_lake_workspace(path)
            return path
        if self._git_toplevel(path) == path:
            node.worktree = str(path)
            self._prepare_lake_workspace(path)
            return path
        if path.exists() and any(path.iterdir()):
            raise RuntimeError(
                f"node worktree path exists but is not a Git worktree: {path}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = self._node_branch(node)
        detail = "unknown Git error"
        for retry in range(6):
            with self._integration_lock:
                if self._git_toplevel(path) == path:
                    break
                # A disappeared /tmp checkout can leave prunable worktree metadata that
                # still claims its proof branch. Pruning removes only that stale checkout
                # record; the named proof branch and every commit remain durable.
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                exists = (
                    subprocess.run(
                        [
                            "git",
                            "show-ref",
                            "--verify",
                            "--quiet",
                            f"refs/heads/{branch}",
                        ],
                        cwd=self.project,
                        capture_output=True,
                        text=True,
                        check=False,
                    ).returncode
                    == 0
                )
                arguments = (
                    ["git", "worktree", "add", str(path), branch]
                    if exists
                    else [
                        "git",
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(path),
                        "HEAD",
                    ]
                )
                completed = subprocess.run(
                    arguments,
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            if completed.returncode == 0:
                break
            detail = (completed.stderr or completed.stdout).strip()
            # Twelve problem supervisors can share one underlying Git repository. Git's
            # own ref/worktree locks are authoritative; retry their brief contention.
            time.sleep(0.2 * (retry + 1))
        if self._git_toplevel(path) != path:
            raise RuntimeError(f"could not create isolated node worktree: {detail}")
        node.worktree = str(path)
        node.proof_branch = branch
        # HEAD may advance while this worker waits for the integration lock.  Record the
        # commit the new worktree actually checked out, not a pre-lock snapshot of the
        # moving problem branch.
        node.proof_base_commit = self._git_head(path)
        self._prepare_lake_workspace(path)
        return path

    def _node_worktree_path(self, node: NodeRecord) -> Path:
        """Choose a stable checkout path short enough for Humanize's epic key."""
        descriptive = (
            self.project.parent
            / ".recursive-lean-node-worktrees"
            / self.run_root.name
            / slug(node.id)
            / f"attempt-{max(node.attempts, 1)}"
            / self.project.name
        )
        if len(str(descriptive)) <= 180:
            return descriptive
        identity = "\0".join(
            (
                str(self.project),
                self.run_root.name,
                node.id,
                str(max(node.attempts, 1)),
            )
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
        return (
            Path(tempfile.gettempdir())
            / "humanize-lean-worktrees"
            / digest
            / self.project.name
        )

    def _prepare_lake_workspace(self, path: Path) -> None:
        """Provision ignored pinned Lake inputs in an isolated worktree.

        Lake worktrees do not receive ignored files.  Sharing the immutable package
        checkout avoids a network fetch, while copying the pinned manifest prevents
        Lake from trying to update dependency repositories through read-only shared
        Git metadata.  A copy is intentional: a worker must never rewrite the source
        manifest in another checkout.
        """
        packages = self.project / ".lake" / "packages"
        linked = path / ".lake" / "packages"
        packages_ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", ".lake/packages"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            packages.is_dir()
            and not linked.exists()
            and packages_ignored.returncode == 0
        ):
            linked.parent.mkdir(parents=True, exist_ok=True)
            linked.symlink_to(packages, target_is_directory=True)

        manifest = path / "lake-manifest.json"
        manifest_ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", "lake-manifest.json"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
        )
        if manifest.exists() or manifest_ignored.returncode != 0:
            return

        sources = [self.project / "lake-manifest.json"]
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=False,
        )
        if common.returncode == 0 and common.stdout.strip():
            sources.append(Path(common.stdout.strip()).parent / "lake-manifest.json")
        for source in sources:
            if source.is_file() and source.resolve() != manifest.resolve():
                shutil.copy2(source, manifest)
                return

    def _node_branch(self, node: NodeRecord) -> str:
        """Return the stable Git branch name retaining one node attempt's proof."""
        if node.proof_branch:
            return node.proof_branch
        return (
            "humanize-recursive/"
            f"{slug(self.project.name)}/{slug(self.run_root.name)}/"
            f"{slug(node.id)}-a{max(node.attempts, 1)}"
        )

    def _integrate_reviewed_candidate(
        self,
        worktree: Path,
        before: str,
        after: str,
        *,
        node: NodeRecord,
        lean_files: list[str],
    ) -> tuple[bool, str]:
        """Keep an accepted candidate in integration until its latest-base merge passes.

        Returning a comparator- and reviewer-approved theorem to natural-language proof would
        discard the wrong checkpoint: an integration failure concerns composition with a moving
        sibling history, not the theorem's accepted mathematics.  Retry only this promotion gate,
        retaining the candidate branch and all earlier approvals.
        """
        retry = 0
        while True:
            integrated, feedback = self._integrate_candidate(
                worktree,
                before,
                after,
                node=node,
                lean_files=lean_files,
            )
            if integrated:
                return True, feedback
            retry += 1
            self.store.update(
                node.id,
                "integrating",
                (
                    "accepted proof retained; integration-only retry "
                    f"{retry} after: {feedback}"
                ),
                candidate_commit=after,
            )
            # Infrastructure or Git-lock failures may resolve without a source repair.  Keep the
            # retry bounded enough to remain observable while avoiding a hot failure loop.
            time.sleep(min(60.0, float(retry)))

    def _integrate_candidate(
        self,
        worktree: Path,
        before: str,
        after: str,
        *,
        node: NodeRecord | None = None,
        lean_files: list[str] | None = None,
    ) -> tuple[bool, str]:
        """Integrate one reviewed history, reconciling parallel sibling bases safely."""
        if not after:
            return False, f"isolated worktree has no Git HEAD: {worktree}"
        if before == after:
            return True, "the reviewed theorem was already present at the worktree base"
        listed = subprocess.run(
            ["git", "rev-list", "--reverse", f"{before}..{after}"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        commits = [one for one in listed.stdout.splitlines() if one]
        if listed.returncode or not commits:
            return False, f"could not enumerate reviewed commits {before}..{after}"
        with self._integration_lock:
            if not self._git_clean(self.project):
                return False, "problem integration worktree is not clean"
            canonical = self._git_head(self.project)
            # A long-running RLCR may fast-forward or rebase its proof branch onto the
            # moving problem branch before it writes the theorem commit.  Its persisted
            # `before` value then predates commits which are already in `canonical`.
            # Do not cherry-pick those ancestors back onto themselves: Git reports that
            # as an empty cherry-pick with no unmerged paths.
            commits = [
                commit
                for commit in commits
                if subprocess.run(
                    ["git", "merge-base", "--is-ancestor", commit, canonical],
                    cwd=self.project,
                    capture_output=True,
                    check=False,
                ).returncode
                != 0
            ]
            if not commits:
                return (
                    True,
                    "all reviewed commits were already present in the problem branch",
                )
            if canonical == before:
                merged = subprocess.run(
                    ["git", "merge", "--ff-only", after],
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if merged.returncode:
                    detail = (merged.stderr or merged.stdout).strip()
                    return (
                        False,
                        f"could not fast-forward reviewed node history: {detail}",
                    )
                return True, f"fast-forwarded {len(commits)} reviewed commit(s)"

            scratch_parent = (
                self.project.parent / ".recursive-lean-integration-worktrees"
            )
            scratch_parent.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f"{slug(node.id) if node else 'node'}-",
                    dir=scratch_parent,
                )
            )
            integration = temporary / self.project.name
            added = subprocess.run(
                ["git", "worktree", "add", "--detach", str(integration), canonical],
                cwd=self.project,
                capture_output=True,
                text=True,
                check=False,
            )
            if added.returncode:
                detail = (added.stderr or added.stdout).strip()
                try:
                    temporary.rmdir()
                except OSError:
                    pass
                return False, f"could not create integration recheck worktree: {detail}"
            try:
                self._prepare_lake_workspace(integration)
                applied, unioned, detail = self._apply_candidate_commits(
                    integration, commits
                )
                agent_repaired = False
                if not applied:
                    repaired, detail = self._repair_integration(
                        integration,
                        canonical=canonical,
                        commits=commits,
                        node=node,
                        lean_files=lean_files or [],
                        failure=detail,
                    )
                    if not repaired:
                        return False, detail
                    agent_repaired = True
                if node is not None and not agent_repaired:
                    passed, log_path, log = self._compare(
                        node,
                        lean_files or [],
                        integration,
                        label="integration",
                    )
                    if not passed:
                        repaired, detail = self._repair_integration(
                            integration,
                            canonical=canonical,
                            commits=commits,
                            node=node,
                            lean_files=lean_files or [],
                            failure=(
                                "combined parallel history failed its integration comparator; "
                                f"see {log_path.relative_to(self.project)}\n\n"
                                f"{log[-12000:]}"
                            ),
                        )
                        if not repaired:
                            return False, detail
                        agent_repaired = True
                integration_head = self._git_head(integration)
                merged = subprocess.run(
                    ["git", "merge", "--ff-only", integration_head],
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if merged.returncode:
                    detail = (merged.stderr or merged.stdout).strip()
                    return (
                        False,
                        f"could not fast-forward reconciled node history: {detail}",
                    )
                method = (
                    "agent-reconciled"
                    if agent_repaired
                    else "union-reconciled"
                    if unioned
                    else "rebased"
                )
                return (
                    True,
                    f"{method} and integrated {len(commits)} reviewed commit(s)",
                )
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(integration)],
                    cwd=self.project,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                try:
                    temporary.rmdir()
                except OSError:
                    pass

    def _repair_integration(
        self,
        integration: Path,
        *,
        canonical: str,
        commits: list[str],
        node: NodeRecord | None,
        lean_files: list[str],
        failure: str,
    ) -> tuple[bool, str]:
        """Repair only composition of histories whose isolated proof gates passed.

        The repair loop deliberately remains inside the serialized integration worktree.  It
        never calls the mathematical planner, natural-language author, decomposition stage, or
        node RLCR prover.  Every source repair receives a new machine comparator run and a fresh
        independent reviewer comparator run before it can advance the canonical branch.
        """
        if node is None or self.agents is None:
            return False, failure
        feedback = failure
        round_number = 0
        while True:
            round_number += 1
            self.store.update(
                node.id,
                "integrating",
                (
                    "accepted proof retained; repairing combined history, round "
                    f"{round_number}: {feedback.splitlines()[0]}"
                ),
            )
            prompt = INTEGRATION_REPAIR.format(
                node_id=node.id,
                statement=node.statement,
                lean_statement=node.lean_statement
                or "Root declarations are fixed by Challenge.lean and the official comparator.",
                candidate_commits="\n".join(f"- `{one}`" for one in commits),
                failure=feedback[-16000:],
                comparator_command=self._review_command(node, lean_files),
                comparator_success=self.config.comparator_success,
            )
            try:
                _WorkspaceAgent(self.agents.worker.clone(), integration)(
                    prompt,
                    suppress=True,
                )
            except Exception as error:  # noqa: BLE001
                feedback = f"integration repair worker failed: {error}"
                continue
            if not self._git_clean(integration):
                feedback = (
                    "integration repair left uncommitted changes; preserve them, finish the "
                    "repair, and commit a clean candidate"
                )
                continue
            integration_head = self._git_head(integration)
            combined_files = sorted(
                set(lean_files)
                | set(self._lean_files(canonical, integration_head, integration))
            )
            passed, log_path, log = self._compare(
                node,
                combined_files,
                integration,
                label=f"integration-repair-{round_number}",
            )
            if not passed:
                feedback = (
                    "repaired combined history still failed its comparator; see "
                    f"{log_path.relative_to(self.project)}\n\n{log[-12000:]}"
                )
                continue
            audit = _WorkspaceAgent(self.agents.reviewer.clone(), integration)(
                INTEGRATION_AUDIT.format(
                    node_id=node.id,
                    statement=node.statement,
                    lean_statement=node.lean_statement
                    or (
                        "Root declarations are fixed by Challenge.lean and the official "
                        "comparator."
                    ),
                    lean_files="\n".join(f"- {one}" for one in combined_files),
                    comparator_command=self._review_command(node, combined_files),
                    comparator_success=self.config.comparator_success,
                    comparator_log=log[-12000:],
                ),
                suppress=True,
                schema=LeanAudit,
            )
            if audit is not None:
                audit_version = self._next_json_version(node, "integration-lean-audit")
                atomic_text(
                    self._node_dir(node)
                    / f"integration-lean-audit-v{audit_version}.json",
                    audit.model_dump_json(indent=2) + "\n",
                )
            if audit is None or not audit.passed:
                feedback = self._lean_feedback(audit)
                continue
            if not self._git_clean(integration):
                feedback = "integration reviewer modified the reviewed worktree"
                continue
            if self._git_head(integration) != integration_head:
                feedback = "integration reviewer changed the reviewed Git history"
                continue
            return (
                True,
                (
                    "integration repair passed machine comparator and fresh reviewer "
                    f"comparator in round {round_number}"
                ),
            )

    def _apply_candidate_commits(
        self, integration: Path, commits: list[str]
    ) -> tuple[bool, bool, str]:
        """Cherry-pick reviewed commits, unioning only ordinary tracked Lean conflicts."""
        unioned = False
        for commit in commits:
            picked = subprocess.run(
                [*INTEGRATION_GIT, "cherry-pick", commit],
                cwd=integration,
                capture_output=True,
                text=True,
                check=False,
            )
            if picked.returncode == 0:
                continue
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=integration,
                capture_output=True,
                text=True,
                check=False,
            )
            if status.returncode == 0 and not status.stdout.strip():
                # The same patch can already exist under a different integration commit
                # hash.  An empty cherry-pick is success: skip its sequencer entry and
                # continue with any later, genuinely new theorem commits.
                skipped = subprocess.run(
                    ["git", "cherry-pick", "--skip"],
                    cwd=integration,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if skipped.returncode == 0:
                    continue
            resolved, detail = self._union_lean_conflicts(integration)
            if not resolved:
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=integration,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return (
                    False,
                    unioned,
                    (
                        f"reviewed node commit {commit[:12]} could not be reconciled: {detail}"
                    ),
                )
            unioned = True
            continued = subprocess.run(
                [
                    *INTEGRATION_GIT,
                    "-c",
                    "core.editor=true",
                    "cherry-pick",
                    "--continue",
                ],
                cwd=integration,
                capture_output=True,
                text=True,
                check=False,
            )
            if continued.returncode:
                detail = (continued.stderr or continued.stdout).strip()
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=integration,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if status.returncode == 0 and not status.stdout.strip():
                    # Union conflict resolution can discover that the canonical branch
                    # already contains the candidate's complete Lean result.  Git then
                    # keeps the sequencer active but rejects --continue as an empty
                    # commit.  This is the same successful duplicate-patch case handled
                    # above, reached only after resolving an ordinary Lean conflict.
                    skipped = subprocess.run(
                        ["git", "cherry-pick", "--skip"],
                        cwd=integration,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if skipped.returncode == 0:
                        continue
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=integration,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return (
                    False,
                    unioned,
                    f"could not commit reconciled Lean sources: {detail}",
                )
        return True, unioned, "candidate commits applied"

    @staticmethod
    def _union_lean_conflicts(integration: Path) -> tuple[bool, str]:
        """Preserve both sides of same-file Lean additions for comparator rechecking."""
        unmerged = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U", "-z"],
            cwd=integration,
            capture_output=True,
            check=False,
        )
        paths = [one.decode("utf-8") for one in unmerged.stdout.split(b"\0") if one]
        if unmerged.returncode or not paths:
            return False, "Git reported no resolvable unmerged paths"
        if any(not path.endswith(".lean") for path in paths):
            return False, f"non-Lean conflict requires a new proof attempt: {paths}"
        for relative in paths:
            stages: list[bytes] = []
            for stage in (2, 1, 3):
                shown = subprocess.run(
                    ["git", "show", f":{stage}:{relative}"],
                    cwd=integration,
                    capture_output=True,
                    check=False,
                )
                if shown.returncode:
                    return False, f"cannot read merge stage {stage} for {relative}"
                stages.append(shown.stdout)
            with tempfile.TemporaryDirectory(prefix="humanize-lean-union-") as held:
                files = [Path(held) / name for name in ("ours", "base", "theirs")]
                for path, content in zip(files, stages, strict=True):
                    path.write_bytes(content)
                merged = subprocess.run(
                    [
                        "git",
                        "merge-file",
                        "--union",
                        "-p",
                        str(files[0]),
                        str(files[1]),
                        str(files[2]),
                    ],
                    capture_output=True,
                    check=False,
                )
            if merged.returncode < 0 or merged.returncode > 127:
                return False, f"text union failed for {relative}"
            target = (integration / relative).resolve()
            if not target.is_relative_to(integration.resolve()):
                return False, f"unsafe conflicted path: {relative}"
            target.write_bytes(merged.stdout)
            staged = subprocess.run(
                ["git", "add", "--", relative],
                cwd=integration,
                capture_output=True,
                text=True,
                check=False,
            )
            if staged.returncode:
                return False, f"could not stage reconciled Lean source {relative}"
        return True, f"unioned {len(paths)} Lean source conflict(s)"

    @staticmethod
    def _git_toplevel(cwd: Path) -> Path | None:
        if not cwd.is_dir():
            return None
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return (
            Path(completed.stdout.strip()).resolve()
            if not completed.returncode
            else None
        )

    @staticmethod
    def _git_clean(cwd: Path) -> bool:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and not completed.stdout.strip()

    def _render_command(self, node: NodeRecord, lean_files: list[str]) -> str:
        """Fill documented comparator placeholders while refusing unknown ones."""
        values = {
            "node_id": node.id,
            "node_dir": str(self._node_dir(node).relative_to(self.project)),
            "run_dir": str(self.run_root.relative_to(self.project)),
            "wiki_dir": self.config.wiki_dir,
            "lean_target": self.config.lean_target,
            "lean_files": os.pathsep.join(lean_files),
        }
        try:
            return self.config.comparator_command.format_map(values)
        except KeyError as error:
            raise ValueError(
                f"unknown comparator command placeholder: {error.args[0]}"
            ) from error

    def _require_git(self) -> None:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            completed.returncode
            or Path(completed.stdout.strip()).resolve() != self.project
        ):
            raise ValueError("run this flow at the root of a clean Lean git repository")

    def _require_comparator(self) -> None:
        root = self.store.nodes.get("root") or NodeRecord(
            id="root", title="Main theorem", statement=self.task
        )
        argv = shlex.split(self._render_command(root, []))
        if not argv:
            raise ValueError("comparator_command is empty")
        executable = argv[0]
        if "/" in executable:
            present = (self.project / executable).is_file()
        else:
            present = shutil.which(executable) is not None
        if not present:
            raise ValueError(f"comparator executable not found: {executable}")
        if len(argv) > 1 and executable in {"bash", "sh"}:
            script = self.project / argv[1]
            if not script.is_file():
                raise ValueError(f"comparator script not found: {argv[1]}")

    def _run_root(self) -> Path:
        digest = self._task_digest()
        previous = self.state.get("run_dir")
        if (
            self.state.get("version") == 1
            and self.state.get("task_digest") == digest
            and isinstance(previous, str)
            and (self.project / previous).is_dir()
        ):
            return (self.project / previous).resolve()
        stamp = now().replace(":", "").replace("-", "")
        return (
            self.project / self.config.artifact_dir / "runs" / f"{stamp}-{digest[:10]}"
        )

    def _node_dir(self, node: NodeRecord) -> Path:
        path = self.run_root / "nodes" / slug(node.id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _implementation_plan(
        self,
        node: NodeRecord,
        *,
        accepted_plan: Path,
        natural_path: Path,
        children: str,
    ) -> Path:
        """Give nested RLCR only the work it can finish before returning control."""
        path = self._node_dir(node) / f"rlcr-plan-v{node.attempts}.md"
        content = f"""# Implement Lean DAG node `{node.id}`

## Authoritative selected-node contract

- Accepted natural proof: `{natural_path}`
- Declaration name: `{node.lean_name}`
- Frozen child type: `{node.lean_statement or "official root Challenge declarations"}`
- Lean target: `{self.config.lean_target or "infer the repository target"}`

Comparator-approved dependencies:

{children}

The current node identity, frozen type, and dependency list above were produced by the
controller's completed natural-proof and decomposition gates. They are the only operational
proof boundary for this nested invocation. The one-time scaffold at `{accepted_plan}` may be
read for mathematical background, but any speculative decomposition, interface inventory,
source placement, or selected-cone shape in that older artifact is historical. It must not
override the current DAG, trigger planning, add or replace child nodes, or invalidate an exact
comparator-approved implementation of this selected node.

## Nested RLCR tasks

1. Read the accepted natural proof and, when useful, the scaffold's mathematical route.
   Implement only this node's exact declaration, using only the comparator-approved dependencies
   listed above. Do not revise the scaffold/proof or reopen decomposition.
2. Run warning-fatal Lean builds and inspect the complete source diff for placeholders,
   weakened statements, new axioms, unsafe mechanisms, or protected-file changes.
3. Commit the candidate and require a clean worktree at that exact SHA.
4. Run `{self._review_command(node, [])}` and require exit zero plus
   `{self.config.comparator_success}`.
5. For a non-root node, run only that exact node comparator. Do not run the official root or
   whole-benchmark comparator and do not validate unrelated parent or sibling theorems.
6. Return control to the recursive controller immediately.

The implementation reviewer may request another round only for a defect in this exact selected
node: a frozen-statement mismatch, invalid Lean proof, source-safety or protected-file violation,
unclean/uncommitted candidate, or failed configured comparator. It must not request a different
DAG shape, extra certification interface, source-layout refactor, or plan revision solely because
an older scaffold proposed one.

## Completion boundary

The fresh reviewer comparator rerun, theorem-wiki publication, and DAG `proved` transition are
outer-controller tasks. They cannot run until this nested RLCR invocation returns, and they are
not blockers for completion of this implementation-only plan.
"""
        atomic_text(path, content)
        return path

    def _task_digest(self) -> str:
        return hashlib.sha256(self.task.encode()).hexdigest()

    def _root_lean_name(self) -> str:
        if self.config.lean_target:
            return Path(self.config.lean_target).stem
        return "main_theorem"

    def _git_head(self, cwd: Path | None = None) -> str:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd or self.project,
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""

    def _next_version(self, node: NodeRecord, prefix: str) -> int:
        existing = self._node_dir(node).glob(f"{prefix}-v*.md")
        return sum(1 for _ in existing) + 1

    def _next_json_version(self, node: NodeRecord, prefix: str) -> int:
        """Allocate a durable version across outer node retries."""
        existing = self._node_dir(node).glob(f"{prefix}-v*.json")
        return sum(1 for _ in existing) + 1

    def _latest_natural_checkpoint(self, node: NodeRecord) -> tuple[str, str]:
        """Return the latest proof draft and its exact rejection feedback."""
        candidates = sorted(
            self._node_dir(node).glob("natural-proof-draft-v*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if not candidates:
            fallback_candidates: list[Path] = []
            if node.natural_proof:
                fallback_candidates.append(self.project / node.natural_proof)
            fallback_candidates.extend(
                sorted(
                    self._node_dir(node).glob("natural-proof-v*.md"),
                    key=lambda path: path.stat().st_mtime_ns,
                    reverse=True,
                )
            )
            fallback_candidates.append(self.project / "NATURAL_LANGUAGE_PROOF.md")
            for fallback in fallback_candidates:
                try:
                    proof = fallback.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if proof:
                    return (
                        proof,
                        node.message
                        or "Continue from this latest preserved root proof draft.",
                    )
            return "No earlier draft is available.", "None."
        latest = candidates[0]
        try:
            proof = NaturalProof.model_validate_json(
                latest.read_text(encoding="utf-8")
            ).proof
        except (OSError, ValueError):
            return "No readable earlier draft is available.", "None."
        version = latest.stem.rsplit("v", 1)[-1]
        feedback_path = self._node_dir(node) / f"natural-feedback-v{version}.txt"
        try:
            feedback = feedback_path.read_text(encoding="utf-8").strip()
        except OSError:
            feedback = "Continue from this latest preserved draft."
        return proof, feedback or "Continue from this latest preserved draft."

    def _preserved_plan(self, node: NodeRecord) -> Path | None:
        """Return the best existing immutable scaffold after an interrupted run.

        ``humanize1:gen-plan`` creates the public output from a blank template and writes
        substantive content through a hidden atomic temporary file.  A stopped flow can
        therefore leave a placeholder ``plan-vN.md`` beside a useful temporary output.  If
        neither finalized nor temporary output is usable, the concrete controller input
        draft is still frozen as the scaffold so planning is never regenerated or reviewed.
        """
        node_dir = self._node_dir(node)
        tiers = (
            node_dir.glob("plan-v*.md"),
            node_dir.glob(".humanize-plan-*.tmp"),
            node_dir.glob("plan-draft-v*.md"),
        )
        for tier in tiers:
            candidates = sorted(
                tier,
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            for candidate in candidates:
                try:
                    text = candidate.read_text(encoding="utf-8")
                except OSError:
                    continue
                if text.strip() and not text.lstrip().startswith("# <Plan Title>"):
                    return candidate
        return None

    def _recorded_plan(self, node: NodeRecord) -> Path | None:
        """Return the node's already accepted plan without regenerating it."""
        if not node.plan:
            return None
        candidate = self.project / node.plan
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.strip() or text.lstrip().startswith("# <Plan Title>"):
            return None
        return candidate

    @staticmethod
    def _natural_feedback(audit: NaturalAudit | None) -> str:
        if audit is None:
            return "The reviewer returned no structured natural-proof audit."
        return (
            "; ".join([audit.first_invalid_step, *audit.required_changes]).strip("; ")
            or "The reviewer rejected the proof without actionable details."
        )

    @staticmethod
    def _lean_feedback(audit: LeanAudit | None) -> str:
        if audit is None:
            return "The Lean reviewer returned no structured audit."
        failed: list[str] = []
        if not audit.comparator_reran:
            failed.append("reviewer did not rerun comparator")
        if not audit.comparator_passed:
            failed.append("reviewer's comparator rerun failed")
        if not audit.proof_matches_statement:
            failed.append("Lean proof does not preserve the statement")
        if not audit.theorems:
            failed.append("reviewer listed no proved theorem for the wiki")
        return (
            "; ".join([*failed, *audit.issues]) or "Lean reviewer rejected the proof."
        )

    @staticmethod
    def _dependency_problem(subproblems: list[Subproblem]) -> str:
        keys = [one.key for one in subproblems]
        if len(keys) != len(set(keys)):
            return "subproblem keys are not unique"
        names = [one.lean_name for one in subproblems]
        if len(names) != len(set(names)):
            return "subproblem Lean names are not unique"
        known = set(keys)
        for one in subproblems:
            unknown = sorted(set(one.depends_on) - known)
            if unknown:
                return f"subproblem {one.key} has unknown dependencies: {unknown}"
            if one.key in one.depends_on:
                return f"subproblem {one.key} depends on itself"
        try:
            Runtime._topological(subproblems)
        except ValueError as error:
            return str(error)
        return ""

    @staticmethod
    def _topological(subproblems: Iterable[Subproblem]) -> list[str]:
        items = {one.key: set(one.depends_on) for one in subproblems}
        ordered: list[str] = []
        while items:
            ready = sorted(
                key for key, dependencies in items.items() if not dependencies
            )
            if not ready:
                raise ValueError("subproblem dependency graph contains a cycle")
            ordered.extend(ready)
            for key in ready:
                del items[key]
            for dependencies in items.values():
                dependencies.difference_update(ready)
        return ordered
