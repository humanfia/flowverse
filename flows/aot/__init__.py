"""AOT -- the flow that writes a flow: a description in, a loaded, smoke-run and reviewed flow out.

    hmz exec -f aot -a writer=claude/claude-opus-5:high -a critic=codex/gpt-5.6-sol:high \
        -b cost=10 "two agents take turns until a reviewer says it is done"

The writer draws a spec from the description, then drafts the flow in a scratch directory.
Each draft is loaded through humanize's engine and run on fakes in three worlds -- an agent
that never says done, one that says done at once, one that answers nothing -- and must end on
its own in every one; a critic then reads it fresh. A refusal goes back to the writer word
for word, for up to `repairs` rounds. What passes lands whole in `.humanize/flows` (or
`~/.humanize/flows` with `-p into=user`), never over a flow that is already there.
"""

import keyword
import os
import sys
import uuid
from typing import Literal

from _aot import briefing, gates, prompts
from hmz.flows import (
    Agent,
    AgentCollection,
    EnvCollection,
    EnvError,
    FilesEnvMixin,
    FlowContext,
    FlowParams,
    HarnessError,
    LocalEnv,
    Outworlder,
    Permission,
    PermissionKind,
    ScratchDirEnvMixin,
    Session,
    ShellEnvMixin,
    flow,
)
from hmz.runtime.flowing.verses import MINE
from pydantic import BaseModel, Field

RENAMES = 2
RENAME = "import os, sys; os.rename(sys.argv[1], sys.argv[2])"


class Writer(Agent):
    _skills = ("writing-flows",)


class Critic(Agent):
    _permission = Permission(local=PermissionKind.READ)
    _skills = ("writing-flows",)


class Compiling(AgentCollection):
    writer: Writer
    critic: Critic
    human: Outworlder


class Workspace(LocalEnv, ShellEnvMixin, FilesEnvMixin, ScratchDirEnvMixin): ...


class Envs(EnvCollection):
    workspace: Workspace


class Params(FlowParams):
    name: str = Field(
        default="",
        description="what to call the flow that lands, or '' to take the name the spec "
        "derives from the description",
    )
    into: Literal["local", "user"] = Field(
        default="local",
        description="where it lands: `local` is this project's .humanize/flows, `user` "
        "the one in your home directory",
    )
    repairs: int = Field(
        default=3,
        ge=0,
        le=6,
        description="how many rounds of repair the writer is given after its first draft",
    )
    strict: bool = Field(
        default=False,
        description="whether every warning sends a draft back, rather than only what "
        "blocks -- a draft that does not end on its own always blocks, whatever this says",
    )
    seconds: float = Field(
        default=60.0,
        gt=0,
        description="the clock each smoke run of a draft on fakes is held to, a world "
        "apiece",
    )


class Seat(BaseModel):
    """One agent the compiled flow will drive.

    Every field required, here and in every shaped answer of this flow: a backend that
    holds a model to a strict schema refuses one whose fields have defaults, and the
    compiler must compile on any backend that shapes.
    """

    model_config = {"extra": "forbid"}

    name: str = Field(description="its role, snake_case, for what it does")
    person: bool = Field(
        description="true only for the person outside the run, an Outworlder whom "
        "nobody configures",
    )
    mixins: list[str] = Field(
        description="the agent capabilities this role is declared with, each named "
        "exactly as the briefing names it -- e.g. GoalCommandAgentMixin -- and [] for a "
        "role that needs none",
    )


class Setting(BaseModel):
    """One param the compiled flow takes."""

    model_config = {"extra": "forbid"}

    name: str = Field(description="the field's name, snake_case")
    kind: Literal["number", "text", "switch"] = Field(description="what it holds")
    default: str = Field(description="the default, written out -- '6', 'off'")
    about: str = Field(description="one line saying what it does, for whoever sets it")


class Ending(BaseModel):
    """One way the compiled flow ends."""

    model_config = {"extra": "forbid"}

    by: Literal["once", "rounds", "verdict"] = Field(
        description="what ends it: a single pass with no loop, a cap on the rounds, or "
        "an agent's shaped verdict -- which always travels with a cap besides"
    )
    bound: str = Field(
        description="the bound, written out -- 'one pass', '6 rounds', 'reviewer says "
        "done, within 6 rounds'"
    )


class Spec(BaseModel):
    """What the flow is to be, drawn from the description before anything is written."""

    model_config = {"extra": "forbid"}

    about: str = Field(description="one line saying what the flow does")
    name: str = Field(description="what to call it, snake_case")
    seats: list[Seat] = Field(
        description="every agent it drives, the person included if it talks to one"
    )
    settings: list[Setting] = Field(
        description="the params it takes, [] for a flow that takes none"
    )
    endings: list[Ending] = Field(
        description="every way it ends -- at least one, and never a verdict alone"
    )
    needs: list[str] = Field(
        description="every capability it declares, agent and environment alike, each "
        "named exactly as the briefing names it -- e.g. GoalCommandAgentMixin, "
        "ShellEnvMixin -- and nothing the briefing does not name"
    )
    plan: str = Field(description="how the flow will work, a short paragraph")


class Review(BaseModel):
    """What the critic answers, having read the draft fresh against the spec."""

    model_config = {"extra": "forbid"}

    approved: bool = Field(
        description="true only if the draft does what the spec says, keeps to the "
        "writing-flows contract, and you would run it on a repository of your own"
    )
    notes: str = Field(
        description="what to tell the writer: what is wrong or missing and what to do "
        "about it, citing files and lines -- passed on word for word. when approved, "
        "one line on what convinced you."
    )


class Going(BaseModel):
    """A yes or no the compiler must not answer for itself; nobody there is a no."""

    model_config = {"extra": "forbid"}

    proceed: bool = Field(
        default=False, description="yes to go on as asked, no to stop the compile"
    )


class Renamed(BaseModel):
    """Another name, where the one the spec chose is already taken; nobody there is none."""

    model_config = {"extra": "forbid"}

    name: str = Field(
        default="", description="another name for the flow, or '' to stop the compile"
    )


@flow(agents=Compiling, envs=Envs, params=Params)
async def aot(
    task: str, *, agents: Compiling, envs: Envs, params: Params, ctx: FlowContext
) -> str | None:
    """Writes a flow from a description: drafted, loaded, smoke-run, reviewed, then landed."""
    workspace = envs["workspace"]
    drafts = await workspace.derive_scratch(f"aot-{uuid.uuid4().hex[:12]}")
    compiling = _Compile(
        agents=agents,
        workspace=workspace,
        drafts=drafts,
        params=params,
        writing=await agents["writer"].spawn(env=drafts),
        asking=await agents["human"].spawn(env=workspace),
    )
    return await compiling.compiled(task)


class _Compile:
    def __init__(
        self,
        *,
        agents: Compiling,
        workspace: Workspace,
        drafts: Workspace,
        params: Params,
        writing: Session,
        asking: Session,
    ) -> None:
        self.writer = agents["writer"]
        self.critic = agents["critic"]
        self.human = agents["human"]
        self.workspace = workspace
        self.drafts = drafts
        self.params = params
        self.writing = writing
        self.asking = asking

    async def compiled(self, task: str) -> str | None:
        spec = await self._drafted(task)
        if spec is None:
            print("hmz: aot: the writer could not draw a spec from the description; "
                  "nothing was written")
            return None
        unserved, limited = _unserved(spec)
        if unserved:
            resaid = await self._shaped(
                prompts.RESAID.format(unserved=_listed(unserved)), Spec
            )
            if resaid is not None:
                spec = resaid
                unserved, limited = _unserved(spec)
        if unserved and not await self._narrowed(unserved):
            print("hmz: aot: cannot compile -- nothing was written")
            return None
        name = await self._free(_named(self.params.name or spec.name))
        if name is None:
            return None
        draft = self.drafts.workdir / name
        feedback = ""
        files: dict[str, bytes] = {}
        running: list[str] = []
        found: gates.Checked | None = None
        for attempt in range(self.params.repairs + 1):
            asked = (
                prompts.WRITE.format(
                    spec=spec.model_dump_json(indent=2), draft=draft, name=name
                )
                if attempt == 0
                else prompts.REPAIR.format(draft=draft, refused=feedback)
            )
            try:
                await self.writer.run(asked, session=self.writing)
            except HarnessError as error:
                print(f"hmz: aot: the writer's turn failed: {error}")
            files, running = await self._read(name)
            feedback, found = await self._refused(files, name, spec)
            if not feedback:
                break
            print(f"hmz: aot: draft {attempt + 1} refused --")
            print(feedback)
        else:
            if gates.ENTRY not in files:
                print("hmz: aot: the repairs ran out with no draft to show; nothing was "
                      "written")
                return None
            going = await self._asked(prompts.TAKEN.format(refused=feedback), Going)
            if not going.proceed:
                print("hmz: aot: the repairs ran out and nobody took the draft as it "
                      "stands; nothing was written")
                return None
        at = await self._landed(files, running, name)
        if at is not None:
            _reported(at, spec, self.params, found, limited)
        return at

    async def _shaped[T: BaseModel](self, prompt: str, schema: type[T]) -> T | None:
        try:
            return await self.writer.run(
                prompt, session=self.writing, output_schema=schema
            )
        except HarnessError:
            return None

    async def _drafted(self, task: str) -> Spec | None:
        asked = prompts.SPEC.format(briefing=briefing.briefed(), task=task)
        spec = await self._shaped(asked, Spec)
        if spec is None:
            spec = await self._shaped(prompts.SPEC_AGAIN, Spec)
        return spec

    async def _asked[T: BaseModel](self, prompt: str, schema: type[T]) -> T:
        try:
            return await self.human.run(prompt, session=self.asking, output_schema=schema)
        except HarnessError:
            return schema()

    async def _narrowed(self, unserved: list[str]) -> bool:
        for one in unserved:
            print(f"hmz: aot: cannot compile -- asks for {one}, which nothing here serves")
        going = await self._asked(prompts.NARROW.format(unserved=_listed(unserved)), Going)
        return going.proceed

    async def _read(self, name: str) -> tuple[dict[str, bytes], list[str]]:
        files: dict[str, bytes] = {}
        for rel in await self._found(name):
            try:
                files[rel] = await self.drafts.read(f"{name}/{rel}")
            except EnvError:
                continue
        running = [rel for rel in await self._found(name, "-perm", "-u+x") if rel in files]
        return files, running

    async def _found(self, name: str, *only: str) -> list[str]:
        _, out, _ = await self.drafts.exec(["find", name, "-type", "f", *only])
        found: list[str] = []
        for line in sorted(out.splitlines()):
            rel = line.removeprefix(f"{name}/")
            parts = rel.split("/")
            if rel == line or any(part.startswith(".") or part == "__pycache__" for part in parts):
                continue
            found.append(rel)
        return found

    async def _refused(
        self, files: dict[str, bytes], name: str, spec: Spec
    ) -> tuple[str, gates.Checked | None]:
        if gates.ENTRY not in files:
            return (
                f"nothing landed at {self.drafts.workdir / name} -- write the flow there: "
                f"a directory of that name holding the {gates.ENTRY} that is the flow",
                None,
            )
        found = await gates.checked(
            files, name, self.params.seconds, str(self.drafts.workdir / name)
        )
        blocking = found.blocking(strict=self.params.strict)
        if blocking:
            return "the gates refused it:\n" + gates.said(blocking), found
        reading = await self.critic.spawn(env=self.drafts)
        try:
            review = await self.critic.run(
                prompts.REVIEW.format(spec=spec.model_dump_json(indent=2), draft=name),
                session=reading,
                output_schema=Review,
            )
        except HarnessError:
            return (
                "the critic's turn failed, so nothing has read the draft fresh -- hold it "
                "tighter to the spec and to the writing-flows contract, and it will be "
                "read again",
                found,
            )
        if not review.approved:
            return review.notes or "the critic did not approve it, and said nothing more", found
        return "", found

    def _mine(self) -> str:
        return os.path.expanduser(MINE[self.params.into])

    async def _free(self, name: str) -> str | None:
        mine = self._mine()
        for asked in range(RENAMES + 1):
            if not await self._taken(mine, name):
                return name
            if asked == RENAMES:
                break
            renamed = await self._asked(prompts.RENAME.format(name=name), Renamed)
            if not renamed.name.strip():
                print(f"hmz: aot: there is already a flow called {name!r} in {mine}, and "
                      "nobody gave another name; nothing was written")
                return None
            name = _named(renamed.name)
        print(f"hmz: aot: there is already a flow called {name!r} in {mine}; nothing "
              "was written")
        return None

    async def _landed(
        self, files: dict[str, bytes], running: list[str], name: str
    ) -> str | None:
        mine = self._mine()
        at = f"{mine}/{name}"
        if await self._taken(mine, name):
            print(f"hmz: aot: a flow called {name!r} appeared in {mine} while this one was "
                  "compiled; nothing was written")
            return None
        holding = f"{mine}/.{name}.{uuid.uuid4().hex[:12]}"
        try:
            for rel, data in files.items():
                await self.workspace.write(f"{holding}/{name}/{rel}", data)
            if running:
                await self.workspace.exec(
                    ["chmod", "+x", *(f"{holding}/{name}/{rel}" for rel in running)]
                )
            code, _, err = await self.workspace.exec(
                [sys.executable, "-c", RENAME, f"{holding}/{name}", at]
            )
            if code != 0:
                print(f"hmz: aot: could not land the flow at {at}: {err.strip()}; nothing "
                      "was written")
                return None
        finally:
            await self.workspace.exec(["rm", "-rf", holding])
        return at

    async def _taken(self, mine: str, name: str) -> bool:
        code, out, _ = await self.workspace.exec(["ls", mine])
        there = set(out.split()) if code == 0 else set[str]()
        return name in there or f"{name}.py" in there


def _listed(items: list[str]) -> str:
    return "\n".join(f"- {one}" for one in items)


def _named(said: str) -> str:
    held = "".join(
        one if one.isascii() and one.isalnum() else "_" for one in said.strip().lower()
    )
    held = "_".join(part for part in held.split("_") if part) or "compiled_flow"
    if held[0].isdigit():
        held = f"flow_{held}"
    return f"{held}_flow" if keyword.iskeyword(held) else held


def _unserved(spec: Spec) -> tuple[list[str], list[str]]:
    served = briefing.capabilities()
    every = set(briefing.harnesses())
    unserved = [need for need in spec.needs if need not in served]
    limited: list[str] = []
    for seat in spec.seats:
        if seat.person:
            continue
        for mixin in seat.mixins:
            harnesses = served.get(mixin)
            if harnesses is None:
                unserved.append(f"{mixin} on {seat.name}")
            elif not set(harnesses) >= every:
                limited.append(
                    f"{seat.name} needs {mixin} -- runs only on: "
                    f"{', '.join(sorted(harnesses))}"
                )
    return unserved, limited


def _reported(
    at: str,
    spec: Spec,
    params: Params,
    found: gates.Checked | None,
    limited: list[str],
) -> None:
    name = os.path.basename(at)
    print(f"\ncompiled: {name} -- {spec.about}")
    print(f"landed:   {at}")
    for seat in spec.seats:
        what = "the person outside the run" if seat.person else "an agent"
        needs = f" (needs {', '.join(seat.mixins)})" if seat.mixins else ""
        print(f"drives:   {seat.name} -- {what}{needs}")
    for setting in spec.settings:
        print(f"takes:    {setting.name} = {setting.default} -- {setting.about}")
    for ending in spec.endings:
        print(f"ends:     by {ending.by} -- {ending.bound}")
    waived = [] if found is None else found.waived(strict=params.strict)
    if waived:
        print("waived:")
        print(gates.said(waived))
    for one in limited:
        print(f"only on:  {one}")
    roles = (
        list(found.agents)
        if found is not None and found.agents
        else [seat.name for seat in spec.seats if not seat.person]
    )
    line = " ".join(f"-a {role}=CLI/MODEL:EFFORT" for role in roles)
    print(f'\nhmz exec -f {params.into}/{name} {line} -b cost=USD "the task"')
