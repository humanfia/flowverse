---
name: parallel-flame-chase-git-pr
description: Operate one lane in the receipt-fast-path Git/PR flow.
---

# Parallel Flame Chase Git/PR

Use the role and workspace map in the turn prompt as the authority. This flow never authorizes a
real remote release, deployment, competition submission, purchase, or message.

For a research lane, work only in its assigned clone. Start experiments on `lane-N/<experiment>`
branches from the latest `origin/main`. Commit and push before using the run-local `pfc` command.
Record the exact official evaluator with `pfc evaluate -- <command>`, open a draft with `pfc pr
open --draft`, and mark the PR ready with its successful receipt. A ready head is immutable. A
newer ready head from the same lane supersedes its older queued candidate.

The runtime validates receipt artifacts and the frozen commit/tree, extracts the official cycle
count, and selects the lowest-cycle queued candidate. It publishes the exact evaluated tree only
when that score improves main. Do not attempt manual integration: there is no model review or
second evaluator run on the fast path.
