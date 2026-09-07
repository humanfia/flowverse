from __future__ import annotations

from pathlib import Path

from hmz.flows import configures, drives, offered, resumes
from hmz.flows.skills import brought


def test_only_canonical_git_pr_lite_is_offered() -> None:
    flows = Path(__file__).parents[1] / "flows"
    names = set(offered(flows))
    git_pr = flows / "parallel_flame_chase_git_pr" / "__init__.py"

    assert "parallel_flame_chase" in names
    assert "parallel_flame_chase_git_pr" in names
    assert drives(git_pr) == (
        "orchestrateor",
        "lane_1_actor_a",
        "lane_1_actor_b",
        "lane_2_actor_a",
        "lane_2_actor_b",
        "lane_3_actor_a",
        "lane_3_actor_b",
    )
    assert resumes(git_pr)
    assert [skill.name for skill in brought(git_pr.parent)] == [
        "parallel-flame-chase-git-pr"
    ]

    config = configures(git_pr)
    assert config is not None
    assert set(config.model_fields) == {
        "rest_seconds",
        "resume_mode",
        "confirm_large_workspace_copies",
        "workspace_file_warning_threshold",
        "workspace_copy_warning_threshold_bytes",
        "git_pr_enabled",
        "global_knowledge_enabled",
        "experiment_memory_enabled",
        "token_efficient_enabled",
        "main_update_monitor_enabled",
    }
    assert config().model_dump() == {
        "rest_seconds": 1.0,
        "resume_mode": "auto",
        "confirm_large_workspace_copies": False,
        "workspace_file_warning_threshold": 5_000,
        "workspace_copy_warning_threshold_bytes": 1024**3,
        "git_pr_enabled": True,
        "global_knowledge_enabled": False,
        "experiment_memory_enabled": False,
        "token_efficient_enabled": False,
        "main_update_monitor_enabled": False,
    }

    removed = {
        "parallel_flame_chase_git_pr_adaptive_eval",
        "parallel_flame_chase_git_pr_main_monitor",
        "parallel_flame_chase_git_pr_token_efficient",
        "parallel_flame_chase_git_pr_token_efficient_main_monitor",
        "parallel_flame_chase_mission",
        "parallel_flame_chase_mission_control",
        "parallel_flame_chase_mission_lite",
        "parallel_flame_chase_report_share",
        "parallel_ralph_git_pr_2way",
        "parallel_ralph_git_pr_4way",
    }
    assert removed.isdisjoint(names)
