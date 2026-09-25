#!/bin/sh
# Git PR Lite over the directory this is run in, with every param spelled out at its default.
exec hmz exec -f official/parallel_flame_chase_git_pr \
  -a orchestrator=codex/gpt-5.6-sol:max \
  -a lane_1_actor_a=codex/gpt-5.6-sol:max,lane_1_actor_b=claude/claude-opus-5:max \
  -a lane_2_actor_a=claude/claude-opus-5:max,lane_2_actor_b=codex/gpt-5.6-sol:max \
  -a lane_3_actor_a=claude/claude-opus-5:max,lane_3_actor_b=codex/gpt-5.6-sol:max \
  -p rest_seconds=1.0,resume_mode=auto,confirm_large_workspace_copies=false \
  -p workspace_file_warning_threshold=5000 \
  -p workspace_copy_warning_threshold_bytes=1073741824 \
  -b duration=12h \
  "$(cat TASK.md)"
