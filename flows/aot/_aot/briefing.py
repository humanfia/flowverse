"""What this humanize lets a flow declare, read off `hmz.flows` itself.

The briefing a writer drafts a spec against, and the names a spec's needs are held to: every
agent mixin with the harnesses that serve it, and every environment mixin.
"""

import inspect

from hmz import flows

AGENT_HOOKS = (
    "on_session_start",
    "on_user_prompt_submit",
    "on_pre_tool_use",
    "on_notification",
    "on_stop",
    "on_session_end",
)


def _named(suffix: str) -> list[type]:
    return [getattr(flows, name) for name in flows.__all__ if name.endswith(suffix)]


def harnesses() -> tuple[str, ...]:
    return tuple(kind.value for kind in flows.HARNESS_AGENTS)


def capabilities() -> dict[str, tuple[str, ...] | None]:
    """Every capability by name: the harnesses serving an agent mixin, None for an env one."""
    served: dict[str, tuple[str, ...] | None] = {
        mixin.__name__: tuple(
            kind.value
            for kind, protocol in flows.HARNESS_AGENTS.items()
            if mixin in protocol.__mro__
        )
        for mixin in _named("AgentMixin")
    }
    served.update((mixin.__name__, None) for mixin in _named("EnvMixin"))
    return served


def _about(one: type) -> str:
    return (inspect.getdoc(one) or "").split("\n\n")[0].replace("\n", " ")


def briefed() -> str:
    every = set(harnesses())
    lines = [
        "Every agent, on every harness, can: `spawn(env=...)` a session, `run(prompt, "
        "session=...)` a turn answering text, or `run(..., output_schema=Model)` answering "
        "an instance of a pydantic model, `fork(session, env=...)`, `derive(permission=..., "
        "skills=...)` a narrower copy, and hang hooks with "
        + ", ".join(f"`{one}`" for one in AGENT_HOOKS)
        + ".",
        f"Harnesses: {', '.join(sorted(every))}.",
        "",
        "Agent capabilities -- declared by subclassing a role's type, as in "
        "`class Coder(Agent, GoalCommandAgentMixin): ...`; each one narrows which harnesses "
        "can fill the role:",
    ]
    for name, served in capabilities().items():
        if served is None:
            continue
        where = (
            "every harness"
            if set(served) >= every
            else f"only {', '.join(sorted(served))}"
        )
        lines.append(f"- {name}: {_about(getattr(flows, name))} Runs on {where}.")
    lines += [
        "",
        "Environment capabilities -- declared on the workspace's type, as in "
        "`class Workspace(LocalEnv, ShellEnvMixin, FilesEnvMixin): ...`:",
    ]
    for name, served in capabilities().items():
        if served is None:
            lines.append(f"- {name}: {_about(getattr(flows, name))}")
    lines += [
        "",
        "The person outside the run is a role typed `Outworlder`, which nobody configures: "
        "under `hmz exec` nobody is there, and every question put to it answers its "
        "defaults.",
    ]
    return "\n".join(lines)
