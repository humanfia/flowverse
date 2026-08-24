from __future__ import annotations

from pathlib import Path

from hmz.flows import configures, drives, held, offered, resumes
from hmz.flows.skills import brought


def test_public_flow_declares_fixed_seven_agent_topology() -> None:
    flows = Path(__file__).parents[1] / "flows"
    base = flows / "parallel_flame_chase" / "__init__.py"
    expected = (
        "coordinator",
        "lane_1_actor_a",
        "lane_1_actor_b",
        "lane_2_actor_a",
        "lane_2_actor_b",
        "lane_3_actor_a",
        "lane_3_actor_b",
    )
    assert drives(base) == expected
    assert resumes(base)
    config = configures(base)
    assert config is not None
    assert config.__name__ == "Config"
    assert set(config.model_fields) == {"rest_seconds", "resume_mode"}
    assert [flow.name for flow in held(base)] == [""]
    assert [skill.name for skill in brought(base.parent)] == ["parallel-flame-chase"]
    offered_names = offered(flows)
    assert "parallel_flame_chase" in offered_names
    assert "_parallel_flame_chase" not in offered_names
