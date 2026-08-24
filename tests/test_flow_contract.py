from __future__ import annotations

from pathlib import Path

from hmz.flows import configures, drives, resumes
from hmz.flows.skills import brought


def test_both_public_flows_declare_fixed_seven_agent_topology() -> None:
    path = Path(__file__).parents[1] / "flows" / "parallel_flame_chase" / "__init__.py"
    expected = (
        "coordinator",
        "lane_1_actor_a",
        "lane_1_actor_b",
        "lane_2_actor_a",
        "lane_2_actor_b",
        "lane_3_actor_a",
        "lane_3_actor_b",
    )
    assert drives(path) == expected
    assert drives(f"{path}:mission") == expected
    assert resumes(path)
    assert resumes(f"{path}:mission")
    assert configures(path).__name__ == "Config"
    assert configures(f"{path}:mission").__name__ == "MissionConfig"
    assert [skill.name for skill in brought(path.parent)] == ["parallel-flame-chase"]
