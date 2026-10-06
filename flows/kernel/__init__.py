"""Kernel optimization with a local or KCoral evaluator and a fresh session per turn."""

import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Literal

from hmz.flows import (
    Agent, AgentCollection, EnvCollection, FlowContext, FlowParams, LocalEnv, flow, load,
)


class Agents(AgentCollection):
    agent: Agent


class Envs(EnvCollection):
    workspace: LocalEnv


class Params(FlowParams):
    backend: Literal["local", "kcoral"] = "local"
    url: str = ""
    evaluator: str = "python evaluate.py"


@flow(agents=Agents, envs=Envs, params=Params, resumable=True)
async def kernel(
    task: str, *, agents: Agents, envs: Envs, params: Params, ctx: FlowContext
) -> None:
    """Run an ordinary kernel task, giving the agent its configured evaluator command."""
    runner = Path(__file__).parents[2] / "tools" / "kernel_benchmark.py"
    prefix = [sys.executable, str(runner), "--backend", params.backend]
    if params.backend == "kcoral":
        if shutil.which("kcoral") is None:
            raise ValueError("Install the KCoral client on PATH before running this flow")
        url = params.url or os.environ.get("KCORAL_URL", "")
        if not url.strip():
            raise ValueError("Set -p url=http://SERVER:PORT or KCORAL_URL")
        prefix.extend(["--url", url])
    evaluator = shlex.split(params.evaluator)
    if not evaluator:
        raise ValueError("evaluator must name a command")
    task += (
        f"\n\nBenchmark backend: {params.backend}. Run the existing evaluator with:\n"
        f"{shlex.join([*prefix, '--', *evaluator])}\n"
        "The command runs from the current project directory. For other benchmark "
        f"commands, use the same prefix: {shlex.join(prefix)} -- COMMAND [ARGS].\n"
        "Keep the evaluator's correctness and timing rules unchanged. A nonzero exit "
        "is a failed trial; retain the measured output and source. KCoral uploads "
        "working files using .gitignore and skips .git, virtualenvs and tool caches. "
        "It uses the worker's installed dependencies; do not run GPU work locally "
        "when KCoral is selected. For remote result files, add --fetch PATH "
        "--out NEW_DIRECTORY before --. Use a new directory under .humanize/ "
        "for downloads within workspace permissions, creating the parent if needed. "
        "The server remains user-managed."
    )
    loop = load("ralph_loop")
    await loop(task, agents=agents, envs=envs, params=loop.expected_params())
