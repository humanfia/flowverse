from __future__ import annotations

from hmz.flows import (
    Agent,
    AgentCollection,
    BashEnvMixin,
    EnvCollection,
    FilesEnvMixin,
    LocalEnv,
    SteeringAgentMixin,
)


class Worker(Agent, SteeringAgentMixin):
    pass


class Workspace(LocalEnv, BashEnvMixin, FilesEnvMixin):
    pass


class Coder(AgentCollection):
    coder: Worker


class Cleaner(AgentCollection):
    cleaner: Worker


class Here(EnvCollection):
    workspace: Workspace
