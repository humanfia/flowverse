from __future__ import annotations

import sys
import textwrap
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest
from hmz.flows import (
    FilesEnvMixin,
    Permission,
    PermissionKind,
    ScratchDirEnvMixin,
    ShellEnvMixin,
)
from hmz.runtime.flowing.engine import load_flow
from hmz.runtime.flowing.fakes import (
    Command,
    FakeAgentDriver,
    FakeEnvDriver,
    FakeOutworlder,
    FakeSession,
    run_fake,
)

from tests.kit import FLOWS

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.asyncio

FLOW = FLOWS / "aot"
TASK = "two agents take turns until a reviewer says it is done"
LANDED = ".humanize/flows/pair_loop/__init__.py"
THEIRS = '"""Somebody\'s own flow."""\n'


def loaded() -> Any:
    return load_flow(str(FLOW), caller_globals={})


def aot() -> Any:
    loaded()
    return sys.modules["aot"]


def gates() -> Any:
    loaded()
    return sys.modules["_aot.gates"]


def spec(name: str = "pair_loop", needs: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "about": "two agents take turns until a reviewer says it is done",
        "name": name,
        "seats": [
            {"name": "actor", "person": False, "mixins": []},
            {"name": "reviewer", "person": False, "mixins": []},
        ],
        "settings": [
            {
                "name": "rounds",
                "kind": "number",
                "default": "6",
                "about": "the most rounds before it stops",
            }
        ],
        "endings": [
            {"by": "verdict", "bound": "the reviewer says done, within six rounds"}
        ],
        "needs": list(needs),
        "plan": "the actor works, the reviewer reads it fresh, the round cap backstops",
    }


GOOD = {
    "pair_loop/__init__.py": '''
    """Pair loop -- an actor works until a fresh reviewer says the task is done.

        hmz exec -f local/pair_loop -a actor=claude/claude-opus-5:high \\
            -a reviewer=codex/gpt-5.6-sol:high -b cost=10 "the task"
    """

    import asyncio

    from hmz.flows import (
        Agent, AgentCollection, EnvCollection, FlowContext, FlowParams, LocalEnv,
        OutputSchemaError, flow,
    )
    from pydantic import BaseModel, Field


    class Agents(AgentCollection):
        actor: Agent
        reviewer: Agent


    class Envs(EnvCollection):
        workspace: LocalEnv


    class Params(FlowParams):
        rounds: int = Field(default=6, ge=1, description="the most rounds before it stops")


    class Review(BaseModel):
        model_config = {"extra": "forbid"}

        done: bool = Field(description="true only if the task is completely done")
        notes: str = Field(description="what the actor is told next, word for word")


    @flow(agents=Agents, envs=Envs, params=Params)
    async def pair_loop(task, *, agents, envs, params, ctx):
        """An actor works until a fresh reviewer says the task is done."""
        actor, reviewer = agents["actor"], agents["reviewer"]
        workspace = envs["workspace"]
        working = await actor.spawn(env=workspace)
        prompt = task
        for round_ in range(params.rounds):
            print(f"round {round_ + 1}/{params.rounds}")
            await actor.run(prompt, session=working)
            reading = await reviewer.spawn(env=workspace)
            try:
                review = await reviewer.run(task, session=reading, output_schema=Review)
            except OutputSchemaError:
                continue
            if review.done:
                return review.notes
            prompt = review.notes or task
            await asyncio.sleep(5)
        return ""
    ''',
}

DEAD = {
    "pair_loop/__init__.py": '''
    """A loop nothing but the budget can end."""

    from hmz.flows import (
        Agent, AgentCollection, EnvCollection, FlowParams, LocalEnv, flow,
    )


    class Agents(AgentCollection):
        actor: Agent
        reviewer: Agent


    class Envs(EnvCollection):
        workspace: LocalEnv


    @flow(agents=Agents, envs=Envs, params=FlowParams)
    async def pair_loop(task, *, agents, envs, params, ctx):
        actor = agents["actor"]
        working = await actor.spawn(env=envs["workspace"])
        while True:
            await actor.run(task, session=working)
    ''',
}

OLD = {
    "pair_loop/__init__.py": '''
    """A flow written to the interface that is gone."""

    from hmz.flows import Agent, flow


    @flow
    def run(agents: tuple[Agent, Agent], task: str) -> None:
        agents[0](task, suppress=True)
    ''',
}

UNNAMED = {"pair_loop/__init__.py": GOOD["pair_loop/__init__.py"].replace(
    "async def pair_loop(", "async def run("
)}

BUGGY = {"pair_loop/__init__.py": GOOD["pair_loop/__init__.py"].replace(
    "prompt = review.notes or task", "prompt = reveiw.notes or task"
)}

HUNG = {
    "pair_loop/__init__.py": '''
    """A flow that never lets go."""

    from hmz.flows import Agent, AgentCollection, EnvCollection, FlowParams, LocalEnv, flow


    class Agents(AgentCollection):
        actor: Agent


    class Envs(EnvCollection):
        workspace: LocalEnv


    @flow(agents=Agents, envs=Envs, params=FlowParams)
    async def pair_loop(task, *, agents, envs, params, ctx):
        while True:
            pass
    ''',
}


SLEEPY = {"pair_loop/__init__.py": GOOD["pair_loop/__init__.py"].replace(
    "import asyncio", "import asyncio, time"
).replace("await asyncio.sleep(5)", "time.sleep(5)")}

READER = {"pair_loop/__init__.py": GOOD["pair_loop/__init__.py"].replace(
    "Agent, AgentCollection,", "Agent, AgentCollection, FilesEnvMixin,"
).replace(
    "workspace: LocalEnv", "workspace: Workspace"
).replace(
    "class Envs(", "class Workspace(LocalEnv, FilesEnvMixin): ...\n\n\n    class Envs("
).replace(
    "await actor.run(prompt, session=working)",
    "await actor.run(prompt, session=working)\n            await workspace.read('plan.md')",
)}


def shell(command: Command, env: FakeEnvDriver) -> tuple[int, str, str] | None:
    """What aot runs, over the fake machine's files: `find`, `chmod`, `rm -rf`, a rename."""
    if isinstance(command, str):
        return None
    files: dict[PurePosixPath, bytes] = env._disk.files  # pyright: ignore[reportPrivateUsage]
    match command:
        case ("find", where, "-type", "f", *only):
            base = env.workdir / where
            found = sorted(one for one in files if one.is_relative_to(base))
            if not found:
                return 1, "", f"find: '{where}': No such file or directory\n"
            if only:
                return 0, "", ""
            lines = "".join(f"{PurePosixPath(where) / one.relative_to(base)}\n" for one in found)
            return 0, lines, ""
        case (python, "-c", _, source, target) if python == sys.executable:
            moved, into = env.workdir / source, env.workdir / target
            if any(one.is_relative_to(into) for one in files):
                return 1, "", f"OSError: [Errno 39] Directory not empty: '{target}'\n"
            for one in [one for one in files if one.is_relative_to(moved)]:
                files[into / one.relative_to(moved)] = files.pop(one)
            return 0, "", ""
        case ("chmod", *_):
            return 0, "", ""
        case ("rm", "-rf", target):
            gone = env.workdir / target
            for one in [one for one in files if one.is_relative_to(gone)]:
                del files[one]
            return 0, "", ""
        case _:
            return None


class Writer:
    """Answers the spec from a list, and writes a tree of files per draft it is asked for."""

    def __init__(
        self,
        local: FakeEnvDriver | None,
        specs: dict[str, object] | list[dict[str, object]],
        trees: list[Mapping[str, str]],
        meanwhile: Mapping[str, str] | None = None,
    ) -> None:
        self.local = local
        self.specs = list(specs) if isinstance(specs, list) else [specs]
        self.trees = list(trees)
        self.meanwhile = dict(meanwhile or {})
        self.asked: list[str] = []

    async def __call__(
        self, prompt: str, *, output_schema: Any = None, session: FakeSession
    ) -> Any:
        if output_schema is not None:
            return self.specs.pop(0) if len(self.specs) > 1 else self.specs[0]
        self.asked.append(prompt)
        if self.local is not None:
            for rel, source in self.meanwhile.items():
                await self.local.write(rel, source.encode())
            self.meanwhile.clear()
        if self.trees:
            for rel, source in self.trees.pop(0).items():
                at = f"{session.placement.workdir}/{rel}"
                data = (textwrap.dedent(source).strip() + "\n").encode()
                if self.local is None:
                    Path(at).parent.mkdir(parents=True, exist_ok=True)
                    Path(at).write_bytes(data)
                    if at.endswith(".sh"):
                        Path(at).chmod(0o755)
                else:
                    await self.local.write(at, data)
        return "written"


class Critic:
    def __init__(self, reviews: list[dict[str, object]] | None = None) -> None:
        self.reviews = list(reviews or [])
        self.asked: list[str] = []

    def __call__(self, prompt: str, **_: Any) -> dict[str, object]:
        self.asked.append(prompt)
        return self.reviews.pop(0) if self.reviews else {"approved": True, "notes": "sound"}


class Compiled:
    def __init__(self, local: FakeEnvDriver, writer: Writer, critic: Critic) -> None:
        self.local = local
        self.writer = writer
        self.critic = critic
        self.result: Any = None


async def compiled(
    *,
    specs: dict[str, object] | list[dict[str, object]] | None = None,
    trees: list[Mapping[str, str]] | None = None,
    reviews: list[dict[str, object]] | None = None,
    human: FakeOutworlder | None = None,
    params: Mapping[str, object] | None = None,
    files: Mapping[str, str] | None = None,
    meanwhile: Mapping[str, str] | None = None,
) -> Compiled:
    local = FakeEnvDriver(files, workdir="/here", run=shell)
    writer = Writer(
        local, specs or spec(), trees if trees is not None else [GOOD], meanwhile
    )
    critic = Critic(reviews)
    done = Compiled(local, writer, critic)
    done.result = await run_fake(
        loaded(),
        TASK,
        agents={
            "writer": FakeAgentDriver(reply=writer),
            "critic": FakeAgentDriver(reply=critic),
        },
        outworlder=human,
        local=local,
        params={"seconds": 30.0, **(params or {})},
    )
    return done


async def test_what_the_compiler_declares() -> None:
    declared = loaded().describe()

    assert declared.name == "aot"
    assert not declared.resumable
    assert [(role.name, role.auto, role.skills) for role in declared.agents] == [
        ("writer", False, ("writing-flows",)),
        ("critic", False, ("writing-flows",)),
        ("human", True, ()),
    ]
    (critic,) = [role for role in declared.agents if role.name == "critic"]
    assert critic.permission == Permission(local=PermissionKind.READ)
    (workspace,) = declared.envs
    assert workspace.name == "workspace"
    assert workspace.auto
    assert workspace.capabilities == frozenset(
        {ShellEnvMixin, FilesEnvMixin, ScratchDirEnvMixin}
    )
    assert set(declared.params.model_fields) == {
        "name",
        "into",
        "repairs",
        "strict",
        "seconds",
    }
    assert (FLOW / "skills" / "writing-flows" / "SKILL.md").is_file()


async def test_a_good_draft_lands_whole(capsys: pytest.CaptureFixture[str]) -> None:
    done = await compiled()

    assert done.result == ".humanize/flows/pair_loop"
    assert done.local.text(LANDED).startswith('"""Pair loop')
    assert [path for path in done.local.files if path.startswith(".humanize")] == [
        LANDED
    ]
    assert len(done.writer.asked) == 1
    assert "pair_loop" in done.writer.asked[0]
    assert len(done.critic.asked) == 1
    out = capsys.readouterr().out
    assert "compiled: pair_loop" in out
    assert (
        "hmz exec -f local/pair_loop -a actor=CLI/MODEL:EFFORT "
        "-a reviewer=CLI/MODEL:EFFORT -b cost=USD" in out
    )
    assert "ends:     by verdict" in out


async def test_what_lands_loads_as_a_flow_named_after_its_directory(
    tmp_path: Path,
) -> None:
    done = await compiled()
    at = tmp_path / "pair_loop"
    at.mkdir()
    (at / "__init__.py").write_text(done.local.text(LANDED))

    landed: Any = load_flow(str(at), caller_globals={})
    declared = landed.describe()

    assert declared.name == "pair_loop"
    assert [role.name for role in declared.agents] == ["actor", "reviewer"]


async def test_a_flow_that_does_not_end_is_handed_back_word_for_word() -> None:
    done = await compiled(trees=[DEAD, GOOD])

    assert done.local.text(LANDED).startswith('"""Pair loop')
    assert len(done.writer.asked) == 2
    assert "unbounded" in done.writer.asked[1]
    assert "under never-done, it did not end on its own" in done.writer.asked[1]
    assert len(done.critic.asked) == 1


async def test_a_draft_that_does_not_load_is_handed_back() -> None:
    done = await compiled(trees=[OLD, GOOD])

    assert done.local.text(LANDED).startswith('"""Pair loop')
    assert "error: load:" in done.writer.asked[1]
    assert "FlowDefinitionError" in done.writer.asked[1]


async def test_nothing_written_is_the_writers_to_write() -> None:
    done = await compiled(trees=[{}, GOOD])

    assert done.local.text(LANDED).startswith('"""Pair loop')
    assert "nothing landed at /scratch/" in done.writer.asked[1]


async def test_a_warning_is_waived_unless_strict(capsys: pytest.CaptureFixture[str]) -> None:
    done = await compiled(trees=[UNNAMED])

    assert done.local.text(LANDED).startswith('"""Pair loop')
    out = capsys.readouterr().out
    assert "waived:" in out
    assert "entry-name" in out

    strict = await compiled(trees=[UNNAMED, GOOD], params={"strict": True})

    assert "entry-name" in strict.writer.asked[1]


async def test_a_flow_that_never_yields_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates(), "GRACE", 1.0)

    done = await compiled(trees=[HUNG, GOOD], params={"seconds": 1.0})

    assert done.local.text(LANDED).startswith('"""Pair loop')
    assert "error: hung: under never-done" in done.writer.asked[1]


async def test_the_critics_veto_is_a_repair_round() -> None:
    done = await compiled(
        trees=[GOOD, GOOD],
        reviews=[{"approved": False, "notes": "the round cap needs a ceiling"}],
    )

    assert done.local.text(LANDED).startswith('"""Pair loop')
    assert len(done.writer.asked) == 2
    assert "the round cap needs a ceiling" in done.writer.asked[1]
    assert "pair_loop" in done.critic.asked[0]


async def test_an_ask_nothing_serves_is_refused_before_anything_is_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    done = await compiled(specs=spec(needs=("interrupting a turn mid-stream",)))

    assert done.result is None
    assert not [path for path in done.local.machine if ".humanize" in path]
    assert done.writer.asked == []
    out = capsys.readouterr().out
    assert (
        "cannot compile -- asks for interrupting a turn mid-stream, which nothing "
        "here serves" in out
    )


async def test_a_mis_worded_need_is_the_writers_to_restate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    done = await compiled(specs=[spec(needs=("plan file",)), spec(needs=("ShellEnvMixin",))])

    assert done.local.text(LANDED).startswith('"""Pair loop')
    assert len(done.writer.asked) == 1
    assert "cannot compile" not in capsys.readouterr().out


async def test_a_capability_only_some_harnesses_serve_is_reported() -> None:
    limited = spec()
    limited["seats"] = [
        {"name": "actor", "person": False, "mixins": ["GoalCommandAgentMixin"]},
        {"name": "reviewer", "person": False, "mixins": []},
    ]

    done = await compiled(specs=limited)

    assert done.result == ".humanize/flows/pair_loop"


async def test_a_name_already_taken_is_not_written_over(
    capsys: pytest.CaptureFixture[str],
) -> None:
    done = await compiled(files={LANDED: THEIRS})

    assert done.result is None
    assert done.local.text(LANDED) == THEIRS
    assert "already a flow called 'pair_loop'" in capsys.readouterr().out
    assert done.writer.asked == []


async def test_a_person_renames_a_flow_whose_name_is_taken_before_it_is_drafted() -> None:
    human = FakeOutworlder(reply=[{"name": "taken two"}, {"name": "pair loop two"}])
    tree = {
        "pair_loop_two/__init__.py": GOOD["pair_loop/__init__.py"].replace(
            "async def pair_loop(", "async def pair_loop_two("
        )
    }

    done = await compiled(
        files={LANDED: THEIRS, ".humanize/flows/taken_two.py": THEIRS},
        human=human,
        trees=[tree],
    )

    assert done.result == ".humanize/flows/pair_loop_two"
    assert "async def pair_loop_two(" in done.local.text(
        ".humanize/flows/pair_loop_two/__init__.py"
    )
    assert done.local.text(LANDED) == THEIRS
    assert len(human.asked) == 2
    assert "`pair_loop_two`" in done.writer.asked[0]


async def test_three_names_taken_is_the_end_of_asking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    human = FakeOutworlder(reply=[{"name": "taken two"}, {"name": "taken three"}])
    taken = {
        f".humanize/flows/{name}/__init__.py": THEIRS
        for name in ("pair_loop", "taken_two", "taken_three")
    }

    done = await compiled(files=taken, human=human)

    assert done.result is None
    assert "already a flow called 'taken_three'" in capsys.readouterr().out
    assert len(human.asked) == 2


async def test_a_flow_that_appears_while_compiling_is_not_written_over(
    capsys: pytest.CaptureFixture[str],
) -> None:
    done = await compiled(meanwhile={LANDED: THEIRS})

    assert done.result is None
    assert done.local.text(LANDED) == THEIRS
    assert "appeared in .humanize/flows while this one was compiled" in (
        capsys.readouterr().out
    )
    assert not [path for path in done.local.files if "/.pair_loop." in path]


@pytest.mark.parametrize(
    ("said", "named"),
    [
        ("Pair Loop", "pair_loop"),
        ("2 agents review", "flow_2_agents_review"),
        ("pass", "pass_flow"),
        ("Ünïcode²", "n_code"),
        ("", "compiled_flow"),
    ],
)
async def test_a_name_is_one_python_can_import(said: str, named: str) -> None:
    assert aot()._named(said) == named


async def test_a_person_may_take_the_draft_the_repairs_ran_out_on() -> None:
    human = FakeOutworlder(reply={"proceed": True})

    done = await compiled(trees=[DEAD], human=human, params={"repairs": 0})

    assert "while True" in done.local.text(LANDED)
    assert len(done.writer.asked) == 1
    assert "unbounded" in human.asked[0]


async def test_nobody_there_keeps_nothing_the_repairs_ran_out_on() -> None:
    done = await compiled(trees=[DEAD], params={"repairs": 0})

    assert done.result is None
    assert not [path for path in done.local.files if path.startswith(".humanize")]


async def test_a_flow_lands_in_the_home_flowverse_when_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/home/someone")

    done = await compiled(params={"into": "user"})

    assert done.result == "/home/someone/.humanize/flows/pair_loop"
    assert "/home/someone/.humanize/flows/pair_loop/__init__.py" in done.local.machine


async def test_a_draft_lands_in_a_real_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hmz.runtime.flowing.environments import local_env

    home = tmp_path / "home"
    monkeypatch.setenv("HUMANIZE_HOME", str(home))
    project = tmp_path / "project"
    kept = project / ".humanize" / "flows" / "kept"
    kept.mkdir(parents=True)
    (kept / "__init__.py").write_text(THEIRS)
    tree = {
        **GOOD,
        "pair_loop/__pycache__/x.pyc": "stale",
        "pair_loop/.cache/x": "stale",
        "pair_loop/_pair_loop/run.sh": "echo run",
    }
    writer = Writer(None, spec(), [tree])

    said = await run_fake(
        loaded(),
        TASK,
        agents={
            "writer": FakeAgentDriver(reply=writer),
            "critic": FakeAgentDriver(reply=Critic()),
        },
        local=local_env(project),
        params={"seconds": 30.0},
    )

    assert said == ".humanize/flows/pair_loop"
    flows = project / ".humanize" / "flows"
    assert sorted(one.name for one in flows.iterdir()) == ["kept", "pair_loop"]
    landed = flows / "pair_loop"
    assert sorted(
        str(one.relative_to(landed)) for one in landed.rglob("*") if one.is_file()
    ) == ["__init__.py", "_pair_loop/run.sh"]
    assert (landed / "__init__.py").read_text().startswith('"""Pair loop')
    assert (landed / "_pair_loop" / "run.sh").stat().st_mode & 0o100
    assert not (landed / "__init__.py").stat().st_mode & 0o100
    assert not [one for one in home.rglob("*") if one.is_file()]


@pytest.mark.parametrize(
    ("tree", "codes"),
    [
        (GOOD, []),
        (DEAD, [("error", "unbounded")] * 3),
        (OLD, [("error", "load")]),
        (UNNAMED, [("warning", "entry-name")]),
        (BUGGY, [("error", "raised")] * 2),
        (SLEEPY, [("error", "raised")] * 2),
        (READER, [("warning", "alone")] * 3),
    ],
)
async def test_the_gates(tree: Mapping[str, str], codes: list[tuple[str, str]]) -> None:
    files = {
        rel.removeprefix("pair_loop/"): (textwrap.dedent(source).strip() + "\n").encode()
        for rel, source in tree.items()
    }

    found = await gates().checked(files, "pair_loop", 30.0, "/drafts/pair_loop")

    assert [(one.severity, one.code) for one in found.findings] == codes
    if not codes or codes[0][1] != "load":
        assert found.agents == ("actor", "reviewer")
    for one in found.findings:
        assert "/.aot." not in one.said
        if one.code in ("load", "raised", "alone"):
            assert "(at __init__.py:" in one.said or "/drafts/pair_loop" in one.said


async def test_a_draft_starts_no_program_on_this_machine(tmp_path: Path) -> None:
    witness = tmp_path / "ran"
    tree = GOOD["pair_loop/__init__.py"].replace(
        "import asyncio",
        f"import asyncio, subprocess\n    subprocess.run(['touch', {str(witness)!r}])",
    )

    found = await gates().checked(
        {"__init__.py": (textwrap.dedent(tree).strip() + "\n").encode()}, "pair_loop", 30.0
    )

    assert [(one.severity, one.code) for one in found.findings] == [("error", "load")]
    assert "a flow starts no program itself" in found.findings[0].said
    assert not witness.exists()
