from __future__ import annotations

from pathlib import Path

import pytest
from hmz.flows import FlowNotFound, Outworlder, Permission, PermissionKind, load

FLOWS = Path(__file__).parents[1] / "flows"
BASE = FLOWS / "parallel_flame_chase"
ACTORS = (
    "coordinator",
    "lane_1_actor_a",
    "lane_1_actor_b",
    "lane_2_actor_a",
    "lane_2_actor_b",
    "lane_3_actor_a",
    "lane_3_actor_b",
)


def test_public_flow_declares_fixed_seven_agent_topology() -> None:
    flow = load(str(BASE))
    declared = flow.describe()

    assert declared.name == "parallel_flame_chase"
    assert not declared.hidden
    assert declared.resumable
    assert flow.resumable
    assert tuple(role.name for role in declared.agents) == (*ACTORS, "human")
    full = Permission(
        local=PermissionKind.ALL,
        user=PermissionKind.ALL,
        system=PermissionKind.ALL,
        online=PermissionKind.ALL,
    )
    for role in declared.agents[:-1]:
        assert role.required
        assert not role.auto
        assert role.harness is None
        assert role.capabilities == frozenset()
        assert role.permission == full
        assert role.skills == ("parallel-flame-chase",)
    human = declared.agent("human")
    assert human is not None
    assert human.auto
    assert human.declared is Outworlder
    (workspace,) = declared.envs
    assert workspace.name == "workspace"
    assert workspace.auto
    assert {one.__name__ for one in workspace.capabilities} >= {
        "ShellEnvMixin",
        "FilesEnvMixin",
        "TemporaryClonedDirEnvMixin",
        "ScratchDirEnvMixin",
    }
    params = declared.params
    assert params.__name__ == "Params"
    assert set(params.model_fields) == {
        "rest_seconds",
        "resume_mode",
        "confirm_large_workspace_copies",
        "workspace_file_warning_threshold",
        "workspace_copy_warning_threshold_bytes",
    }
    assert params().model_dump() == {
        "rest_seconds": 1.0,
        "resume_mode": "auto",
        "confirm_large_workspace_copies": False,
        "workspace_file_warning_threshold": 5_000,
        "workspace_copy_warning_threshold_bytes": 1024**3,
    }


def test_turns_are_hidden_subflows_of_the_same_module() -> None:
    for name in ("plan", "lane_turn"):
        sub = load(f"{BASE}:{name}").describe()
        assert sub.hidden
        assert not sub.resumable
        assert all(role.skills == ("parallel-flame-chase",) for role in sub.agents)
        assert [role.name for role in sub.envs] == ["place"]


def test_bare_ref_is_the_flow_and_the_private_package_is_not_one() -> None:
    assert load(str(BASE)).describe().ref == "parallel_flame_chase:parallel_flame_chase"
    with pytest.raises(FlowNotFound):
        load(f"{BASE}:_parallel_flame_chase")
