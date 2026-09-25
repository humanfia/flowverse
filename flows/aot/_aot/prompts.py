SPEC = """You are the writer half of a compiler that turns a description into a humanize \
flow. Below is a briefing of what this installed humanize lets a flow declare, and then the \
description. Read both and answer with the spec of the flow to be written -- do not write \
any code yet.

Ground rules for the spec:
- `needs` names every capability the flow declares -- agent mixins and environment mixins \
alike -- spelled exactly as the briefing spells them. If the description asks for something \
the briefing does not serve, still list it in `needs` as the description asked for it: the \
compiler refuses it honestly rather than building around it silently.
- `seats` is every agent the flow drives, the person outside the run included only if the \
flow talks to them. A seat's `mixins` names only the agent capabilities that role truly \
uses: each one narrows which harnesses can fill it.
- `endings` must hold at least one. A `verdict` ending never stands alone: it travels with \
a round cap, because an agent may never say the verdict. The budget a run is given with \
`-b` stops any flow, but it is the runner's, not an ending the flow implements.
- `name` is snake_case, short, and says what the flow does.

The briefing:

{briefing}

The description:

{task}"""

SPEC_AGAIN = """Your last answer did not fit the shape asked for. Answer again with the \
spec alone, exactly in the shape: every field, nothing outside it."""

WRITE = """Now write the flow the spec describes. You are working in a scratch directory; \
create the flow at exactly this path:

    {draft}

as a directory called `{name}` holding the `__init__.py` that is the flow -- plus whatever \
it imports beside itself (an underscore-named sibling module or package inside the flow's \
own directory, imported by its plain name -- `from _{name} import helpers`, never \
`from ._{name} import ...`), and a `skills/` directory only if a role declares skills.

Follow the writing-flows skill you carry: it is the contract this draft will be held to. \
The compiler will load the draft through humanize's engine, run it on fakes in the worst \
worlds there are -- an agent that never says done, one that says done at once, one that \
answers nothing -- with nobody at the prompt, and hand it to a fresh critic. The rules that \
decide most refusals:

- The entry flow is an `async def` named `{name}`, decorated with \
`@flow(agents=..., envs=..., params=...)`, taking `(task, *, agents, envs, params, ctx)`.
- Every loop has a bound of its own: `for round_ in range(params.rounds):`, or another \
deterministic bound. The run's budget is never the flow's to declare or to rely on; a draft \
that runs until the smoke's budget stops it is refused, always.
- The flow ends by returning, in every world. Nothing in it blocks the event loop: \
`await asyncio.sleep(...)`, never `time.sleep`; `await env.exec([...])`, never \
`subprocess`. Those runs are of the draft alone, in an empty workspace where no agent writes \
a file: a file an agent was to write may be missing, and the flow survives that.
- Every param has a default, since `hmz exec` runs the flow with none set; a question put \
to the person outside the run has a default for every field, since nobody is there.
- One import of humanize's: `from hmz.flows import ...`, and only names it offers.

The spec:

{spec}

Write the files now, and end by saying what you wrote where."""

REPAIR = """The draft at {draft} was refused. Here is everything found, exactly as the \
gates said it:

{refused}

Fix the draft in place -- edit the files at {draft} -- addressing every line above. Keep to \
the writing-flows contract: an async entry flow named after its directory, every loop with \
its own bound, a return in every world, nothing blocking, one import of humanize's. End by \
saying what you changed."""

REVIEW = """You are the critic half of a compiler that turns a description into a humanize \
flow. A writer you share nothing with has produced a draft; it has already been loaded \
through humanize's engine and run to its end on fakes, so what is left is what only reading \
can catch: does it do what the spec says, and would you run it?

The draft is the directory `{draft}` in your working directory -- read every file in it \
with your tools. Judge it against this spec:

{spec}

Hold it to the writing-flows contract: an async entry flow named after its directory, with \
its agents, environments and params declared as `AgentCollection`, `EnvCollection` and \
`FlowParams` subclasses; a role declares exactly the capabilities it uses, and no more; \
every loop has its own bound even where an ending is a verdict, and no budget is declared \
or implemented by the flow; a shaped answer's model has every field required and described, \
and a malformed one is caught where a loop should survive it; params each have a default \
and a description; nothing blocks the event loop; a docstring whose first line says what \
the flow does, with the `hmz exec` line under it; prints that say where a long run has got \
to. Approve only what you would run on a repository of your own. Answer in the shape."""

NARROW = """This description asks for things nothing in this humanize serves:

{unserved}

Compile the rest without them? Answering no -- or nothing -- stops the compile."""

TAKEN = """The repairs ran out. The last refusal was:

{refused}

Keep the draft anyway, as it stands? It will land with the findings above still in it. \
Answering no -- or nothing -- stops the compile and keeps nothing."""

RENAME = """There is already a flow called {name!r} where this one is to land, and a \
compiler does not write over what somebody keeps. Give another name for the compiled \
flow, or answer nothing to stop the compile."""

RESAID = """Some of the spec's capabilities name nothing the briefing serves:

{unserved}

A capability is one of the briefing's mixin names, spelled exactly as the briefing spells \
it, and nothing else belongs in `needs` or in a seat's `mixins`. An ordinary ability -- \
taking turns, reading the repository, writing files through the agent, printing -- is not a \
capability to declare: every agent has it, so drop it. Only if the description truly \
requires something the briefing does not serve should you keep it listed, exactly as the \
description asks, and the compile will stop honestly. Answer with the whole corrected spec, \
in the shape."""
