# Mission and audit protocol

## Missions

A mission is a bounded, falsifiable unit of research, implementation, validation, or integration.
Work toward its success criteria and information question. Architectural diversity matters: a
redirect must change the material hypothesis, method, or change scale rather than rename the same
work.

Terminal lane outcomes trigger review:

- Lane 2 or 3 `deliverable_ready`, `no_result`, or `blocked`: targeted audit of that lane.
- Lane 1 `deliverable_ready`: global audit of all lanes because integrated source may change the
  whole portfolio.
- A deadline, repeated progress without an outcome, or two consecutive actor failures: targeted
  audit.
- The configured periodic cadence or an objective revision: global audit.

## Scoped interruption

Only audit targets pause. Non-target lanes continue unless an audit escalates to global. The
runtime first asks a running target to checkpoint and stop at a safe boundary. After the configured
grace it closes that target session; it never scans or kills unrelated processes. There are no
actor defense turns.

Audit evidence is the latest validated report, exact-identity checkpoint when present, trigger
events, active missions, queue, manifest, and—only for a global audit—an immutable source snapshot.
Decide that packet, not speculation or earlier conversational context.

## Decisions

Return one verdict for every exact target and no other lane:

- `continue`: the same mission remains the best information-bearing next step.
- `redirect`: reject it and provide a materially different mission.
- `accept`: permitted only for a validated `deliverable_ready` outcome.

Accepting Lane 2 or 3 requires both a reconstructable Lane 1 integration directive and a new
research mission for the accepted lane. The package enters Lane 1's priority queue; Lane 1 starts
it only at a natural turn boundary, pausing its research mission. Once an integration is accepted,
Lane 1 resumes that paused mission unless the decision deliberately supplies a successor.

Bind every answer to the exact `audit_id` and `revision`. New evidence can supersede an in-flight
revision. A stale or semantically invalid decision is never guessed at or replaced by a runtime
fallback: targets stay paused and a fresh coordinator retries after bounded backoff.
