# Parallel Flame Chase Report Share

`parallel_flame_chase_report_share` is an additive report-only experiment. It retains the original
three isolated lanes and has no Mission audit controller. Its first public role is named
`orchestrateor` and plans once.

Every fresh Lane A/B actor receives the immediately preceding report from its own lane in addition
to unread reports from the other two lanes. The prompt treats that report as claims rather than
authority: the new actor must check mission identity, inspect cited files, rerun proportionate
tests, retain correct work, and repair stale or incorrect claims. The original
`parallel_flame_chase` continues to omit this same-lane injection.

```console
hmz exec -f ./flows/parallel_flame_chase_report_share \
  -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -a claude/claude-opus-5:max -a codex/gpt-5.6-sol:max \
  -c ./flows/parallel_flame_chase_report_share/examples/report-share.yaml \
  "$(cat TASK.md)"
```
