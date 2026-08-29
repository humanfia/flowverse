# Parallel Flame Chase Mission Lite

`parallel_flame_chase_mission_lite` is an additive seven-agent experiment. Its public planning
and audit role is named `orchestrateor`; the six lane actors retain the ordinary fixed topology.

Unlike the original Mission flow, a `deliverable_ready` outcome does not itself cause a global
audit. Every terminal outcome still receives a targeted audit so the lane can obtain a successor
mission and private artifacts can be accepted for integration. When any lane publishes a valid
candidate that strictly improves the primary `shared/leaderboard.json` best, that audit becomes
global. Explicit periodic reviews, objective revisions, and global external requests retain their
configured scopes.

```console
hmz exec -f ./flows/parallel_flame_chase_mission_lite \
  -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -c ./flows/parallel_flame_chase_mission_lite/examples/lite.yaml \
  "$(cat TASK.md)"
```
