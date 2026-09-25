from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import os
import shlex
import shutil
import time
import traceback
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pydantic
from hmz.flows import (
    BudgetExceeded,
    EnvError,
    FlowRuntimeError,
    HarnessContended,
    HarnessDropped,
    HarnessKilled,
    HarnessMissing,
    HarnessThrottled,
    OutputSchemaError,
    OutworlderAway,
    SessionError,
    load,
)

from . import models
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
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from hmz.flows import FlowContext


GEN_PLAN = "humanize1:gen-plan"
SKILL = (
    Path(__file__).resolve().parents[1] / "skills" / "recursive-lean-proof" / "SKILL.md"
)
WORKTREE_RLCR = ":worktree-rlcr"
TURN = ":turn"
#: Seconds a failed turn waits before its step goes round again.
FAILED_PAUSE = 10.0
#: The scratch directory the runtime keeps for the problem repository, which node and
#: integration worktrees are checked out under.
WORKTREES = "recursive-lean-worktrees"
SHORT_PATH = 180
ROOT_TYPE = "Root declarations are fixed by Challenge.lean and the official comparator."
INTEGRATION_GIT = (
    "git",
    "-c",
    "user.name=Humanize Recursive Integrator",
    "-c",
    "user.email=humanize-recursive@example.invalid",
)
STATE = ("version", "task_digest", "run_dir", "last_failure")
#: How a turn can fail that another try may not: answered with nothing, as a suppressed turn
#: always was. A refused credential, a model not served or an unrecoverable turn still raise.
FAILED_TURN = (
    HarnessContended,
    HarnessDropped,
    HarnessKilled,
    HarnessMissing,
    HarnessThrottled,
    OutputSchemaError,
    SessionError,
)
UNION = """set -u
tmp=$(mktemp -d) || exit 9
trap 'rm -rf "$tmp"' EXIT
git show ":2:$1" > "$tmp/ours" || exit 2
git show ":1:$1" > "$tmp/base" || exit 1
git show ":3:$1" > "$tmp/theirs" || exit 3
git merge-file --union -p "$tmp/ours" "$tmp/base" "$tmp/theirs" > "$tmp/merged"
[ $? -lt 128 ] || exit 5
cat "$tmp/merged" > "$1" || exit 6
git add -- "$1" || exit 7
"""


def _fatal(error: BaseException) -> bool:
    """Whether an error stops the whole run rather than the step it arose in.

    The runtime's own errors do -- a spent budget, a stopped run, a flow called with what it
    does not accept -- except an outworlder nobody is there to answer for; and so does a
    flow's params refusing what this one builds, which no other try would change.
    """
    return isinstance(error, pydantic.ValidationError) or (
        isinstance(error, FlowRuntimeError) and not isinstance(error, OutworlderAway)
    )


def _raised(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """What a frontier raises when one of its nodes stopped the run.

    A cancel from outside stays a cancel; otherwise the first node's own error, unwrapped.
    """
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        return asyncio.CancelledError()
    first = group.exceptions[0]
    while isinstance(first, BaseExceptionGroup):
        first = first.exceptions[0]
    return first


async def _finished(work: Awaitable[Any]) -> Any:
    """Awaits a change to the problem's Git repository, which a stopped run lets finish.

    Stopping a run kills the command it is waiting on; one half way through updating the
    problem checkout, its branches or its worktrees would leave a lock or a half-written
    tree behind. The stop still stops the run, once the change is made.
    """
    running = asyncio.ensure_future(work)
    stopped = False
    while not running.done():
        try:
            # `wait` leaves what it waits on running when the waiter is cancelled.
            await asyncio.wait([running])
        except asyncio.CancelledError:
            stopped = True
    if stopped:
        if not running.cancelled():
            running.exception()
        raise asyncio.CancelledError
    return running.result()


async def take_turn(agent: Any, prompt: str, schema_name: str, env: Any) -> Any:
    """One turn in a fresh session: the answer, or nothing where the turn failed.

    Nothing is None for an answer asked for as a model of `models`, and "" for text. A turn
    that failed waits a little before the step it belongs to tries again.
    """
    schema = getattr(models, schema_name) if schema_name else None
    session = await agent.spawn(env=env)
    try:
        if schema is None:
            return await agent.run(prompt, session=session)
        return await agent.run(prompt, session=session, output_schema=schema)
    except FAILED_TURN as error:
        print(f"[turn] {agent.role} answered nothing: {error}")
        if not isinstance(error, OutputSchemaError):
            await asyncio.sleep(FAILED_PAUSE)
        return None if schema is not None else ""


@functools.cache
def _discipline() -> str:
    """This flow's skill, for the humanize1 flows it hands plans to, which do not carry it."""
    text = SKILL.read_text(encoding="utf-8")
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return f"\n## Recursive Lean proof discipline\n\n{text.strip()}\n"


class Runtime:
    def __init__(
        self,
        agents: Mapping[str, Any],
        envs: Mapping[str, Any],
        task: str,
        config: Any,
        state: Any,
        ctx: FlowContext | None = None,
    ) -> None:
        self.worker: Any = agents.get("worker")
        self.reviewer: Any = agents.get("reviewer")
        self.workspace = envs["workspace"]
        self.task = task.strip()
        self.config = config
        self.state = state if state is not None else {}
        self.ctx = ctx
        self._since = time.monotonic()
        self.project = Path(str(self.workspace.workdir)).resolve()
        self._worktrees: Any = None
        self._worktree_lock = asyncio.Lock()
        self._integration_lock = asyncio.Lock()
        self._integrations: dict[str, asyncio.Task[SolveResult]] = {}
        self.run_root = self._run_root()
        self.store = Store(
            self.run_root,
            self.project / self.config.wiki_dir,
            self.task,
        )

    async def execute(self) -> None:
        if not self.task:
            raise ValueError("recursive_lean_prover needs a mathematical problem")
        await self._require_git()
        self._require_comparator()
        await self._worktree_root()
        latest = self.project / self.config.artifact_dir / "LATEST"
        atomic_text(latest, str(self.run_root.relative_to(self.project)) + "\n")
        self._remember()
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
        try:
            result = await (
                self._resume_existing_dag(root)
                if root.children and root.plan and root.natural_proof
                else self._solve(root)
            )
        except BaseException:
            # Accepted candidates stay `integrating`, and a resumed run integrates them.
            for pending in self._integrations.values():
                pending.cancel()
            await asyncio.gather(*self._integrations.values(), return_exceptions=True)
            raise
        if result.ok:
            await self._wait_for_integrations()
            print(
                f"Proved root theorem; {len(result.theorems)} theorem record(s) at root."
            )
            self._forget()
            return
        self._remember(last_failure=result.feedback)
        print(f"Root theorem not accepted: {result.feedback}")
        # What was accepted on the way still integrates before the run ends.
        integrated = await asyncio.gather(
            *self._integrations.values(), return_exceptions=True
        )
        for node_id, outcome in zip(self._integrations, integrated, strict=True):
            if isinstance(outcome, BaseException):
                print(f"Integration of {node_id} failed: {outcome!r}")

    def _remember(self, **extra: str) -> None:
        self.state["version"] = 1
        self.state["task_digest"] = self._task_digest()
        self.state["run_dir"] = str(self.run_root.relative_to(self.project))
        for key, value in extra.items():
            self.state[key] = value

    def _recalled(self, key: str) -> Any:
        return self.state[key] if key in self.state else None

    def _forget(self) -> None:
        for key in STATE:
            if key in self.state:
                del self.state[key]

    def _spent(self) -> bool:
        """Whether this run's own budget is spent, rather than one a flow it called set."""
        if self.ctx is None:
            return True
        budget, usage = self.ctx.budget, self.ctx.usage
        return (
            (budget.cost is not None and usage.cost >= budget.cost)
            or (
                budget.output_tokens is not None
                and usage.output_tokens >= budget.output_tokens
            )
            or (
                budget.duration is not None
                and time.monotonic() - self._since >= budget.duration.total_seconds()
            )
        )

    async def _ask(
        self,
        agent: Any,
        prompt: str,
        schema: type[pydantic.BaseModel] | None = None,
        env: Any = None,
    ) -> Any:
        """One turn in a fresh session: an instance of `schema`, or text where it is None.

        Taken as a `turn` call of its own, so that the session -- and, for some harnesses,
        the CLI process serving it -- is closed as soon as the turn is over rather than when
        the whole run is. A turn that failed answers None, or "" for text.
        """
        turn = load(TURN)
        taking: Any = {agent.role: agent}
        return await turn(
            prompt,
            agents=taking,
            envs={"workspace": env or self.workspace},
            params=turn.expected_params.model_validate(
                {
                    "role": agent.role,
                    "schema_name": "" if schema is None else schema.__name__,
                }
            ),
        )

    async def _solve(self, node: NodeRecord) -> SolveResult:
        if node.status == "proved":
            return SolveResult(
                ok=True,
                node_id=node.id,
                theorems=self._checkpoint_theorems(node),
            )
        if node.status == "integrating" and node.candidate_commit:
            return await self._resume_accepted_candidate(node)
        feedback = node.message if node.status == "failed" else "None."
        plan = self._recorded_plan(node)
        if plan is None:
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
            if plan is None:
                if plan_attempted:
                    break
                plan_attempted = True
                plan = await self._accepted_plan(node, feedback)
                if plan is not None:
                    feedback = node.message
            if plan is None:
                feedback = (
                    "One-time direct plan generation produced no usable scaffold."
                )
                break
            natural = await self._accepted_natural_proof(node, plan, feedback)
            if natural is None:
                feedback = "No complete natural-language proof survived review."
                continue
            decomposition = await self._decompose(node, natural)
            if decomposition is None:
                feedback = node.message or (
                    "The proposed subproblem graph was invalid or cyclic."
                )
                continue
            children = await self._solve_children(node, decomposition)
            failed = [one for one in children if not one.ok]
            if failed and self.config.stop_on_child_failure:
                feedback = "Required child failure(s): " + "; ".join(
                    f"{one.node_id}: {one.feedback}" for one in failed
                )
                continue
            result = await self._formalize(node, plan, natural, children)
            if result.ok:
                return result
            feedback = result.feedback
            if node.parent:
                self._revise_parent(node, feedback)
        self.store.update(node.id, "failed", feedback)
        return SolveResult(ok=False, node_id=node.id, feedback=feedback)

    async def _accepted_plan(self, node: NodeRecord, feedback: str) -> Path | None:
        preserved = self._preserved_plan(node) if node.status == "interrupted" else None
        if preserved is not None:
            self.store.update(
                node.id,
                "natural-proof",
                f"preserved scaffold {preserved.name} frozen; iterate only NL proof",
                plan=str(preserved.relative_to(self.project)),
            )
            return preserved
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
        atomic_text(draft, body + _discipline())
        self.store.update(
            node.id,
            "planning",
            f"direct one-time plan generation {version}",
            attempts=node.attempts,
        )
        try:
            gen_plan = load(GEN_PLAN)
            await gen_plan(
                f"Plan a correct natural and Lean proof for DAG node {node.id}",
                agents={"planner": self.worker, "analyst": self.reviewer},
                envs={"workspace": self.workspace},
                params=gen_plan.expected_params.model_validate(
                    {
                        "input": str(draft.relative_to(self.project)),
                        "output": str(output.relative_to(self.project)),
                        "mode": "direct",
                        "auto_start_rlcr_if_converged": False,
                        "turn_timeout": self.config.plan_turn_timeout,
                        "total_timeout": self.config.plan_total_timeout,
                        "turn_retries": 1,
                    }
                ),
            )
        except BudgetExceeded:
            # gen-plan's own timeouts stop planning; this run's spent budget stops the run.
            if self._spent():
                raise
            self.store.update(
                node.id,
                "natural-proof",
                f"direct plan stopped; scaffold draft {version} frozen for NL proof",
                plan=str(draft.relative_to(self.project)),
            )
            return draft
        except Exception as error:
            if _fatal(error):
                raise
            self.store.update(
                node.id,
                "natural-proof",
                f"direct plan unavailable; scaffold draft {version} frozen for NL proof",
                plan=str(draft.relative_to(self.project)),
            )
            return draft
        if not output.is_file() or not output.read_text(encoding="utf-8").strip():
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

    async def _accepted_natural_proof(
        self, node: NodeRecord, plan_path: Path, outer_feedback: str = ""
    ) -> NaturalProof | None:
        plan = plan_path.read_text(encoding="utf-8")
        prior_proof, feedback = self._latest_natural_checkpoint(node)
        if outer_feedback and outer_feedback not in {
            "None.",
            "No complete natural-language proof survived review.",
        }:
            feedback = outer_feedback
        version = 0
        while True:
            for _ in range(self.config.natural_proof_attempts):
                version = self._next_json_version(node, "natural-proof-draft")
                self.store.update(
                    node.id,
                    "natural-proof",
                    f"natural-language RLCR author revision {version}",
                )
                proof = await self._ask(
                    self.worker,
                    NATURAL_PROOF.format(
                        statement=node.statement,
                        plan=plan,
                        feedback=feedback,
                        prior_proof=prior_proof,
                    ),
                    NaturalProof,
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
                audit = await self._ask(
                    self.reviewer,
                    NATURAL_AUDIT.format(statement=node.statement, proof=proof.proof),
                    NaturalAudit,
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

    async def _decompose(
        self, node: NodeRecord, proof: NaturalProof
    ) -> Decomposition | None:
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
                made = await self._ask(
                    self.worker,
                    DECOMPOSE.format(
                        max_children=self.config.max_children,
                        depth=node.depth,
                        max_depth=self.config.max_depth,
                        statement=node.statement,
                        proof=proof.proof,
                        feedback=feedback,
                    ),
                    Decomposition,
                )
            except Exception as error:
                if _fatal(error):
                    raise
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
                audit = await self._ask(
                    self.reviewer,
                    DECOMPOSITION_AUDIT.format(
                        statement=node.statement,
                        proof=proof.proof,
                        decomposition=made.model_dump_json(indent=2),
                    ),
                    DecompositionAudit,
                )
            except Exception as error:
                if _fatal(error):
                    raise
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

    async def _bounded(
        self,
        gate: asyncio.Semaphore,
        work: Callable[[NodeRecord], Awaitable[SolveResult]],
        node: NodeRecord,
        label: str,
        *,
        record: bool = False,
    ) -> SolveResult:
        async with gate:
            try:
                return await work(node)
            except Exception as error:
                if _fatal(error):
                    raise
                result = SolveResult(
                    ok=False, node_id=node.id, feedback=f"{label}: {error}"
                )
                if record:
                    self.store.update(node.id, "failed", result.feedback)
                return result

    async def _solve_children(
        self,
        parent: NodeRecord,
        decomposition: Decomposition,
    ) -> list[SolveResult]:
        if not decomposition.should_split:
            return []
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
        gate = asyncio.Semaphore(
            min(self.config.max_parallel_children, max(1, len(pending)))
        )
        try:
            async with asyncio.TaskGroup() as group:
                running: dict[asyncio.Task[SolveResult], str] = {}
                while pending or running:
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
                                feedback=(
                                    f"dependency {dependency_failure.node_id} failed"
                                ),
                            )
                            self.store.update(made[key].id, "failed", result.feedback)
                            results[key] = result
                            pending.remove(key)
                            progressed = True
                            continue
                        if all(
                            dependency in results for dependency in child.depends_on
                        ):
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
                                    results[key] = SolveResult(
                                        ok=False,
                                        node_id=checkpoint.id,
                                        feedback=(
                                            "accepted checkpoint lacks durable "
                                            "reviewer metadata"
                                        ),
                                    )
                                else:
                                    self._integrate_later(checkpoint)
                                    results[key] = SolveResult(
                                        ok=True,
                                        node_id=checkpoint.id,
                                        theorems=theorems,
                                    )
                            else:
                                task = group.create_task(
                                    self._bounded(
                                        gate,
                                        self._solve,
                                        checkpoint,
                                        "parallel child worker failed",
                                        record=True,
                                    )
                                )
                                running[task] = key
                            progressed = True
                    if not running:
                        if pending and not progressed:
                            for key in sorted(pending):
                                result = SolveResult(
                                    ok=False,
                                    node_id=made[key].id,
                                    feedback="no dependency-ready node in child DAG",
                                )
                                self.store.update(
                                    made[key].id, "failed", result.feedback
                                )
                                results[key] = result
                            pending.clear()
                        continue
                    done, _ = await asyncio.wait(
                        running, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        results[running.pop(task)] = task.result()
        except BaseExceptionGroup as failed:
            raise _raised(failed) from None
        return [results[one.key] for one in decomposition.subproblems]

    async def _resume_existing_dag(self, root: NodeRecord) -> SolveResult:
        managed = {root.id}
        frontier = [root.id]
        while frontier:
            node = self.store.nodes[frontier.pop()]
            for related in [*node.children, *node.depends_on]:
                if related in self.store.nodes and related not in managed:
                    managed.add(related)
                    frontier.append(related)
        scheduled: set[str] = set()
        workers = min(self.config.max_parallel_children, max(1, len(managed)))

        if root.status == "integrating" and root.candidate_commit:
            for node_id in sorted(managed - {root.id}):
                node = self.store.nodes[node_id]
                if node.status == "integrating" and node.candidate_commit:
                    self._integrate_later(node)
            await self._wait_for_integrations()
            return await self._resume_accepted_candidate(root)

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
                    self._integrate_later(node)
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

        gate = asyncio.Semaphore(workers)
        try:
            async with asyncio.TaskGroup() as group:
                running: dict[asyncio.Task[SolveResult], str] = {}
                while self.store.nodes[root.id].status != "proved":
                    for node in ready_nodes():
                        scheduled.add(node.id)
                        self.store.update(
                            node.id,
                            "queued",
                            "dependency-ready; launched in global DAG frontier",
                        )
                        task = group.create_task(
                            self._bounded(
                                gate,
                                self._formalize_checkpoint_parent
                                if node.children
                                else self._solve,
                                node,
                                "global frontier worker failed",
                            )
                        )
                        running[task] = node.id
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
                    done, _ = await asyncio.wait(
                        running, return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in done:
                        node_id = running.pop(task)
                        result = task.result()
                        if not result.ok:
                            return SolveResult(
                                ok=False,
                                node_id=root.id,
                                feedback=f"{node_id}: {result.feedback}",
                            )
        except BaseExceptionGroup as failed:
            raise _raised(failed) from None
        root_record = self.store.nodes[root.id]
        return SolveResult(
            ok=True,
            node_id=root.id,
            theorems=self._checkpoint_theorems(root_record),
        )

    async def _formalize_checkpoint_parent(self, node: NodeRecord) -> SolveResult:
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
            result = await self._formalize(node, plan, natural, children)
            if result.ok:
                return result
            natural = await self._accepted_natural_proof(node, plan, result.feedback)
            if natural is None:
                return result

    def _checkpoint_theorems(self, node: NodeRecord) -> list[ProvedTheorem]:
        audit = self._latest_lean_audit(node)
        if audit is not None:
            return audit.theorems
        lean_file = (
            node.lean_files[0]
            if node.lean_files
            else self.config.lean_target or "Submission.lean"
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
        return node.status == "proved" or (
            node.status == "integrating" and bool(node.candidate_commit)
        )

    def _latest_lean_audit(self, node: NodeRecord) -> LeanAudit | None:
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

    async def _resume_accepted_candidate(self, node: NodeRecord) -> SolveResult:
        theorems = self._checkpoint_theorems(node)
        if not theorems:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback="accepted checkpoint has no durable reviewer theorem record",
            )
        if not node.proof_base_commit or not node.candidate_commit:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback="accepted checkpoint lacks its Git base or candidate commit",
            )
        return await self._complete_accepted_integration(
            node,
            node.proof_base_commit,
            node.candidate_commit,
            theorems,
        )

    def _integrate_later(
        self,
        node: NodeRecord,
        before: str = "",
        after: str = "",
        theorems: list[ProvedTheorem] | None = None,
        comparator_log: str = "",
    ) -> None:
        if node.id in self._integrations:
            return
        self._integrations[node.id] = asyncio.create_task(
            self._complete_accepted_integration(
                node,
                before or node.proof_base_commit,
                after or node.candidate_commit,
                self._checkpoint_theorems(node) if theorems is None else theorems,
                comparator_log,
            )
        )

    async def _wait_for_integrations(self) -> None:
        while True:
            tasks = list(self._integrations.values())
            unfinished = [task for task in tasks if not task.done()]
            if not unfinished:
                for task in tasks:
                    task.result()
                return
            await asyncio.wait(unfinished, return_when=asyncio.FIRST_COMPLETED)

    async def _complete_accepted_integration(
        self,
        node: NodeRecord,
        before: str,
        after: str,
        theorems: list[ProvedTheorem],
        comparator_log: str = "",
    ) -> SolveResult:
        integrated, feedback = await self._integrate_reviewed_candidate(
            before,
            after,
            node=node,
            lean_files=node.lean_files,
        )
        if not integrated:  # pragma: no cover - integration retries until success
            return SolveResult(ok=False, node_id=node.id, feedback=feedback)
        integrated_head = await self._git_head()
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

    async def _overlay_accepted_children(
        self, node: NodeRecord, worktree: Any
    ) -> tuple[bool, str]:
        commits: list[str] = []
        seen: set[str] = set()
        prerequisite_ids = list(dict.fromkeys([*node.children, *node.depends_on]))
        current = await self._git_head(worktree)
        for child_id in prerequisite_ids:
            child = self.store.nodes.get(child_id)
            if child is None or not self._accepted_checkpoint(child):
                continue
            if not child.candidate_commit or not child.proof_base_commit:
                continue
            listed, history, _ = await self.workspace.exec(
                [
                    "git",
                    "rev-list",
                    "--reverse",
                    f"{child.proof_base_commit}..{child.candidate_commit}",
                ]
            )
            if listed:
                return (
                    False,
                    f"could not enumerate accepted child history for {child.id}",
                )
            for commit in history.splitlines():
                if not commit or commit in seen:
                    continue
                present, _, _ = await worktree.exec(
                    ["git", "merge-base", "--is-ancestor", commit, current]
                )
                if present != 0:
                    commits.append(commit)
                    seen.add(commit)
        if not commits:
            return True, "all accepted child checkpoints already present"
        if not await self._git_clean(worktree):
            return False, "parent worktree is dirty before accepted-child overlay"
        applied, unioned, detail = await self._apply_candidate_commits(
            worktree, commits
        )
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

    async def _formalize(
        self,
        node: NodeRecord,
        plan_path: Path,
        natural: NaturalProof,
        children: list[SolveResult],
    ) -> SolveResult:
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
            worktree = await self._node_worktree(node)
        except (EnvError, RuntimeError) as error:
            return SolveResult(ok=False, node_id=node.id, feedback=str(error))
        where = node.worktree
        before = node.proof_base_commit or await self._git_head(worktree)
        overlaid, overlay_feedback = await self._overlay_accepted_children(
            node, worktree
        )
        if not overlaid:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=overlay_feedback,
            )
        review_base = await self._git_head(worktree)
        self.store.update(
            node.id,
            "rlcr-lean",
            f"isolated humanize1:rlcr formalization in {where}",
            worktree=where,
            proof_branch=self._node_branch(node),
            proof_base_commit=before,
        )
        task = RLCR_LEAN_TASK.format(
            node_id=node.id,
            plan_path=plan_path,
            natural_path=natural_path,
            statement=node.statement,
            lean_statement=node.lean_statement or ROOT_TYPE,
            lean_name=node.lean_name or "choose a descriptive theorem name",
            proof_base_commit=before,
            lean_target=self.config.lean_target
            or "infer the repository's correct target .lean file",
            children=child_text,
            comparator_command=self._review_command(node, []),
            comparator_success=self.config.comparator_success,
        )
        rlcr_ok, rlcr_log = await self._run_rlcr(
            node, worktree, plan_path, task, review_base
        )
        if not rlcr_ok:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=f"isolated humanize1:rlcr failed; see {rlcr_log}",
            )
        after = await self._git_head(worktree)
        if not await self._git_clean(worktree):
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback=f"RLCR left uncommitted participant changes in {where}",
            )
        lean_files = await self._lean_files(before, after, worktree)
        if not lean_files:
            return SolveResult(
                ok=False,
                node_id=node.id,
                feedback="RLCR completed without an identifiable Lean target",
            )
        self.store.update(
            node.id,
            "comparing",
            f"running independent machine comparator in {where}",
            lean_files=lean_files,
        )
        passed, log_path, log = await self._compare(node, lean_files, worktree)
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
            f"fresh reviewer reruns comparator in {where}",
        )
        audit = await self._ask(
            self.reviewer,
            LEAN_AUDIT.format(
                node_id=node.id,
                statement=node.statement,
                lean_statement=node.lean_statement or ROOT_TYPE,
                proof_base_commit=before,
                lean_files="\n".join(f"- {one}" for one in lean_files),
                comparator_command=self._review_command(node, lean_files),
                comparator_success=self.config.comparator_success,
                comparator_log=log[-12000:],
            ),
            LeanAudit,
            worktree,
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
            self._integrate_later(node, before, after, audit.theorems, log)
            return SolveResult(ok=True, node_id=node.id, theorems=audit.theorems)
        await self._wait_for_integrations()
        return await self._complete_accepted_integration(
            node,
            before,
            after,
            audit.theorems,
            log,
        )

    def _revise_parent(self, child: NodeRecord, failure: str) -> None:
        if child.parent is None:
            return
        parent = self.store.nodes[child.parent]
        self.store.update(
            parent.id,
            "waiting-children",
            f"revise latest natural proof after {child.id} failed: {failure}",
        )

    async def _compare(
        self,
        node: NodeRecord,
        lean_files: list[str],
        env: Any = None,
        *,
        label: str = "",
    ) -> tuple[bool, Path, str]:
        rendered = self._render_command(node, lean_files)
        variables = {
            "HUMANIZE_NODE_ID": node.id,
            "HUMANIZE_NODE_STATEMENT": node.statement,
            "HUMANIZE_LEAN_FILES": os.pathsep.join(lean_files),
            "HUMANIZE_RUN_DIR": str(self.run_root),
            "HUMANIZE_WIKI_DIR": str(self.store.wiki),
        }
        argv = [
            "env",
            *(f"{name}={value}" for name, value in variables.items()),
            *shlex.split(rendered),
        ]
        try:
            code, out, err = await (env or self.workspace).exec(
                argv, timeout=self.config.comparator_timeout
            )
            log = (
                f"command: {rendered}\nexit: {code}\n\n"
                f"stdout:\n{out}\n\nstderr:\n{err}\n"
            )
            passed = code == 0 and self.config.comparator_success in out + err
        except EnvError as error:
            log = f"command: {rendered}\ncomparator execution failed: {error}\n"
            passed = False
        suffix = f"-{slug(label)}" if label else ""
        path = self._node_dir(node) / f"comparator-v{node.attempts}{suffix}.log"
        atomic_text(path, log)
        return passed, path, log

    async def _lean_files(self, before: str, after: str, env: Any = None) -> list[str]:
        env = env or self.workspace
        found: set[str] = set()
        if before and after:
            code, out, _ = await env.exec(
                ["git", "diff", "--name-only", f"{before}..{after}", "--", "*.lean"]
            )
            if code == 0:
                found.update(one.strip() for one in out.splitlines() if one.strip())
        if (
            self.config.lean_target
            and (Path(str(env.workdir)) / self.config.lean_target).is_file()
        ):
            found.add(self.config.lean_target)
        return sorted(found)

    def _review_command(self, node: NodeRecord, lean_files: list[str]) -> str:
        environment = (
            f"HUMANIZE_RUN_DIR={shlex.quote(str(self.run_root))} "
            f"HUMANIZE_WIKI_DIR={shlex.quote(str(self.store.wiki))}"
        )
        return f"env {environment} {self._render_command(node, lean_files)}"

    async def _run_rlcr(
        self,
        node: NodeRecord,
        worktree: Any,
        plan_path: Path,
        task: str,
        review_base: str,
    ) -> tuple[bool, Path]:
        node_dir = self._node_dir(node)
        log_path = node_dir / f"rlcr-process-v{node.attempts}.log"
        try:
            nested = load(WORKTREE_RLCR)
            config = nested.expected_params.model_validate(
                {
                    "plan_file": str(plan_path),
                    "max": self.config.rlcr_rounds,
                    "base_branch": review_base,
                }
            )
            atomic_text(
                node_dir / f"rlcr-config-v{node.attempts}.json",
                config.model_dump_json(indent=2) + "\n",
            )
            reason = await nested(
                task,
                agents={"worker": self.worker, "reviewer": self.reviewer},
                envs={"workspace": worktree},
                params=config,
            )
        except Exception as error:
            # RLCR failing, or running out of a budget of its own, fails this attempt only.
            if _fatal(error) and (
                not isinstance(error, BudgetExceeded) or self._spent()
            ):
                raise
            atomic_text(
                log_path,
                f"humanize1:rlcr in {worktree.workdir} failed:\n\n"
                + "".join(traceback.format_exception(error)),
            )
            return False, log_path
        # How the loop ended is RLCR's to say; whether the node is proved is the gates'.
        atomic_text(
            log_path, f"humanize1:rlcr in {worktree.workdir} returned {reason!r}\n"
        )
        return True, log_path

    async def _worktree_root(self) -> Any:
        if self._worktrees is None:
            self._worktrees = await self.workspace.derive_scratch(WORKTREES)
        return self._worktrees

    async def _node_worktree(self, node: NodeRecord) -> Any:
        scratch = await self._worktree_root()
        root = Path(str(scratch.workdir))
        branch = self._node_branch(node)
        recorded = Path(node.worktree) if node.worktree else None
        if (
            recorded is not None
            and await self._git_toplevel(recorded) == recorded.resolve()
        ):
            if recorded.is_relative_to(root):
                return await self._attached(scratch, root, recorded, branch)
            # A checkout this run can no longer reach: let go of its branch, which the
            # new worktree checks out, and leave the rest of it where it is.
            await _finished(
                self.workspace.exec(
                    ["git", "-C", str(recorded), "checkout", "--quiet", "--detach"]
                )
            )
        path = self._node_worktree_path(node, root)
        if await self._git_toplevel(path) != path.resolve():
            if path.exists() and any(path.iterdir()):
                raise RuntimeError(
                    f"node worktree path exists but is not a Git worktree: {path}"
                )
            detail = "unknown Git error"
            for retry in range(6):
                async with self._worktree_lock:
                    if await self._git_toplevel(path) == path.resolve():
                        break
                    await _finished(self.workspace.exec(["git", "worktree", "prune"]))
                    try:
                        await self._branch_at_head(branch)
                        await _finished(
                            self.workspace.derive_worktree(ref=branch, dir=str(path))
                        )
                        break
                    except (EnvError, RuntimeError) as error:
                        detail = str(error)
                await asyncio.sleep(0.2 * (retry + 1))
            if await self._git_toplevel(path) != path.resolve():
                raise RuntimeError(f"could not create isolated node worktree: {detail}")
        worktree = await self._attached(scratch, root, path, branch)
        node.worktree = str(path)
        node.proof_branch = branch
        if not node.proof_base_commit:
            node.proof_base_commit = await self._git_head(worktree)
        return worktree

    async def _branch_at_head(self, branch: str) -> None:
        exists, _, _ = await self.workspace.exec(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        )
        if exists == 0:
            return
        made, out, err = await _finished(
            self.workspace.exec(["git", "branch", branch, "HEAD"])
        )
        if made:
            raise RuntimeError(
                f"could not create node branch {branch}: {(err or out).strip()}"
            )

    async def _attached(self, scratch: Any, root: Path, path: Path, branch: str) -> Any:
        """The node worktree at `path`, on its branch and ready for Lake.

        A run stopped between two steps may have left a cherry-pick under way in it, or left
        it detached where it was just made; either is put right first.
        """
        worktree = await scratch.derive_subdir(subdir=str(path.relative_to(root)))
        picking, _, _ = await worktree.exec(
            ["git", "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD"]
        )
        if picking == 0:
            await _finished(worktree.exec(["git", "cherry-pick", "--abort"]))
        named, current, _ = await worktree.exec(
            ["git", "symbolic-ref", "-q", "--short", "HEAD"]
        )
        if named or current.strip() != branch:
            known, tip, _ = await worktree.exec(
                ["git", "rev-parse", "-q", "--verify", f"refs/heads/{branch}"]
            )
            if known == 0 and tip.strip() == await self._git_head(worktree):
                checked, out, err = await _finished(
                    worktree.exec(["git", "checkout", "--quiet", branch])
                )
                if checked:
                    raise RuntimeError(
                        f"could not check out node branch {branch}: "
                        f"{(err or out).strip()}"
                    )
        await self._prepare_lake_workspace(worktree)
        return worktree

    def _node_worktree_path(self, node: NodeRecord, root: Path) -> Path:
        descriptive = (
            root
            / self.run_root.name
            / slug(node.id)
            / f"attempt-{max(node.attempts, 1)}"
            / self.project.name
        )
        if len(str(descriptive)) <= SHORT_PATH:
            return descriptive
        identity = "\0".join(
            (
                str(self.project),
                self.run_root.name,
                node.id,
                str(max(node.attempts, 1)),
            )
        )
        digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
        return root / digest / self.project.name

    async def _prepare_lake_workspace(self, env: Any) -> None:
        path = Path(str(env.workdir))
        packages = self.project / ".lake" / "packages"
        linked = path / ".lake" / "packages"
        packages_ignored, _, _ = await env.exec(
            ["git", "check-ignore", "--quiet", ".lake/packages"]
        )
        if packages.is_dir() and not linked.exists() and packages_ignored == 0:
            linked.parent.mkdir(parents=True, exist_ok=True)
            linked.symlink_to(packages, target_is_directory=True)

        manifest = path / "lake-manifest.json"
        manifest_ignored, _, _ = await env.exec(
            ["git", "check-ignore", "--quiet", "lake-manifest.json"]
        )
        if manifest.exists() or manifest_ignored != 0:
            return

        sources = [self.project / "lake-manifest.json"]
        common, found, _ = await self.workspace.exec(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]
        )
        if common == 0 and found.strip():
            sources.append(Path(found.strip()).parent / "lake-manifest.json")
        for source in sources:
            if source.is_file() and source.resolve() != manifest.resolve():
                shutil.copy2(source, manifest)
                return

    def _node_branch(self, node: NodeRecord) -> str:
        if node.proof_branch:
            return node.proof_branch
        return (
            "humanize-recursive/"
            f"{slug(self.project.name)}/{slug(self.run_root.name)}/"
            f"{slug(node.id)}-a{max(node.attempts, 1)}"
        )

    async def _integrate_reviewed_candidate(
        self,
        before: str,
        after: str,
        *,
        node: NodeRecord,
        lean_files: list[str],
    ) -> tuple[bool, str]:
        retry = 0
        while True:
            integrated, feedback = await self._integrate_candidate(
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
            await asyncio.sleep(min(60.0, float(retry)))

    async def _integrate_candidate(
        self,
        before: str,
        after: str,
        *,
        node: NodeRecord | None = None,
        lean_files: list[str] | None = None,
    ) -> tuple[bool, str]:
        if not after:
            return False, "the reviewed candidate has no Git commit"
        if before == after:
            return True, "the reviewed theorem was already present at the worktree base"
        listed, history, _ = await self.workspace.exec(
            ["git", "rev-list", "--reverse", f"{before}..{after}"]
        )
        commits = [one for one in history.splitlines() if one]
        if listed or not commits:
            return False, f"could not enumerate reviewed commits {before}..{after}"
        async with self._integration_lock:
            if not await self._git_clean(self.workspace):
                return False, "problem integration worktree is not clean"
            canonical = await self._git_head()
            commits = [
                commit
                for commit in commits
                if (
                    await self.workspace.exec(
                        ["git", "merge-base", "--is-ancestor", commit, canonical]
                    )
                )[0]
                != 0
            ]
            if not commits:
                return (
                    True,
                    "all reviewed commits were already present in the problem branch",
                )
            if canonical == before:
                merged, out, err = await _finished(
                    self.workspace.exec(["git", "merge", "--ff-only", after])
                )
                if merged:
                    detail = (err or out).strip()
                    return (
                        False,
                        f"could not fast-forward reviewed node history: {detail}",
                    )
                return True, f"fast-forwarded {len(commits)} reviewed commit(s)"

            scratch = await self._worktree_root()
            temporary = (
                Path(str(scratch.workdir))
                / "integration"
                / f"{slug(node.id) if node else 'node'}-{uuid.uuid4().hex[:8]}"
            )
            path = temporary / self.project.name
            try:
                integration = await _finished(
                    self.workspace.derive_worktree(ref=canonical, dir=str(path))
                )
            except EnvError as error:
                with contextlib.suppress(OSError):
                    temporary.rmdir()
                return False, f"could not create integration recheck worktree: {error}"
            try:
                await self._prepare_lake_workspace(integration)
                applied, unioned, detail = await self._apply_candidate_commits(
                    integration, commits
                )
                agent_repaired = False
                if not applied:
                    repaired, detail = await self._repair_integration(
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
                    passed, log_path, log = await self._compare(
                        node,
                        lean_files or [],
                        integration,
                        label="integration",
                    )
                    if not passed:
                        repaired, detail = await self._repair_integration(
                            integration,
                            canonical=canonical,
                            commits=commits,
                            node=node,
                            lean_files=lean_files or [],
                            failure=(
                                "combined parallel history failed its integration "
                                "comparator; see "
                                f"{log_path.relative_to(self.project)}\n\n"
                                f"{log[-12000:]}"
                            ),
                        )
                        if not repaired:
                            return False, detail
                        agent_repaired = True
                integration_head = await self._git_head(integration)
                merged, out, err = await _finished(
                    self.workspace.exec(["git", "merge", "--ff-only", integration_head])
                )
                if merged:
                    detail = (err or out).strip()
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
                await _finished(
                    self.workspace.exec(
                        ["git", "worktree", "remove", "--force", str(path)]
                    )
                )
                with contextlib.suppress(OSError):
                    temporary.rmdir()

    async def _repair_integration(
        self,
        integration: Any,
        *,
        canonical: str,
        commits: list[str],
        node: NodeRecord | None,
        lean_files: list[str],
        failure: str,
    ) -> tuple[bool, str]:
        if node is None or self.worker is None:
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
                lean_statement=node.lean_statement or ROOT_TYPE,
                candidate_commits="\n".join(f"- `{one}`" for one in commits),
                failure=feedback[-16000:],
                comparator_command=self._review_command(node, lean_files),
                comparator_success=self.config.comparator_success,
            )
            await self._ask(self.worker, prompt, None, integration)
            if not await self._git_clean(integration):
                feedback = (
                    "integration repair left uncommitted changes; preserve them, finish the "
                    "repair, and commit a clean candidate"
                )
                continue
            integration_head = await self._git_head(integration)
            combined_files = sorted(
                set(lean_files)
                | set(await self._lean_files(canonical, integration_head, integration))
            )
            passed, log_path, log = await self._compare(
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
            audit = await self._ask(
                self.reviewer,
                INTEGRATION_AUDIT.format(
                    node_id=node.id,
                    statement=node.statement,
                    lean_statement=node.lean_statement or ROOT_TYPE,
                    lean_files="\n".join(f"- {one}" for one in combined_files),
                    comparator_command=self._review_command(node, combined_files),
                    comparator_success=self.config.comparator_success,
                    comparator_log=log[-12000:],
                ),
                LeanAudit,
                integration,
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
            if not await self._git_clean(integration):
                feedback = "integration reviewer modified the reviewed worktree"
                continue
            if await self._git_head(integration) != integration_head:
                feedback = "integration reviewer changed the reviewed Git history"
                continue
            return (
                True,
                (
                    "integration repair passed machine comparator and fresh reviewer "
                    f"comparator in round {round_number}"
                ),
            )

    async def _apply_candidate_commits(
        self, integration: Any, commits: list[str]
    ) -> tuple[bool, bool, str]:
        unioned = False
        for commit in commits:
            picked, _, _ = await _finished(
                integration.exec([*INTEGRATION_GIT, "cherry-pick", commit])
            )
            if picked == 0:
                continue
            if await self._git_clean(integration):
                skipped, _, _ = await _finished(
                    integration.exec(["git", "cherry-pick", "--skip"])
                )
                if skipped == 0:
                    continue
            resolved, detail = await self._union_lean_conflicts(integration)
            if not resolved:
                await _finished(integration.exec(["git", "cherry-pick", "--abort"]))
                return (
                    False,
                    unioned,
                    (
                        f"reviewed node commit {commit[:12]} could not be reconciled: {detail}"
                    ),
                )
            unioned = True
            continued, out, err = await _finished(
                integration.exec(
                    [
                        *INTEGRATION_GIT,
                        "-c",
                        "core.editor=true",
                        "cherry-pick",
                        "--continue",
                    ]
                )
            )
            if continued:
                detail = (err or out).strip()
                if await self._git_clean(integration):
                    skipped, _, _ = await _finished(
                        integration.exec(["git", "cherry-pick", "--skip"])
                    )
                    if skipped == 0:
                        continue
                await _finished(integration.exec(["git", "cherry-pick", "--abort"]))
                return (
                    False,
                    unioned,
                    f"could not commit reconciled Lean sources: {detail}",
                )
        return True, unioned, "candidate commits applied"

    @staticmethod
    async def _union_lean_conflicts(integration: Any) -> tuple[bool, str]:
        unmerged, listed, _ = await integration.exec(
            ["git", "diff", "--name-only", "--diff-filter=U", "-z"]
        )
        paths = [one for one in listed.split("\0") if one]
        if unmerged or not paths:
            return False, "Git reported no resolvable unmerged paths"
        if any(not path.endswith(".lean") for path in paths):
            return False, f"non-Lean conflict requires a new proof attempt: {paths}"
        root = Path(str(integration.workdir)).resolve()
        for relative in paths:
            if not (root / relative).resolve().is_relative_to(root):
                return False, f"unsafe conflicted path: {relative}"
            # In the shell, so the sources' bytes go from Git to the file untouched.
            code, _, _ = await _finished(
                integration.exec(f"set -- {shlex.quote(relative)}\n{UNION}")
            )
            if code in {1, 2, 3}:
                return False, f"cannot read merge stage {code} for {relative}"
            if code == 7:
                return False, f"could not stage reconciled Lean source {relative}"
            if code:
                return False, f"text union failed for {relative}"
        return True, f"unioned {len(paths)} Lean source conflict(s)"

    async def _git_toplevel(self, path: Path) -> Path | None:
        if not path.is_dir():
            return None
        code, out, _ = await self.workspace.exec(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"]
        )
        return Path(out.strip()).resolve() if not code else None

    @staticmethod
    async def _git_clean(env: Any) -> bool:
        code, out, _ = await env.exec(["git", "status", "--porcelain"])
        return code == 0 and not out.strip()

    async def _git_head(self, env: Any = None) -> str:
        code, out, _ = await (env or self.workspace).exec(["git", "rev-parse", "HEAD"])
        return out.strip() if code == 0 else ""

    def _render_command(self, node: NodeRecord, lean_files: list[str]) -> str:
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

    async def _require_git(self) -> None:
        code, out, _ = await self.workspace.exec(
            ["git", "rev-parse", "--show-toplevel"]
        )
        if code or Path(out.strip()).resolve() != self.project:
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
        previous = self._recalled("run_dir")
        if (
            self._recalled("version") == 1
            and self._recalled("task_digest") == digest
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
        atomic_text(path, content + _discipline())
        return path

    def _task_digest(self) -> str:
        return hashlib.sha256(self.task.encode()).hexdigest()

    def _root_lean_name(self) -> str:
        if self.config.lean_target:
            return Path(self.config.lean_target).stem
        return "main_theorem"

    def _next_version(self, node: NodeRecord, prefix: str) -> int:
        existing = self._node_dir(node).glob(f"{prefix}-v*.md")
        return sum(1 for _ in existing) + 1

    def _next_json_version(self, node: NodeRecord, prefix: str) -> int:
        existing = self._node_dir(node).glob(f"{prefix}-v*.json")
        return sum(1 for _ in existing) + 1

    def _latest_natural_checkpoint(self, node: NodeRecord) -> tuple[str, str]:
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
