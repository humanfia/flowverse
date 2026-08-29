# Audit planning

Inspect TASK.md, evaluator helpers, configuration, and relevant repository documentation before
choosing audit pressure. Record evidence and unknowns for:

- candidate-submission and evaluator-call limits;
- evaluator runtime and feedback latency;
- total experiment duration;
- whether integration is cheap and reversible or expensive and fragile;
- how much useful work an audit interruption is likely to discard.

Use longer mission deadlines and fewer completion audits when evaluations are slow or scarce. Use
targeted audits for lane-local evidence. Reserve global audits for evidence that can change the
whole portfolio, such as a true primary shared-best refresh or a material objective revision.

Decide every condition exactly once. `off` suppresses the audit. For a terminal outcome, suppression
automatically continues the same mission and retains the outcome in its state, but does not enqueue
a private deliverable for Lane 1. `targeted` selects the event lane, `global` selects all lanes, and
`original` preserves the original Mission scope.

The original Mission trigger behavior is represented by:

- `shared_best_updated`: off;
- every original terminal, stall, deadline, failure, invalid-deliverable, external-review, and
  objective-revision condition: original;
- `periodic_review`: original with 6 hours.

Choose that policy only when the task evidence supports the original interruption rate. Never
invent a quota or evaluator duration merely to justify a preferred cadence.
