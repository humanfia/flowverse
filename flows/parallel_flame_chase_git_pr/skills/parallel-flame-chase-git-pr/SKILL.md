---
name: parallel-flame-chase-git-pr
description: Operate one lane in the receipt-fast-path Git/PR and compact-knowledge flow.
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

When knowledge is enabled, the prompt includes up to four recent evaluator-backed success cards.
Query it with `pfc knowledge search` only when useful, inspect relevant IDs with `pfc knowledge
get`, and cite relied-on IDs in the final LaneReport. The hot digest retains at most 12 cards;
ordinary reports remain the complete evidence archive.

When the runtime prompt enables Experiment Memory Lite, use its `experiment check`, `begin`, and
`finish` protocol before and after a bounded experiment. Treat covered work as a warning rather
than a permanent ban; reopen it only with the structured reason required by the runtime. Cite the
same exact evaluator receipt in both the experiment record and PR when Git/PR is enabled. In a
standalone-memory cell, do not create or assume a central Git repository: the runtime binds
receipts to a content hash of the frozen task paths instead.
