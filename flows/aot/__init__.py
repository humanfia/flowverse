"""AOT -- the flow that writes a flow: a description in, a checked and proved flow out."""

import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NamedTuple

from _aot import prompts
from hmz.flows import (
    ALWAYS_DONE,
    ENTRY,
    EVERYWHERE,
    MINE,
    NEVER_DONE,
    SILENT,
    Agent,
    Finding,
    Person,
    Scenario,
    Session,
    briefed,
    catalogue,
    checked,
    flow,
    proved,
)
from pydantic import BaseModel, Field


class Compiling(NamedTuple):
    writer: Agent
    critic: Agent
    human: Person


class Config(BaseModel):
    model_config = {"frozen": True}

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
        "blocks -- an unbounded loop always blocks, whatever this says",
    )
    seconds: float = Field(
        default=60.0,
        gt=0,
        description="the clock each stub-driven proof of a draft is held to, a scenario "
        "apiece",
    )


class Seat(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(description="what the flow calls it, snake_case, for what it does")
    person: bool = Field(
        description="true only for the person at the prompt, whom nobody configures",
    )
    moments: list[str] = Field(
        description="moments this seat hangs hooks on beyond the ones every backend "
        "runs, each by the name the briefing uses -- e.g. PermissionRequest -- and [] "
        "for a seat that needs none",
    )
    goal: bool = Field(
        description="true only if this seat runs under the backend's own goal feature",
    )


class Setting(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(description="the field's name, snake_case")
    kind: Literal["number", "text", "switch"] = Field(description="what it holds")
    default: str = Field(description="the default, written out -- '10.0', 'off'")
    about: str = Field(description="one line saying what it does, for whoever sets it")


class Ending(BaseModel):
    model_config = {"extra": "forbid"}

    by: Literal["budget", "rounds", "verdict"] = Field(
        description="what ends it: output tokens spent, a cap on the rounds, or an "
        "agent's shaped verdict -- which always travels with a budget or a cap besides"
    )
    bound: str = Field(
        description="the bound, written out -- '10 million output tokens', '6 rounds', "
        "'reviewer says done, under a 10M budget'"
    )


class Spec(BaseModel):
    model_config = {"extra": "forbid"}

    about: str = Field(description="one line saying what the flow does")
    name: str = Field(description="what to call it, snake_case")
    seats: list[Seat] = Field(description="every agent it drives, the person included "
                              "if it talks to one")
    settings: list[Setting] = Field(
        description="the knobs it takes, [] for a flow that takes none"
    )
    endings: list[Ending] = Field(
        description="every way it ends -- at least one, and never a verdict alone"
    )
    needs: list[str] = Field(
        description="what it needs of the interface, each named exactly as the briefing "
        "names a capability -- e.g. shapes, pursue, moment:PermissionRequest -- and "
        "nothing the briefing does not name"
    )
    plan: str = Field(description="how the flow will work, a short paragraph")


class Review(BaseModel):
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
    model_config = {"extra": "forbid"}

    proceed: bool = Field(description="yes to go on as asked, no to stop the compile")


class Renamed(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(description="another name for the flow, or '' to stop the compile")


@flow
def run(agents: Compiling, task: str, config: Config | None = None) -> None:
    held = config or Config()
    scratch = tempfile.mkdtemp(prefix=".aot.")
    try:
        _compiled(agents, task, held, Path(scratch))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _compiled(agents: Compiling, task: str, held: Config, scratch: Path) -> None:
    writing = agents.writer.new(cwd=scratch)
    spec = _drafted(writing, task)
    if spec is None:
        print("hmz: aot: the writer could not draw a spec from the description; nothing "
              "was written")
        return
    unserved, limited = _unserved(spec)
    if unserved:
        resaid = writing(
            prompts.RESAID.format(
                unserved="\n".join(f"- {one}" for one in unserved)
            ),
            suppress=True,
            schema=Spec,
        )
        if resaid is not None:
            spec = resaid
            unserved, limited = _unserved(spec)
    if unserved and not _narrowed(agents.human, unserved):
        print("hmz: aot: cannot compile -- nothing was written")
        return
    name = _named(held.name or spec.name)
    draft = scratch / name
    feedback = ""
    landed = False
    for attempt in range(held.repairs + 1):
        asked = (
            prompts.WRITE.format(spec=spec.model_dump_json(indent=2), draft=draft,
                                 name=name)
            if attempt == 0
            else prompts.REPAIR.format(draft=draft, refused=feedback)
        )
        writing(asked, suppress=True)
        feedback = _refused(draft, spec, held, agents)
        if not feedback:
            landed = True
            break
        print(f"hmz: aot: draft {attempt + 1} refused --")
        print(feedback)
    if not landed:
        if not (draft / ENTRY).is_file():
            print("hmz: aot: the repairs ran out with no draft to show; nothing was "
                  "written")
            return
        if not _taken(agents.human, feedback):
            print("hmz: aot: the repairs ran out and nobody took the draft as it "
                  "stands; nothing was written")
            return
    at = _landed(draft, name, held.into, agents.human)
    if at is None:
        return
    _reported(at, spec, held, limited)


def _drafted(writing: Session, task: str) -> Spec | None:
    asked = prompts.SPEC.format(briefing=briefed(), task=task)
    spec = writing(asked, suppress=True, schema=Spec)
    if spec is None:
        spec = writing(prompts.SPEC_AGAIN, suppress=True, schema=Spec)
    return spec


def _named(said: str) -> str:
    held = "".join(one if one.isalnum() else "_" for one in said.strip().lower())
    held = "_".join(part for part in held.split("_") if part)
    return held or "compiled_flow"


def _unserved(spec: Spec) -> tuple[list[str], list[str]]:
    served = {one.name: one for one in catalogue()}
    everywhere = {one.value for one in EVERYWHERE}
    unserved: list[str] = []
    limited: list[str] = []
    for need in spec.needs:
        if need not in served:
            unserved.append(need)
        elif served[need].backends:
            limited.append(
                f"{need} -- runs only on: {', '.join(sorted(served[need].backends))}"
            )
    for seat in spec.seats:
        for moment in seat.moments:
            if moment in everywhere:
                continue
            key = f"moment:{moment}"
            if key not in served:
                unserved.append(f"a hook on {moment!r}")
            else:
                limited.append(
                    f"{seat.name} needs Moment.{moment} -- runs only on: "
                    f"{', '.join(sorted(served[key].backends))}"
                )
    return unserved, limited


def _narrowed(human: Person, unserved: list[str]) -> bool:
    for one in unserved:
        print(f"hmz: aot: cannot compile -- asks for {one}, which nothing here serves")
    going = human(
        prompts.NARROW.format(unserved="\n".join(f"- {one}" for one in unserved)),
        suppress=True,
        schema=Going,
    )
    return going is not None and going.proceed


def _refused(draft: Path, spec: Spec, held: Config, agents: Compiling) -> str:
    if not (draft / ENTRY).is_file():
        return (
            f"nothing landed at {draft} -- write the flow there: a directory of that "
            f"name holding the {ENTRY} that is the flow"
        )
    found = checked(draft)
    blocking = [one for one in found if _blocks(one, strict=held.strict)]
    if blocking:
        return "the checker refused it:\n" + _said(blocking)
    proof = proved(draft, scenarios=_worlds(held.seconds))
    if proof.findings:
        return "loading it was refused:\n" + _said(proof.findings)
    stalled = [one for one in proof.outcomes if not one.finished]
    if stalled:
        return "driven by stubs, it did not end:\n" + "\n".join(
            f"- under {one.scenario}: {one.said}" for one in stalled
        )
    review = agents.critic(
        prompts.REVIEW.format(spec=spec.model_dump_json(indent=2), draft=draft),
        suppress=True,
        schema=Review,
        cwd=draft.parent,
    )
    if review is None:
        return (
            "the critic's turn failed, so nothing has read the draft fresh -- hold it "
            "tighter to the spec and to the writing-flows contract, and it will be "
            "read again"
        )
    if not review.approved:
        return review.notes or "the critic did not approve it, and said nothing more"
    return ""


def _blocks(one: Finding, *, strict: bool) -> bool:
    return one.severity == "error" or one.code == "unbounded-loop" or strict


def _worlds(seconds: float) -> tuple[Scenario, ...]:
    return tuple(
        one._replace(seconds=seconds) for one in (NEVER_DONE, ALWAYS_DONE, SILENT)
    )


def _said(findings: Sequence[Finding]) -> str:
    return "\n".join(
        f"- {one.where.name}:{one.line}: {one.severity}: {one.code}: {one.said}"
        for one in findings
    )


def _taken(human: Person, feedback: str) -> bool:
    going = human(
        prompts.TAKEN.format(refused=feedback), suppress=True, schema=Going
    )
    return going is not None and going.proceed


def _landed(draft: Path, name: str, into: str, human: Person) -> str | None:
    mine = os.path.expanduser(MINE[into])
    for _ in range(2):
        at = os.path.join(mine, name)
        stem = at.removesuffix(".py")
        if not (os.path.exists(stem) or os.path.exists(stem + ".py")):
            break
        asked = human(prompts.RENAME.format(name=name), suppress=True, schema=Renamed)
        if asked is None or not asked.name.strip():
            print(f"hmz: aot: there is already a flow called {name!r} in {mine}, and "
                  "nobody gave another name; nothing was written")
            return None
        name = _named(asked.name)
    else:
        print(f"hmz: aot: there is already a flow called {name!r} in {mine}; nothing "
              "was written")
        return None
    os.makedirs(mine, exist_ok=True)
    holding = tempfile.mkdtemp(dir=mine, prefix=f".{name}.")
    try:
        kept = os.path.join(holding, name)
        shutil.copytree(draft, kept)
        os.replace(kept, os.path.join(mine, name))
    finally:
        shutil.rmtree(holding, ignore_errors=True)
    return os.path.join(mine, name)


def _reported(at: str, spec: Spec, held: Config, limited: list[str]) -> None:
    name = os.path.basename(at)
    print(f"\ncompiled: {name} -- {spec.about}")
    print(f"landed:   {at}")
    for seat in spec.seats:
        what = "the person at the prompt" if seat.person else "an agent"
        extras = [f"Moment.{one}" for one in seat.moments]
        if seat.goal:
            extras.append("a goal feature")
        needs = f" (needs {', '.join(extras)})" if extras else ""
        print(f"drives:   {seat.name} -- {what}{needs}")
    for setting in spec.settings:
        print(f"takes:    {setting.name} = {setting.default} -- {setting.about}")
    for ending in spec.endings:
        print(f"ends:     by {ending.by} -- {ending.bound}")
    waived = [
        one for one in checked(at) if not _blocks(one, strict=held.strict)
    ]
    if waived:
        print("waived:")
        print(_said(waived))
    for one in limited:
        print(f"only on:  {one}")
    chosen = sum(1 for seat in spec.seats if not seat.person)
    line = " ".join(["-a CLI/MODEL:EFFORT"] * chosen)
    print(f'\nhmz exec -f {held.into}/{name} {line} "the task"')
