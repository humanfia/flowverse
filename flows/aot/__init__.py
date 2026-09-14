"""AOT -- the flow that writes a flow: a description in, a checked and proved flow out.

hmz exec -f official/aot -a claude/MODEL:high -a codex/MODEL:high \\
    "two agents take turns on the task until a reviewer says it is done"

The writer reads the description against a briefing of what this installed humanize serves,
and says what the flow is to be first -- the places, the settings, the ways it ends, and what
it needs of the interface. What it needs is checked against the catalogue before anything is
written: a description that asks for what nothing here serves is refused at compile time,
with the person at the prompt asked whether to narrow it, rather than compiled into a flow
that fails at hour three.

Then the writer writes the flow, in a scratch directory of its own, and the flow is held to
three gates before anybody keeps it. The checker reads it without running it. The stubs drive
it against the worst worlds there are -- the reviewer that never says done, the turn that
always fails -- in a subprocess held to a clock, so a loop that cannot end is caught in
milliseconds. And a critic that shares nothing with the writer reads it fresh against the
spec. Whatever any gate refuses goes back to the writer's own session, word for word, for as
many repairs as the config allows.

One rule is the compiler's own and not the checker's: a generated loop is bounded, always.
The checker only warns about a loop whose every way out waits on an agent's verdict; here
that warning is a refusal, whatever `strict` says, so the flow that lands ends even when its
reviewer never says done. What lands is copied whole into the flows of your own -- atomically,
the way `fork` copies one -- and the compile ends with a report: what it is called, what it
drives, how every loop ends, and the line that runs it.
"""

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
    """The three the compile drives: two agents that share nothing, and the person.

    The writer holds the whole compile in one session -- the spec it drew, the drafts it
    wrote, every refusal it was handed -- because repair is a conversation. The critic
    arrives fresh each round and reads only the draft against the spec, which is the one
    reviewer arrangement that catches what the writer has talked itself into. The person
    is the gate for what a compiler must not decide alone: an ask nothing serves, a name
    already taken, a draft the repairs ran out on.
    """

    writer: Agent
    critic: Agent
    human: Person


class Config(BaseModel):
    """What a compile takes."""

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
    """One agent the compiled flow will drive.

    Every field required, here and in every shaped answer of this flow: a backend that
    holds a model to a strict schema refuses one whose fields have defaults, and the
    compiler must compile on any backend that shapes.
    """

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
    """One knob the compiled flow can be set up with."""

    model_config = {"extra": "forbid"}

    name: str = Field(description="the field's name, snake_case")
    kind: Literal["number", "text", "switch"] = Field(description="what it holds")
    default: str = Field(description="the default, written out -- '10.0', 'off'")
    about: str = Field(description="one line saying what it does, for whoever sets it")


class Ending(BaseModel):
    """One way the compiled flow ends."""

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
    """What the flow is to be, drawn from the description before anything is written."""

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
    """A yes or no the compiler must not answer for itself."""

    model_config = {"extra": "forbid"}

    proceed: bool = Field(description="yes to go on as asked, no to stop the compile")


class Renamed(BaseModel):
    """Another name, where the one the spec chose is already taken."""

    model_config = {"extra": "forbid"}

    name: str = Field(description="another name for the flow, or '' to stop the compile")


@flow
def run(agents: Compiling, task: str, config: Config | None = None) -> None:
    held = config or Config()
    # A scratch directory of the compile's own: drafts are proved there, and only what
    # passed every gate is copied out. Taken away however the compile ends, so a refused
    # draft is nowhere.
    scratch = tempfile.mkdtemp(prefix=".aot.")
    try:
        _compiled(agents, task, held, Path(scratch))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _compiled(agents: Compiling, task: str, held: Config, scratch: Path) -> None:
    # One session for the whole compile: the spec it drew and every refusal it was handed
    # are the context its repairs are made of.
    writing = agents.writer.new(cwd=scratch)
    spec = _drafted(writing, task)
    if spec is None:
        print("hmz: aot: the writer could not draw a spec from the description; nothing "
              "was written")
        return
    unserved, limited = _unserved(spec)
    if unserved:
        # The writer's own round first: an unserved ask is more often the description's
        # words taken for a capability -- writing a file, reading the repository -- than
        # a real hole, and the writer can restate the spec in the catalogue's vocabulary
        # before anybody is asked to build less.
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
            # There is nothing to take: the writer never landed a draft at all.
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
    """The spec, drawn from the description against the briefing -- with one more try.

    Args:
      writing: The writer's session, whose first turn this is.
      task: The description, as it was given.

    Returns:
      The spec, or None for a writer that would not answer in shape twice.
    """
    asked = prompts.SPEC.format(briefing=briefed(), task=task)
    spec = writing(asked, suppress=True, schema=Spec)
    if spec is None:
        spec = writing(prompts.SPEC_AGAIN, suppress=True, schema=Spec)
    return spec


def _named(said: str) -> str:
    """A flow's name as a directory may be called: snake_case, and never empty."""
    held = "".join(one if one.isalnum() else "_" for one in said.strip().lower())
    held = "_".join(part for part in held.split("_") if part)
    return held or "compiled_flow"


def _unserved(spec: Spec) -> tuple[list[str], list[str]]:
    """The spec's needs, checked against the catalogue of what is actually served.

    Args:
      spec: What the flow is to be.

    Returns:
      What nothing here serves -- each one a reason not to compile -- and what only some
      backends serve, which is compiled and said in the report.
    """
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
    """Puts an ask nothing serves to the person: narrow the flow, or stop here.

    Args:
      human: The person at the prompt, who answers nothing when nobody is there.
      unserved: What was asked for that nothing here serves.

    Returns:
      Whether to compile the rest. Nobody there is no: a compiler must not decide alone
      to build less than what was asked for.
    """
    for one in unserved:
        print(f"hmz: aot: cannot compile -- asks for {one}, which nothing here serves")
    going = human(
        prompts.NARROW.format(unserved="\n".join(f"- {one}" for one in unserved)),
        suppress=True,
        schema=Going,
    )
    return going is not None and going.proceed


def _refused(draft: Path, spec: Spec, held: Config, agents: Compiling) -> str:
    """The three gates, in their order, and what the first to refuse said.

    Args:
      draft: Where the writer was told to put the flow.
      spec: What it is to be.
      held: The compile's config.
      agents: For the critic, who reads the draft fresh.

    Returns:
      What to hand the writer, or "" for a draft every gate let through.
    """
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
    """Whether one finding sends a draft back.

    Every error does. `unbounded-loop` does whatever `strict` says: it is the compiler's
    own rule that a generated loop is bounded, since the flow that lands must end even
    when its reviewer never says done. The rest of the warnings block only under
    `strict`, and are said in the report either way.
    """
    return one.severity == "error" or one.code == "unbounded-loop" or strict


def _worlds(seconds: float) -> tuple[Scenario, ...]:
    """The scenarios every draft is driven against, at the compile's own clock."""
    return tuple(
        one._replace(seconds=seconds) for one in (NEVER_DONE, ALWAYS_DONE, SILENT)
    )


def _said(findings: Sequence[Finding]) -> str:
    """Findings as the writer is handed them, one a line."""
    return "\n".join(
        f"- {one.where.name}:{one.line}: {one.severity}: {one.code}: {one.said}"
        for one in findings
    )


def _taken(human: Person, feedback: str) -> bool:
    """The last gate: take the draft with what is still wrong with it, or stop.

    Args:
      human: The person at the prompt.
      feedback: The last refusal, which is what they would be taking.

    Returns:
      Whether to keep it anyway. Nobody there is no.
    """
    going = human(
        prompts.TAKEN.format(refused=feedback), suppress=True, schema=Going
    )
    return going is not None and going.proceed


def _landed(draft: Path, name: str, into: str, human: Person) -> str | None:
    """Copies the draft into the flows of your own, whole and atomically.

    The way `fork` lands one: copied beside and then moved into place, so a copy that
    fails partway leaves no half a flow under the name. A name already taken is the
    person's to change, not the compiler's to write over.

    Args:
      draft: The draft that passed the gates.
      name: What it is to be called.
      into: Which of the two places of your own, `local` or `user`.
      human: The person, for a name already taken.

    Returns:
      The directory it landed in, or None for a landing refused.
    """
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
    """Says what was compiled: what it is, what it drives, how it ends, how to run it.

    Args:
      at: Where the flow landed.
      spec: What it was compiled to be.
      held: The compile's config.
      limited: What it builds on that only some backends serve.
    """
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
