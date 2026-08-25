# Lane collaboration

## Fixed topology

The coordinator plans once and does not return. Three lanes run concurrently; each has actors A
and B alternating in fresh sessions.

- Lane 1 alone edits and integrates the original source.
- Lanes 2 and 3 edit only their private snapshots.
- A private snapshot is not a shared branch and is never merged implicitly.

Read the repository at the start of every turn. Conversational memory does not cross actors or
turns. Leave the next actor a coherent workspace, tests, and a precise report.

## Reports and evidence

Return facts, not forecasts. Select:

- `progress` when material local work landed and another turn can answer a concrete question;
- `deliverable_ready` only when the explicit package exists and can be reconstructed;
- `no_result` when a direction was tested and exhausted with useful negative evidence;
- `blocked` only for a concrete blocker beyond ordinary unfinished work.

The runtime alone records `turn_failed`. Include changed files or artifacts, commands/tests and
their observed results, risks, and the single most useful next step. Other lanes receive reports
at least once; they acknowledge a batch only after their own valid turn lands, so repeated reports
can be legitimate redelivery.

## Artifact handoff

A deliverable declares regular files relative to the lane's assigned artifact root. Do not use
absolute paths, parent traversal, symlinks, implicit workspace state, or a prose-only claim. Keep
the package minimal but reconstructable. Integration notes must say prerequisites, application
steps, validation performed, known failure modes, and what Lane 1 should compare against.

Lane 1 consumes only runtime-validated packages. Recheck their assumptions in the original source;
integration is new work, not blind copying. Keep unrelated source work intact.

## Checkpoints

The shared checkpoint path is optional recovery evidence for a turn likely to be interrupted.
Write the exact identity supplied in the prompt and a truthful partial or terminal report. A
checkpoint does not announce completion, trigger a control transition, replace the final
structured report, or carry across a different turn generation.
