---
name: recursive-lean-proof
description: Prove a mathematical statement in natural language before Lean, factor it into independently checkable named lemmas, and accept Lean only through the configured comparator.
---

# Recursive Lean proof discipline

Keep the mathematical statement fixed. A proof is not a proof of a nearby easier theorem.

Before writing new or revised Lean:

1. Give a numbered natural-language proof.
2. State every lemma with every hypothesis.
3. Identify the first unsupported step rather than papering over it.
4. Split only at genuine mathematical obligations; never create circular child statements.

A plan is not itself the complete proof. Generate it once in direct mode as a concrete scaffold,
then freeze it without a separate plan-review or plan-revision stage. The next flow gate writes
and independently reviews the full numbered proof. Lean files that already existed when the flow
started are inherited proof-base material, not automatic evidence that the current node is proved.
Definitions and kernel-checked helper lemmas present at the node's frozen proof-base commit may be
reused as ordinary library infrastructure when the configured comparator and source-safety checks
accept them. The approved-child list governs new candidate histories overlaid after that base; it
is not an exhaustive allowlist of declarations in the base, and an empty child list does not ban
base helpers. Do not reuse an unapproved previous proof of the current node, a placeholder, a new
axiom, or a candidate history absent from both the frozen base and approved children.

At the natural-language proof review gate, audit the mathematical argument and every stated
lemma, but do not require child Lean declarations or frozen Lean type expressions yet. Those are
created and independently audited only in the following decomposition gate. Missing mathematical
hypotheses or circular prose remain rejection reasons; missing post-decomposition Lean artifacts
at this earlier gate do not.

For every recursive child, freeze before formalization both a prose statement with all hypotheses
and a single-line exact Lean proposition/type expression. The expression must not contain a full
declaration or `:=` proof. Independently review that prose/type pair and its acyclic dependencies.
Use a bare child identifier `X` in decomposition metadata; the implementation and comparator refer
to it as `Submission.X`. Do not encode the namespace as `Submission_X` or `SubmissionX`.
The child comparator must compile the candidate against this frozen type; comparing two aliases
whose types are both inferred from the candidate is not an acceptable correctness gate.

The root is intentionally different: its DAG metadata may use the aggregate module name and omit
a child-only frozen type. The root comparator dispatches directly to the official benchmark
challenge, whose trusted declarations fix every required root theorem type. Do not apply the
child-only metadata requirement to that official root gate.

Once the decomposition gate has selected and audited the current DAG, that node identity, frozen
type, and accepted dependency list are authoritative for Lean implementation. An older one-time
scaffold or natural proof may contain speculative interface names or a different decomposition;
use those parts only as mathematical background. A later implementation reviewer must not reopen
planning, replace the selected cone, require extra nodes, or reject an exact comparator-passing
theorem solely for source-layout or certification-architecture preferences. In particular, it
must not reject a proof merely because a kernel-checked helper already existed at the frozen
proof-base commit or lacks a child wiki page.

For Lean:

- Turn each DAG node into a globally named theorem or lemma.
- Do not use `sorry`, `admit`, new axioms, declaration shadowing, or weaker assumptions/targets.
- Preserve challenge files, imports, namespaces, and theorem types unless the task explicitly
  requires an authorized change.
- Run the exact configured comparator. A successful build alone is insufficient.
- A reviewer must rerun the comparator independently before accepting a theorem.

Generate one scaffold plan per node and never iterate it. When a natural-proof reviewer,
decomposition reviewer, isolated comparator, or Lean reviewer rejects the theorem, preserve the
latest prose draft and revise only that natural-language proof before trying another Lean proof.
Once the isolated comparator and the independent reviewer comparator both pass, freeze those
approvals: a later integration failure must remain in an integration-only repair loop and must
never restart the NL proof or revise the parent. This invariant applies at every recursion depth.
Re-decomposition must reuse an accepted theorem by its Lean name; never create an `-a2` copy or
run planning, prose, or Lean proving for it again. Publish every accepted theorem, including leaf
lemmas, to the wiki.

The nested RLCR implementation stage ends after it has produced a warning-clean, committed
candidate and its author comparator run passes. It must then return control immediately. The
outer recursive controller—not nested RLCR—runs the role-distinct reviewer comparator, publishes
the wiki page, and changes the DAG node to `proved`; waiting inside RLCR for those later actions
is a circular wait.

Scan the whole existing DAG and launch every dependency-ready frontier node into a shared worker
pool. Refill the pool as soon as any completion unlocks another node; do not wait for an unrelated
slow branch. Fresh decompositions launch every zero-indegree sibling in the first topological
wave. Planning, natural-proof review, decomposition, Lean formalization, comparator runs, and Lean
review may proceed concurrently. Give every formalizing node its own named Git branch and
worktree, and invoke nested RLCR in a separate process whose real working directory is that
worktree, so Humanize state, source edits, and comparator scratch files are isolated. Serialize
integration of fully comparator- and reviewer-approved histories into the problem branch. When
parallel histories touch the same Lean file, preserve both in an integration worktree and rerun
the comparator before advancing the problem branch. If the combined history fails, use a Codex
worker to repair only the reconciliation, then require both a machine comparator and a fresh
Codex reviewer comparator. Keep the node `integrating` throughout and retain its accepted branch,
plan, NL proof, comparator, and reviewer checkpoints. An `integrating` node has passed both
isolated gates and therefore unlocks its dependants immediately. Overlay its exact accepted commit
history into each dependant's worktree, let parent proving and serialized integration overlap,
and require the root to await every descendant integration before final acceptance. In the live
Mermaid graph every edge is solid and every arrow `A --> B` means A depends on B. Parent theorems
therefore point to their decomposition children, and nodes point to their explicit prerequisites;
a decomposition leaf may still be dependency-blocked.

Persist every natural-language draft and its exact review feedback. If proof review fails or the
run resumes, revise the latest preserved draft—retaining its sound steps—instead of starting the
proof again from an empty response.
