# Parallel Flame Chase Mission Control

`parallel_flame_chase_mission_control` is an additive experiment whose first public role is named
`orchestrateor`. On a fresh run that role creates both the three-lane portfolio and a strict audit
policy after inspecting task constraints. The policy is persisted and reused on resume.

The policy explicitly controls shared-best updates, terminal outcomes, stalls, deadlines, paired
actor failures, invalid deliverables, external reviews, objective revisions, and periodic review.
Each condition can be disabled, lane-targeted, global, or left at its original Mission scope where
the condition supports that choice. Overlapping conditions use the strongest enabled scope.

If a terminal audit is disabled, the runtime records the suppressed outcome and continues the same
mission with a fresh deadline. That preserves throughput, but a private-lane package cannot enter
Lane 1's integration queue without an acceptance audit. Choosing original scopes for every
original trigger, disabling the additional shared-best rule, and selecting a six-hour period
reproduces the original Mission trigger policy.

```console
hmz exec -f ./flows/parallel_flame_chase_mission_control \
  -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -c ./flows/parallel_flame_chase_mission_control/examples/control.yaml \
  "$(cat TASK.md)"
```
