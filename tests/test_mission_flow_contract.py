from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hmz.flows import configures, drives, held, loaded, offered, resumes
from hmz.flows.skills import brought


def test_mission_flow_is_a_separate_fixed_seven_agent_entry() -> None:
    flows = Path(__file__).parents[1] / "flows"
    mission = flows / "parallel_flame_chase_mission" / "__init__.py"
    expected = (
        "coordinator",
        "lane_1_actor_a",
        "lane_1_actor_b",
        "lane_2_actor_a",
        "lane_2_actor_b",
        "lane_3_actor_a",
        "lane_3_actor_b",
    )
    assert drives(mission) == expected
    assert resumes(mission)
    config = configures(mission)
    assert config is not None
    assert config.__name__ == "Config"
    assert set(config.model_fields) == {
        "rest_seconds",
        "resume_mode",
        "global_audit_hours",
        "mission_deadline_hours",
        "max_turns_without_outcome",
        "interrupt_grace_seconds",
        "external_events",
    }
    assert [flow.name for flow in held(mission)] == [""]
    assert [skill.name for skill in brought(mission.parent)] == [
        "parallel-flame-chase-mission"
    ]
    offered_names = offered(flows)
    assert "parallel_flame_chase" in offered_names
    assert "parallel_flame_chase_mission" in offered_names
    assert "_parallel_flame_chase" not in offered_names
    assert {path.name for path in mission.parent.glob("*.py")} == {"__init__.py"}


def test_base_flow_does_not_load_the_mission_package() -> None:
    flows = Path(__file__).parents[1] / "flows"
    script = (
        f"import sys; sys.path.insert(0, {str(flows)!r}); "
        "import parallel_flame_chase; "
        "assert not any(name.startswith('parallel_flame_chase_mission') "
        "for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_mission_entry_runs_after_directory_loading() -> None:
    mission = Path(__file__).parents[1] / "flows" / "parallel_flame_chase_mission"
    namespace = loaded(mission)
    calls: list[tuple[object, ...]] = []
    namespace["run"].__globals__["execute"] = lambda *args: calls.append(args)

    namespace["run"](None, "task", None, None)

    assert len(calls) == 1
    assert calls[0][1:] == ("task", namespace["Config"](), None)
