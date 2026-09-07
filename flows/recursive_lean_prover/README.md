# Recursive Lean prover

A native Humanize flow for recursively solving large mathematical problems in Lean. It uses the official
`humanize1:gen-plan` and `humanize1:rlcr` phases, recursively activates subproblem workers,
requires the repository comparator before and during every Lean review, displays a live DAG,
and publishes every accepted theorem to a Markdown wiki.

This repository runs natively on the Humanize 2 `hmz` runtime and flow API. The component names
`official/humanize1:gen-plan` and `official/humanize1:rlcr` are the names under which Humanize 2's
official flowverse currently exposes the ported Humanize 1 algorithms; they do not mean that this
flow runs on the old Humanize 1 runtime. There are currently no `official/humanize2:gen-plan` or
`official/humanize2:rlcr` aliases.

Exactly one scaffold plan is generated in `humanize1:gen-plan` direct mode, with no subsequent
plan-review or plan-revision stage, and retained unchanged. Mathematical defects are
handled by an RLCR-style natural-language loop that repeatedly revises the latest proof draft
from the fresh reviewer's exact first-invalid-step feedback. Exhausting a configured review
batch starts another batch from that checkpoint; it does not regenerate the plan or fail the
node. Decomposition, child, and comparator failures also feed the natural proof, never plan
generation.

The upstream direct gen-plan flow still asks its analyst to check that the input belongs to the
repository and to provide one pre-candidate risk analysis. Those calls happen before the planner
writes its one candidate; they are not candidate-plan convergence reviews. Direct mode skips the
reasonability-review/revision loop entirely. If an interrupted direct run leaves its substantive
output in an atomic-write temporary file, that output is frozen on resume. If no substantive
output survived, the concrete controller input draft is frozen instead and work advances to the
natural-language proof rather than generating another plan.

On resume, the scheduler scans the whole existing DAG and launches every dependency-ready
frontier node into one shared pool. It refills the pool whenever any node completes, so a newly
unblocked branch does not wait for an unrelated slow worker. Fresh decompositions likewise
launch every zero-indegree sibling in the first topological wave. Planning, natural-proof,
review, decomposition, Lean RLCR, comparator runs, and Lean review all run concurrently up to
`max_parallel_children`. Every formalizing node receives its own named Git branch and worktree,
and its official RLCR invocation runs in a separate process whose real working directory is that
worktree. Source edits, Humanize state, and comparator scratch files therefore cannot collide.
Only integration of fully reviewed histories is serialized. If parallel histories edited the
same Lean file, the controller preserves both changes in an integration worktree and requires
another comparator pass before advancing the problem branch. If that combined check fails, an
integration-only Codex repair loop preserves the accepted candidate, reconciles the histories,
and must pass both a machine comparator and a fresh reviewer comparator. It never returns the node
to planning, natural-language proof, decomposition, or theorem proving. Deep repository paths are
mapped to a stable short checkout path under `/tmp/humanize-lean-worktrees`; the named Git branch
retains the durable proof history even if that disposable checkout is later removed.
Each isolated checkout also receives its own ignored copy of the repository's pinned
`lake-manifest.json` and a link to the immutable `.lake/packages` checkout. This keeps Lean builds
offline-reproducible and prevents Lake from trying to update shared read-only Git metadata.

## How the flow works

1. **Restore or create the theorem node.** The controller loads the durable `dag.json`, preserves
   every accepted checkpoint, and creates the root only when no run exists.
2. **Generate one scaffold.** The node invokes `humanize1:gen-plan` in direct mode exactly once.
   The resulting scaffold is frozen. There is no candidate-plan review loop and no later plan
   regeneration.
3. **Prove the mathematics in natural language.** A Codex worker writes a complete proof and an
   independent Codex reviewer checks the first invalid step. A rejection revises the latest proof,
   not the scaffold. `natural_proof_attempts` is only the size of one checkpoint batch: reaching it
   starts another batch from the latest draft and cannot kill the node.
4. **Decide whether to split.** After the prose proof passes, a decomposition audit checks each
   proposed child theorem, its exact Lean statement and name, and the acyclic dependency list. A
   child repeats the same lifecycle, so recursive workers also produce prose before Lean.
5. **Launch the ready frontier.** Every node whose explicit prerequisites and required children
   have passed both isolated comparator gates is launched, up to `max_parallel_children`.
   Independent leaves from the same problem run together. A parent may therefore start while an
   accepted child is still `integrating`; the accepted child commits are overlaid into the
   parent's isolated worktree. In the diagram, `A --> B` always means that A depends on B.
6. **Formalize in isolation.** Each ready Lean node gets a named Git branch and a short independent
   worktree. The official `humanize1:rlcr` worker/reviewer loop builds the Lean proof without sharing
   source files or build scratch state with sibling workers. Its implementation reviewer checks the
   worker against the selected node contract and author comparator, then returns control; it does not
   start a second repository-wide code-review phase.
7. **Apply the acceptance gates.** The controller runs the project comparator, then a fresh Codex
   reviewer inspects the exact candidate and reruns that comparator itself. That creates an
   immutable accepted checkpoint and immediately unlocks dependants while canonical integration
   continues in the background. If concurrent proofs touched the same file, a separate integration
   worktree preserves both histories and the comparator checks the combined result.
8. **Publish or revise.** Every accepted theorem is written to the wiki immediately and unlocks its
   dependants. A mathematical, isolated Lean, comparator, or Lean-review rejection is fed back into
   the latest natural-language proof at the appropriate upper level; it does not create another
   plan. A failure caused only by combining already accepted histories stays in `integrating` and
   enters a dedicated Codex repair plus machine/reviewer-comparator loop. It never invalidates or
   restarts the accepted NL proof and never sends false theorem-failure feedback to the parent.

## Requirements

- Humanize with the `hmz` command and the official `humanize1` flowverse installed.
- Lean projects should pin `leanprover/lean4:v4.33.0` in `lean-toolchain` when reproducing the
  current Lean-Eval experiment.
- Run at the root of a clean Lean git repository.
- Provide a comparator wrapper such as `tools/check-with-comparator.sh`.
- The comparator must exit zero and print the configured success marker.
- Use Codex for both declared roles. The two roles are separate agents and therefore keep
  worker and reviewer context independent.

The comparator is called once by the flow before review, then the reviewer is required to run
it again. It receives `HUMANIZE_NODE_ID`, `HUMANIZE_NODE_STATEMENT`,
`HUMANIZE_LEAN_FILES`, `HUMANIZE_RUN_DIR`, and `HUMANIZE_WIKI_DIR`. A repository that needs a
different comparator target for each generated lemma should use these values in its wrapper.

## Install

Install the flow directly into the user-flow directory:

```sh
git clone git@github.com:humanfia/math-lean-flow.git \
  ~/.humanize/flows/recursive_lean_prover
hmz check user/recursive_lean_prover
```

For an existing clone, update the installed flow with:

```sh
git -C ~/.humanize/flows/recursive_lean_prover pull --ff-only
hmz check user/recursive_lean_prover
```

## Run

First create a task file such as `PROBLEM.md`. It should state the exact theorem(s), the Lean file
that may be edited, any files that must not be inspected or changed, and any project-specific
acceptance rules. Internet access is enabled by the agent arguments below, but a task may impose a
narrower source policy.

Provide a project-specific comparator wrapper. It must return a nonzero status on rejection and
print the configured marker only after every required check succeeds. Adapt this outline to the
repository's real evaluator:

```sh
#!/usr/bin/env bash
set -euo pipefail
lake env lean Submission.lean
./tools/project-comparator "$HUMANIZE_NODE_ID"
printf '%s\n' 'Your solution is okay!'
```

The wrapper can use `HUMANIZE_NODE_ID`, `HUMANIZE_NODE_STATEMENT`,
`HUMANIZE_LEAN_FILES`, `HUMANIZE_RUN_DIR`, and `HUMANIZE_WIKI_DIR`. Never print the success marker
before the real evaluator succeeds.

Copy and edit the example config, especially `lean_target` and `comparator_command`:

```sh
cp ~/.humanize/flows/recursive_lean_prover/config.example.yaml ./recursive-proof.yaml
```

The settings most often changed are:

- `max_depth`: deepest recursive decomposition level; the root is depth 0.
- `max_children` and `max_nodes`: fan-out and total DAG bounds.
- `max_parallel_children`: number of dependency-ready nodes allowed to work concurrently.
- `natural_proof_attempts`: revisions per saved batch, not a total proof-attempt limit.
- `rlcr_rounds`: rounds in one official Lean RLCR invocation.
- `comparator_timeout: 21600`: six hours for each comparator execution.
- `lean_target`: project-relative candidate `.lean` file.
- `comparator_command`: argv-style command; it is not evaluated by a shell.

Then run both worker and reviewer on Codex:

```sh
hmz exec -f user/recursive_lean_prover -c recursive-proof.yaml \
  -a cli=codex,model=gpt-5.6-sol,effort=max,permission=auto,web_search=on \
  -a cli=codex,model=gpt-5.6-sol,effort=max,permission=auto,web_search=on \
  "$(cat PROBLEM.md)"
```

Both roles use `permission=auto`: RLCR's plan-integrity guards operate on permission requests,
and a Lean comparator may need to write build artifacts. The reviewer prompt forbids edits and
the reviewer remains a separate Codex agent with independent sessions.

Use `Ctrl-C` to stop only this foreground supervisor. To resume, run the same `hmz exec` command
with the same task text, config, and repository. The flow reuses the durable run, accepted proof
nodes, Git branches, wiki pages, and the latest rejected natural-language draft. Do not use a broad
`pkill` when other experiments share the machine.

## Observe

At startup the flow prints its run directory. In a second terminal:

```sh
run_dir="$(cat .humanize/recursive-lean-prover/LATEST)"
watch -n 1 "sed -n '1,220p' \"$run_dir/DAG.md\""
```

The same directory contains `dag.json` and `dag.mmd`. Each problem workspace owns a wiki indexed
at `.humanize/math-wiki/README.md`. A theorem is published as soon as that node passes its
controller comparator and the fresh reviewer's independent rerun; publication does not wait for
the root theorem or the rest of the problem. Pages include the natural proof, frozen scaffold,
Lean source, recursion level, and comparator evidence.

The Mermaid diagram uses one line style and one direction convention everywhere: every solid arrow
`A --> B` means **A depends on B**, so B must be proved before A can finish. A parent theorem points
to each theorem created by its decomposition, and a theorem points to every explicit prerequisite
listed in `depends_on`. A node can therefore be a decomposition leaf while still pointing to an
upstream prerequisite. The node label and status table say `dependency-ready` or list the exact
blocking prerequisite.

## Review gates

- Direct planning performs an input relevance check and one pre-candidate analysis. It skips
  candidate convergence review and plan revision.
- A natural-language reviewer runs once the author reports no unresolved gaps. Rejection revises
  the latest proof draft indefinitely; it never regenerates the plan.
- A decomposition reviewer checks every proposed child statement, exact frozen Lean type, and
  dependency edge after the prose proof passes.
- The official RLCR implementation loop reviews every Lean worker round against the current audited
  DAG node, frozen type, accepted dependency list, and author comparator. Once that implementation
  reviewer accepts the candidate, RLCR returns control immediately. The bridge deliberately leaves
  RLCR's optional generic code-review base blank: enabling that second repository-wide review would
  duplicate the controller review, would not enforce the exact comparator, and could reopen accepted
  child histories. The exact post-overlay base remains recorded in the node configuration for audit.
- The controller runs the comparator with a default six-hour timeout. Only after that passes does
  a fresh Lean reviewer inspect the exact candidate and personally rerun the same comparator.
- If independently accepted histories must be combined, integration runs the comparator again on
  the merged candidate before advancing the problem branch. A failed combined check retains the
  accepted proof and runs integration-only repair followed by another machine comparator and a
  fresh reviewer comparator; it does not restart the theorem or NL proof.
- A decomposed parent may start from comparator-approved child candidates while they integrate;
  its isolated worktree overlays those exact commits and rechecks the combination. The root cannot
  become `proved` until all descendant integration gates and its own final comparator contract pass.
- Every node records a frozen proof-base commit. Kernel-checked definitions and helper lemmas at
  that base may be reused as library infrastructure; the child list governs only post-base
  candidate overlays and is not an exhaustive theorem allowlist.
- Any mathematical rejection returns to the latest natural-language proof. The full outer
  comparator/reviewer pass freezes the candidate, marks it `integrating`, publishes it, and
  unlocks dependants. Only the subsequent canonical integration gate marks the node `proved`.

## DAG scheduling

On resume, the scheduler scans the complete persisted DAG. Every node whose dependencies have
passed both isolated comparator gates enters the global frontier together, up to
`max_parallel_children`. Accepted `integrating` nodes unlock dependants immediately; their exact
candidate commits are overlaid into the dependant's isolated worktree. Planning, natural-language
proof, decomposition, Lean implementation, comparator passes, parent proving, and serialized
integration can therefore overlap. Same-file reconciliations are performed in a separate
integration worktree and comparator-checked before the canonical branch advances. Any
reconciliation failure remains in an integration-only repair loop; the accepted node branch,
scaffold, NL proof, comparator result, reviewer result, theorem identity, and wiki page remain
immutable checkpoints. Re-decomposition reuses an existing accepted Lean theorem by name at any
depth instead of creating an `-a2` copy or proving it again. The root still waits for every
descendant integration future before final acceptance.

## Safety and stopping

The official RLCR loop commits Lean changes as it works and runs coding agents with Humanize's
permission prompting disabled. Plans, DAG state, comparator logs, and the wiki stay below
`.humanize/` so they do not enter RLCR's git-clean gate. A stopped run is resumable: running the
same task again in the same repository reuses its durable run directory, already approved wiki
pages, and nested RLCR state.
