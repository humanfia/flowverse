"""The backend selection reaches an ordinary agent without new task files."""

import sys

import pytest
from hmz.runtime.flowing.fakes import FakeAgentDriver, run_fake

from tests.kit import loaded


@pytest.mark.asyncio
async def test_kernel_flow_supplies_the_selected_evaluator(tmp_path, monkeypatch):
    loaded("ralph_loop")
    monkeypatch.setattr(sys.modules["ralph_loop"], "PAUSE", 0)
    flow = loaded("kernel")
    monkeypatch.setattr(sys.modules["kernel"].shutil, "which", lambda name: "/bin/kcoral")
    agent = FakeAgentDriver(reply="")
    await run_fake(
        flow,
        "optimize the existing project",
        agents={"agent": agent},
        params=flow.expected_params(
            backend="kcoral", url="http://worker:8000", evaluator="python check.py --size 42"
        ),
        journal=tmp_path / "run.jsonl",
    )
    assert len(agent.sessions) == 3
    prompt = agent.sessions[0].prompts[0]
    assert "optimize the existing project" in prompt
    assert "--backend kcoral --url http://worker:8000 -- python check.py --size 42" in prompt
    assert "--bundle" not in prompt


@pytest.mark.asyncio
async def test_missing_remote_setup_is_reported_before_a_turn(tmp_path, monkeypatch):
    flow = loaded("kernel")
    monkeypatch.setattr(sys.modules["kernel"].shutil, "which", lambda name: None)
    agent = FakeAgentDriver(reply="")
    with pytest.raises(ValueError, match="KCoral client"):
        await run_fake(
            flow,
            "optimize",
            agents={"agent": agent},
            params=flow.expected_params(backend="kcoral"),
            journal=tmp_path / "run.jsonl",
        )
    assert agent.sessions == []
