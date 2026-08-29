"""The prompts of the compile, one constant per turn the flow takes.

Kept beside the flow rather than inline, so a fork that wants its compiler to speak
differently edits this file and runs. What each turn is *for* is the flow's own docstrings;
what is said to get it is here.
"""

#: The writer's first turn: the description read against the briefing, answered as a Spec.
SPEC = """You are the writer half of a compiler that turns a description into a humanize \
flow. Below is a briefing of what this installed humanize actually serves, and then the \
description. Read both and answer with the spec of the flow to be written -- do not write \
any code yet.

Ground rules for the spec:
- `needs` names only capabilities the briefing names, spelled exactly as it spells them. \
If the description asks for something the briefing does not serve, still list it in \
`needs` as the description asked for it: the compiler refuses it honestly rather than \
building around it silently.
- `seats` is every agent the flow drives, the person at the prompt included only if the \
flow talks to them. A seat's `moments` names only moments from the briefing's \
"only some backends" list that the seat truly hangs hooks on.
- `endings` must hold at least one, and a `verdict` ending never stands alone: it travels \
with a budget or a round cap, because an agent may never say the verdict.
- `name` is snake_case, short, and says what the flow does.

The briefing:

{briefing}

The description:

{task}"""

#: One more try at the spec, for a first answer that was not in shape.
SPEC_AGAIN = """Your last answer did not fit the shape asked for. Answer again with the \
spec alone, exactly in the shape: every field, nothing outside it."""

#: The writer's second turn: the spec written out as a flow, in the scratch directory.
WRITE = """Now write the flow the spec describes. You are working in a scratch directory; \
create the flow at exactly this path:

    {draft}

as a directory called `{name}` holding the `__init__.py` that is the flow -- plus whatever \
it imports beside itself (an underscore-named sibling module or package inside the flow's \
own directory), and a `skills/` directory only if the flow brings skills.

Follow the writing-flows skill you carry: it is the contract this draft will be held to. \
The compiler will read the draft without running it, drive it with stubs against the worst \
worlds there are -- a reviewer that never says done, turns that always fail -- and hand it \
to a fresh critic. Three rules decide most refusals:

- Every loop is bounded. Even where the spec ends by verdict, the loop also ends by budget \
(`agent.spent().output` against a limit) or by a round cap (`for` over a `range`). A loop \
whose only way out is an agent's verdict is refused, always.
- Every shaped answer is guarded. A turn taken with `suppress=True, schema=...` answers \
None when it fails; test it before reading a field off it.
- One import of humanize's: `from hmz.flows import ...`, and only names it offers.

The spec:

{spec}

Write the files now, and end by saying what you wrote where."""

#: A refused draft handed back, word for word, to the session that wrote it.
REPAIR = """The draft at {draft} was refused. Here is everything found, exactly as the \
gates said it:

{refused}

Fix the draft in place -- edit the files at {draft} -- addressing every line above. Keep to \
the writing-flows contract: every loop bounded, every shaped answer guarded, one import of \
humanize's. End by saying what you changed."""

#: The critic's whole turn: fresh eyes, the spec, and the draft on disk.
REVIEW = """You are the critic half of a compiler that turns a description into a humanize \
flow. A writer you share nothing with has produced a draft; it has already passed a static \
checker and been driven to completion by stubs, so what is left is what only reading can \
catch: does it do what the spec says, and would you run it?

The draft is the directory `{draft}` in your working directory -- read every file in it \
with your tools. Judge it against this spec:

{spec}

Hold it to the writing-flows contract: every loop bounded even where an ending is a \
verdict; every shaped answer guarded before a field is read; settings as a frozen or \
extra-forbidding pydantic model with a described field apiece; a docstring whose first \
line says what the flow does, with the `hmz exec` line under it; prints that say where a \
long run has got to. Approve only what you would run on a repository of your own. Answer \
in the shape."""

#: The person's gate for an ask nothing serves: narrow the flow, or stop the compile.
NARROW = """This description asks for things nothing in this humanize serves:

{unserved}

Compile the rest without them? Answering no -- or nothing -- stops the compile."""

#: The person's last gate: the repairs ran out, and the draft stands as it is.
TAKEN = """The repairs ran out. The last refusal was:

{refused}

Keep the draft anyway, as it stands? It will land with the findings above still in it. \
Answering no -- or nothing -- stops the compile and keeps nothing."""

#: The person's gate for a name already taken at the destination.
RENAME = """There is already a flow called {name!r} where this one is to land, and a \
compiler does not write over what somebody keeps. Give another name for the compiled \
flow, or answer nothing to stop the compile."""

#: The writer's own round on an ask nothing serves, before the person is troubled with it.
RESAID = """Some of the spec's `needs` name nothing the briefing serves:

{unserved}

A need is one of the briefing's capability names, spelled exactly as the briefing spells \
it, and nothing else belongs in `needs`. An ordinary ability -- writing files, reading the \
repository, taking turns, printing -- is not a capability to declare: every agent has it, \
so drop it from `needs`. Only if the description truly requires something the briefing \
does not serve should you keep it listed, exactly as the description asks, and the compile \
will stop honestly. Answer with the whole corrected spec, in the shape."""
