"""Prompts whose invariants are enforced again by structured flow gates."""

PLAN_DRAFT = """# Recursive Lean theorem node

## Mathematical task

{statement}

## Node identity

- DAG node: `{node_id}`
- Proposed Lean name: `{lean_name}`
- Depth: {depth}
- Parent: `{parent}`

## Required order of work

1. Produce and independently review a complete natural-language proof before writing Lean.
2. From that proof, identify named subproblems with explicit dependency edges when a split is
   useful. Each child must be a self-contained theorem, not merely a tactic-level action.
3. Use the already comparator-approved child theorems when formalizing this node.
4. Formalize the exact statement in Lean in `{lean_target}`. Do not weaken the theorem,
   assumptions, imports, or declarations, and do not introduce axioms, `sorry`, or placeholders.
5. Run `{comparator_command}`. The theorem is not proved unless it exits zero and emits
   `{comparator_success}`.
6. Require a fresh reviewer to inspect the Lean changes and rerun the comparator.
7. Publish every accepted theorem from this node to the run's Markdown wiki.

This artifact is a plan, not the natural-language proof itself. It must give a concrete,
mathematically plausible route and checks for every step, but it must remain a concise scaffold;
the next gated RLCR phase writes, critiques, and revises the complete proof. Do not replace the
proof-producing route with a feasibility report, failure disposition, request for a new run, or
a list of controller decisions. Exact child Lean signatures, DAG node IDs, and detailed formal
interfaces are derived only after the complete prose proof passes review, so do not try to
pre-build that later decomposition here. Lean files already present when this flow began are
inherited checkpoints, not new formalization performed out of order. They may be inspected as
untrusted evidence, but may not be relied on, modified, or accepted until the new natural proof
and child gates have passed. Do not invent controller receipts or services beyond the configured
DAG, wiki, Git checks, and comparator commands.
The root is validated directly against the official trusted Challenge declarations, so its
aggregate DAG name and absence of a child-only frozen type are not defects.

## Acceptance criteria

- Every numbered proof step states why it follows.
- The one-time scaffold gives a concrete proof-producing route and is never iterated.
- Natural language precedes Lean formalization.
- Every required child is comparator-approved before the parent is accepted.
- The machine comparator and the independent reviewer both pass.
- The theorem and its provenance are present in the wiki and the DAG says `proved`.

## Feedback from an earlier attempt

{feedback}
"""

NATURAL_PROOF = """Write the complete natural-language proof for this theorem before any Lean
formalization. Number every logical step. State every lemma with all hypotheses, explain why it
is true, and show exactly how the lemmas imply the requested result. Named lemmas may later
become child DAG nodes, but they are not excuses for a gap: give their mathematical proofs here.
Do not write Lean code and do not edit files.

Theorem:
{statement}

Accepted plan:
{plan}

Reviewer feedback from the previous natural-language attempt:
{feedback}

Latest natural-language proof draft, if one exists:
{prior_proof}

When a latest draft is present, revise it in place conceptually: preserve every sound step,
repair the first rejected step using the reviewer feedback, and continue from that version.
Do not restart from a blank proof or silently discard established parts of the argument.
"""

NATURAL_AUDIT = """Read this natural-language proof one step at a time. You did not write it.
Reject it at the first false, circular, ambiguous, or unjustified step. Check all hypotheses,
boundary cases, quantifiers, and the final implication to the exact theorem. Do not repair it.

This is the mathematical prose gate before decomposition. Named lemmas must have complete
mathematical statements and proofs, but their exact frozen Lean types and `Submission.X`
declarations do not exist yet: the next independent decomposition gate creates and audits those,
and later child workers formalize them. Do not reject this proof solely because those later Lean
artifacts are absent. Do reject a missing mathematical hypothesis, proof, or non-circular
dependency in the prose itself.

Theorem:
{statement}

Proof:
{proof}
"""

DECOMPOSE = """After reading the complete natural-language proof below, decide whether it
should be factored into separately named Lean theorems. This decision is made at every depth,
so a subproblem may itself activate more workers. Split only on genuine reusable mathematical
obligations. When splitting, return 2 to {max_children} self-contained statements, unique
snake_case keys, proposed bare Lean identifiers, and sibling dependencies. A child's
`lean_name` must be only `X`, never `Submission.X`, `Submission_X`, or `SubmissionX`; the
implementation and comparator will refer to that declaration as `Submission.X`. For each child, also give
`lean_statement`: the exact, single-line Lean proposition/type expression for that theorem,
without `theorem`, a declaration name, or `:=` proof. It must elaborate after `import Submission`
before child proof work begins. This type is frozen and later becomes the independent challenge
side of the child comparator. Dependencies must be acyclic.
Return no children when the theorem is already atomic or depth {depth} reached the limit
{max_depth}. Do not use `sorry`, placeholders, or circular restatements of the parent.

Parent theorem:
{statement}

Natural-language proof:
{proof}

Correction after an invalid decomposition:
{feedback}
"""

DECOMPOSITION_AUDIT = """Independently audit this theorem decomposition after the complete
natural-language proof has passed review. You did not create the split.

Check that the split decision is appropriate, every child is a genuine non-circular obligation,
the prose statement includes all hypotheses, dependencies are acyclic and correctly directed,
and every `lean_statement` is an exact one-line Lean proposition matching its prose statement.
Check that every `lean_name` is a bare identifier `X` intended to be declared as `Submission.X`,
not an attempted encoding of the namespace such as `Submission_X` or `SubmissionX`.
The frozen Lean type must be independently usable as the challenge side of a comparator; reject
a full declaration, proof, placeholder, post-hoc alias type, or type that depends on the child
being implemented already. For a split, return exactly one node audit for every key. For an
atomic theorem, return an empty node list and judge the no-split rationale.

Parent theorem:
{statement}

Accepted natural-language proof:
{proof}

Proposed decomposition:
{decomposition}
"""

RLCR_LEAN_TASK = """Formalize DAG node `{node_id}` only, following the accepted plan at
`{plan_path}` and the natural-language proof at `{natural_path}`.

Exact theorem:
{statement}

Proposed declaration name: `{lean_name}`
Frozen expected Lean type: `{lean_statement}`
Lean target: `{lean_target}`

Comparator-approved child theorem wiki pages:
{children}

Requirements:
- Read the natural proof before editing Lean.
- Create or complete a globally named theorem for this node; do not hide it as a local `have`.
- For a child node, its declaration must have exactly the frozen expected Lean type above.
- Preserve the exact target, hypotheses, imports, and declarations.
- No `sorry`, `admit`, new axioms, unsafe loopholes, or weakened replacement theorem.
- Run `{comparator_command}` until it exits zero and contains `{comparator_success}`.
- Commit only real Lean/project changes with a conventional descriptive commit.
- Do not edit anything under `.humanize/` except files the RLCR runtime itself requires.

This nested RLCR stage owns only implementation, a warning-clean build, a clean committed
candidate, and the author's comparator run. Return successfully as soon as those are complete.
Do not wait for, simulate, or mark complete the outer controller's fresh-reviewer comparator
rerun, wiki publication, or DAG `proved` transition: those gates run only after this nested
stage returns. Treating those later gates as unfinished RLCR work creates a circular wait.
"""

LEAN_AUDIT = """Review the Lean proof for this DAG node. You did not write it, and approval is
forbidden unless you personally rerun the exact comparator command shown below. Inspect the git
diff and the named Lean files. Check for weakened statements, changed challenge files/imports,
extra axioms, `sorry`/`admit`, declaration shadowing, or any mismatch with the mathematical
statement. List every new or completed theorem belonging to this node for the wiki.

Node: {node_id}
Mathematical statement:
{statement}

Frozen expected Lean type (children only):
{lean_statement}

Lean files:
{lean_files}

Comparator command to rerun:
{comparator_command}
Required marker: {comparator_success}

Independent machine-gate log from before your review:
{comparator_log}
"""
