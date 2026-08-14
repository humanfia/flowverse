"""What every test here shares: the gate on the agent-driving ones.

`pytest_addoption` is honoured only in a root conftest, so `--run-agents` lives here rather
than beside the tests it gates; the `agent` marker it keys on is registered below.

The import path the tests need is `flows/`, and that is set where the rest of the run is, in
`pyproject.toml`: humanize runs a flow by path with its own directory on `sys.path` while it
does, so a flow may import what came with it -- `_humanize1`, here -- and a test that imports
the flow needs that directory for the same reason.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "agent: end-to-end test that drives a real coding agent binary"
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-agents",
        action="store_true",
        default=False,
        help="also run the end-to-end tests that drive real coding agents",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--run-agents"):
        return
    skip = pytest.mark.skip(
        reason="needs --run-agents (drives real agents, costs tokens)"
    )
    for item in items:
        if "agent" in item.keywords:
            item.add_marker(skip)
