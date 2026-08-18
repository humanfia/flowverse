"""humanize1: what it refuses to be set up as, and what a round has to get past.

The flow is PolyArch/humanize, so what is checked here is that it still is: the combinations
the plugin cannot run are refused before a turn is taken, the markers it reads are read the
same way, and the gates its stop hook runs still refuse the same rounds.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from hmz.agents import HumanAgent, Moment, Occasion, Question
from hmz.flows import configures, drives, held, resumes, wanted

import humanize1
from _humanize1 import guards, loop, prompts
from humanize1 import Idea, Plan, Rlcr

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel


def _loop(tmp_path: Path, **fields: object) -> loop.Loop:
    """A loop of the shape a run has, with nothing driving it."""
    where = tmp_path / ".humanize" / "rlcr" / "now"
    where.mkdir(parents=True)
    state = loop.State(plan_file="docs/plan.md", start_branch="main", **fields)  # pyright: ignore[reportArgumentType]
    running = loop.Loop(None, where, tmp_path, state)  # pyright: ignore[reportArgumentType]
    (where / "state.md").write_text(state.written())
    return running


# ------------------------------------------------------------------------------------
# What the flow drives, and what it says it can be set up with
# ------------------------------------------------------------------------------------


def test_the_file_holds_three_flows_and_names_each_of_them() -> None:
    """One per command of the plugin, which is what `<flow>:<name>` addresses."""
    said = {one.name: one.about for one in held(humanize1.__file__)}

    assert set(said) == {"gen-idea", "gen-plan", "rlcr"}
    assert said["gen-idea"] == "Opens a loose idea into a repo-grounded draft."


@pytest.mark.parametrize(
    ("inside", "carries_on"),
    [("gen-idea", False), ("gen-plan", False), ("rlcr", True)],
)
def test_only_the_loop_says_it_can_be_picked_up(inside: str, carries_on: bool) -> None:
    """The two phases in front of it hand over a file, and running one again writes another.

    The loop is the one that is meant to run for days, and the one with somewhere to carry
    on from: a directory of rounds it left half finished.
    """
    assert resumes(f"{humanize1.__file__}:{inside}") is carries_on


@pytest.mark.parametrize(
    ("inside", "agents"),
    [
        ("gen-idea", ("drafter",)),
        ("gen-plan", ("planner", "analyst")),
        ("rlcr", ("builder", "reviewer")),
    ],
)
def test_each_phase_asks_only_for_its_own_agents(
    inside: str, agents: tuple[str, ...]
) -> None:
    """Which is what splitting them buys: `/agents` asks two questions, not five."""
    assert drives(f"{humanize1.__file__}:{inside}") == agents


def test_the_builder_has_to_be_one_a_hook_can_say_no_to() -> None:
    """The plugin's validators are what keep the plan fixed, and they refuse tool calls."""
    places = {place.name: place for place in wanted(f"{humanize1.__file__}:rlcr")}

    assert Moment.PERMISSION_REQUEST in places["builder"].moments
    # And nobody is asked what the person runs, so they are not among the two.
    assert "human" not in places


@pytest.mark.parametrize(
    ("inside", "model"), [("gen-idea", Idea), ("gen-plan", Plan), ("rlcr", Rlcr)]
)
def test_each_phase_says_it_can_be_set_up_with_its_own_flags(
    inside: str, model: type[BaseModel]
) -> None:
    """Which is how anything starting one finds out there is anything to ask about."""
    said = configures(f"{humanize1.__file__}:{inside}")

    assert said is not None
    assert set(said.model_fields) == set(model.model_fields)


def test_every_flag_the_plugin_takes_is_a_field() -> None:
    """One field per flag, under the plugin's own name for it, on the phase it belongs to."""
    assert {"n", "output"} == set(Idea.model_fields)
    assert {
        "input",
        "output",
        "mode",
        "auto_start_rlcr_if_converged",
        "alternative_plan_language",
    } == set(Plan.model_fields)
    assert {
        "plan_file",
        "max",
        "codex_timeout",
        "full_review_round",
        "base_branch",
        "track_plan_file",
        "push_every_round",
        "skip_impl",
        "claude_answer_codex",
        "agent_teams",
        "skip_quiz",
        "yolo",
        "privacy",
        "require_bitlesson_entry_for_none",
    } == set(Rlcr.model_fields)


def test_the_defaults_are_the_plugin_s_own() -> None:
    """A run nobody set up is the run the plugin does with no flags at all."""
    assert (Idea().n, Rlcr().max, Rlcr().full_review_round) == (6, 42, 5)
    assert Rlcr().codex_timeout == 5400
    assert Plan().mode == "discussion"


def test_yolo_is_the_two_flags_it_is_a_name_for() -> None:
    """As `--yolo` is in the plugin: an alias, spelled out where it is read."""
    config = Rlcr(yolo=True)

    assert (config.skip_quiz, config.claude_answer_codex) == (True, True)


def test_a_plan_asked_for_before_there_is_a_draft_is_refused(tmp_path: Path) -> None:
    """Before a turn is taken, and by the flow rather than by whatever is asking."""
    with pytest.raises(ValueError, match="no draft to plan from"):
        humanize1._last(tmp_path)


def test_the_draft_a_plan_starts_from_is_the_last_one_written(tmp_path: Path) -> None:
    """What `gen-idea` left behind, which is what the two of them being one flow did."""
    ideas = tmp_path / humanize1.IDEAS
    ideas.mkdir(parents=True)
    (ideas / "older.md").write_text("old")
    newer = ideas / "newer.md"
    newer.write_text("new")
    os.utime(ideas / "older.md", (1, 1))

    assert humanize1._last(tmp_path) == newer


@pytest.mark.parametrize(
    ("model", "fields"),
    [
        (Idea, {"n": 1}),
        (Idea, {"n": 11}),
        (Rlcr, {"full_review_round": 1}),
        (Rlcr, {"max": -1}),
    ],
)
def test_a_setting_outside_what_the_plugin_takes_is_refused(
    model: type[BaseModel], fields: dict[str, object]
) -> None:
    """`--n` is 2 to 10 and `--full-review-round` is at least 2, as the scripts check."""
    with pytest.raises(ValueError, match="Input should be"):
        model.model_validate(fields)


# ------------------------------------------------------------------------------------
# What the loop reads out of a review
# ------------------------------------------------------------------------------------


def test_a_failed_turn_is_taken_again_and_only_that_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round is hours and a review is one question about it, so they retry separately.

    Letting a failed review send the round back would pay for the expensive half twice to
    recover from the cheap half.
    """
    import subprocess as sub
    from typing import cast

    from hmz.agents import SessionBase

    def instant(_seconds: float) -> None:
        """The wait between rounds, taken out of the test."""

    monkeypatch.setattr("time.sleep", instant)
    taken: list[str] = []

    class _Flaky:
        def __call__(self, prompt: str, *, suppress: bool = False) -> str:
            taken.append(prompt)
            if len(taken) <= 2:
                if not suppress:
                    # The flow has to ask for the turn suppressed, or a loop that runs for
                    # days ends on the first turn that failed.
                    raise sub.CalledProcessError(1, ["claude"])
                return ""  # what a suppressed turn that failed answers with
            # Exited clean having said nothing, which is not an answer either: forwarding it
            # would spend a round asking the other side to reply to silence.
            return "" if len(taken) == 3 else "answered"

    said, _ = loop.spoken(cast("SessionBase", _Flaky()), "do it")
    assert said == "answered"
    assert taken == ["do it"] * 4  # the same turn, four times, and no other


@pytest.mark.parametrize(
    ("said", "found"),
    [
        ("Mainline Progress Verdict: ADVANCED", "advanced"),
        ("mainline progress verdict: stalled\n", "stalled"),
        ("Mainline Progress Verdict: REGRESSED (see below)", "regressed"),
        ("Mainline Progress Verdict: ADVANCED / STALLED / REGRESSED", "unknown"),
        ("nothing about it at all", "unknown"),
    ],
)
def test_the_mainline_verdict_is_read_as_the_hook_reads_it(
    said: str, found: str
) -> None:
    """The template line states all three, and is not a verdict -- the hook says so too."""
    assert loop.verdict(said) == found


def test_the_last_verdict_line_is_the_one_that_counts() -> None:
    """A review that quotes the format and then states one has stated one."""
    said = "Mainline Progress Verdict: ADVANCED\nand later\nMainline Progress Verdict: STALLED"

    assert loop.verdict(said) == "stalled"


@pytest.mark.parametrize(
    ("said", "accepted"),
    [
        ("holds.\n\nCOMPLETE", True),
        ("holds.\n\n  COMPLETE  \n\n", True),
        # The plugin is strict on purpose: a review that says the opposite while quoting
        # the word must not end the run, and neither must one that trails a full stop.
        ("holds.\n\ncomplete.", False),
        ("CANNOT COMPLETE", False),
        (
            "Answer with the single word\nCOMPLETE\nif it holds. It does not: AC-2.",
            False,
        ),
    ],
)
def test_only_the_last_line_being_the_word_accepts_the_work(
    said: str, accepted: bool
) -> None:
    """Nothing between the two agents parses anything, so one word has to carry the verdict."""
    assert (loop._last(said) == loop.COMPLETE) is accepted


def test_findings_are_taken_from_the_first_marker_to_the_end() -> None:
    """A `[P0-9]` in the first ten characters of a line, in the last fifty lines."""
    said = "some reasoning\n- [P1] a thing - a.py:1\n  why\n- [P3] another - b.py:2\n"

    found = loop.issues(said)

    assert found.startswith("## Code Review Issues")
    assert "- [P1] a thing" in found
    assert "some reasoning" not in found


def test_a_marker_in_the_middle_of_a_line_is_not_a_finding() -> None:
    """Which is what keeps a review that explains the format from reading as one."""
    assert loop.issues("the reviewer writes findings like [P0] this one") == ""


def test_a_review_with_nothing_to_fix_finds_nothing() -> None:
    """And is what moves the loop into the finalize phase."""
    assert loop.issues("Everything looks good. Nothing to fix before this ships.") == ""


# ------------------------------------------------------------------------------------
# What the loop left behind, read back
# ------------------------------------------------------------------------------------


def test_the_state_file_reads_back_as_the_state_that_wrote_it(tmp_path: Path) -> None:
    """Which is what picking a loop up rests on: that file is the whole of what it left."""
    state = loop.State(
        current_round=3,
        max_iterations=9,
        codex_model="gpt-5.6-sol",
        codex_timeout=60,
        plan_file="docs/plan.md",
        plan_tracked=True,
        start_branch="main",
        base_branch="main",
        base_commit="a17c0de",
        review_started=True,
        bitlesson_required=False,
        mainline_stall_count=2,
        last_mainline_verdict=loop.STALLED,
        started_at="2026-01-01T00:00:00Z",
    )
    at = tmp_path / "state.md"
    at.write_text(state.written(), encoding="utf-8")

    assert loop.State.read(at) == state


def test_a_loop_directory_with_no_state_file_holds_no_state(tmp_path: Path) -> None:
    """A loop that ended renamed it on the way out, and a loop that was deleted has none."""
    assert loop.State.read(tmp_path / "state.md") is None


@pytest.mark.parametrize(
    ("was", "now"),
    [
        # A field gone, and a field this version has never had: each is a state file some
        # other version of the flow wrote.
        ("start_branch: \n", ""),
        ("---\n", "---\nrung: 3\n"),
        # A number that is not one, and a switch that is neither of the two words.
        ("max_iterations: 42", "max_iterations: as many as it takes"),
        ("privacy_mode: false", "privacy_mode: perhaps"),
        # And a file that is not this file at all: no frontmatter, or prose inside it.
        ("---\n", ""),
        ("---\n", "---\nsomebody was here\n"),
    ],
)
def test_a_state_file_this_version_did_not_write_is_not_carried_on(
    tmp_path: Path, was: str, now: str
) -> None:
    """Half a state read back is worse than none: the loop is fifteen gates deep in it."""
    at = tmp_path / "state.md"
    at.write_text(loop.State().written().replace(was, now, 1), encoding="utf-8")

    assert loop.State.read(at) is None


# ------------------------------------------------------------------------------------
# What a round has to get past
# ------------------------------------------------------------------------------------


def test_a_round_with_no_summary_is_refused(tmp_path: Path) -> None:
    """The first thing the hook checks that the builder can actually do something about."""
    running = _loop(tmp_path)

    refused = running._summary_written(Occasion(moment=Moment.STOP, agent="builder"))

    assert refused is not None
    assert "Work Summary Missing" in refused.because


def test_a_round_with_no_contract_is_refused(tmp_path: Path) -> None:
    """The round contract is what keeps one round to one objective."""
    running = _loop(tmp_path)
    running.summary.write_text("# Round 0 Summary\n")

    refused = running._contract_written(Occasion(moment=Moment.STOP, agent="builder"))

    assert refused is not None
    assert "Round Contract Missing" in refused.because


def test_a_goal_tracker_left_as_a_placeholder_is_refused(tmp_path: Path) -> None:
    """Round 0 is where the immutable half is written, so round 0 is where this is checked."""
    running = _loop(tmp_path)
    running.tracker.write_text(
        prompts.render(
            prompts.GOAL_TRACKER,
            GOAL_SECTION="[To be extracted from plan by the builder in Round 0]",
            AC_SECTION="[To be defined by the builder in Round 0 based on the plan]",
        )
    )

    refused = running._goal_tracker_started(
        Occasion(moment=Moment.STOP, agent="builder")
    )

    assert refused is not None
    assert "Ultimate Goal" in refused.because
    assert "Acceptance Criteria" in refused.because


@pytest.mark.parametrize(
    ("delta", "because"),
    [
        ("", "BitLesson Delta Missing"),
        ("## BitLesson Delta\n- Action: sideways\n", "must include one action"),
        (
            "## BitLesson Delta\n- Action: none\n- Lesson ID(s): BL-1\n",
            "does not match",
        ),
        (
            "## BitLesson Delta\n- Action: add\n- Lesson ID(s): NONE\n",
            "requires concrete",
        ),
        (
            (
                "## BitLesson Delta\n- Action: add\n- Lesson ID(s): BL-1\n"
                "- Notes: [what changed and why]\n"
            ),
            "requires a `Notes:` field",
        ),
    ],
)
def test_a_bitlesson_delta_that_does_not_add_up_is_refused(
    tmp_path: Path, delta: str, because: str
) -> None:
    """Every rule `bitlesson-validate-delta.sh` applies, in the order it applies them."""
    running = _loop(tmp_path)

    refused = running._delta(f"# Round 0 Summary\n\n{delta}")

    assert refused is not None
    assert because in refused.because


def test_a_delta_that_names_a_lesson_the_project_has_is_taken(tmp_path: Path) -> None:
    """Which is the only way `add` gets through: the lesson has to be there to be added."""
    running = _loop(tmp_path)
    lessons = tmp_path / running.state.bitlesson_file
    lessons.parent.mkdir(parents=True, exist_ok=True)
    lessons.write_text("## Lesson: one\nLesson ID: BL-20260101-one\n")

    said = (
        "# Round 0 Summary\n\n## BitLesson Delta\n- Action: add\n"
        "- Lesson ID(s): BL-20260101-one\n- Notes: the retry needed a backoff\n"
    )

    assert running._delta(said) is None


def test_a_delta_of_none_is_taken_when_nothing_was_learned(tmp_path: Path) -> None:
    """Which is what most rounds say, and is why the empty case is allowed by default."""
    running = _loop(tmp_path)

    said = "# Round 0 Summary\n\n## BitLesson Delta\n- Action: none\n- Lesson ID(s): NONE\n"

    assert running._delta(said) is None


def test_none_is_refused_where_the_run_asked_for_a_lesson_a_round(
    tmp_path: Path,
) -> None:
    """`--require-bitlesson-entry-for-none`, which is the flag's whole effect."""
    running = _loop(tmp_path, bitlesson_allow_empty_none=False)

    said = "# Round 0 Summary\n\n## BitLesson Delta\n- Action: none\n- Lesson ID(s): NONE\n"
    refused = running._delta(said)

    assert refused is not None
    assert "BitLesson Recording Required" in refused.because


# ------------------------------------------------------------------------------------
# What the builder may not do while the loop runs
# ------------------------------------------------------------------------------------


def _asked(tool: str, **called: object) -> Occasion:
    """One tool call, as the moment a backend asks whether it may run."""
    return Occasion(
        moment=Moment.PERMISSION_REQUEST, agent="builder", tool=tool, input=called
    )


def test_the_state_file_is_not_the_builder_s_to_write(tmp_path: Path) -> None:
    """However it reaches for it: the tool, or a shell command that writes the same file."""
    running = _loop(tmp_path)
    guard = guards.Guard(running, tmp_path)

    written = guard(_asked("Write", file_path=str(running.where / "state.md")))
    shelled = guard(_asked("Bash", command=f"sed -i s/0/9/ {running.where}/state.md"))

    assert written is not None
    assert "State File Modification" in written.because
    assert shelled is not None
    assert "State File Modification" in shelled.because


def test_the_plan_is_fixed_once_the_loop_has_started(tmp_path: Path) -> None:
    """Both of them: the plan itself, and the backup the loop checks it against."""
    running = _loop(tmp_path)
    guard = guards.Guard(running, tmp_path)

    plan = guard(_asked("Edit", file_path=str(tmp_path / "docs" / "plan.md")))
    backup = guard(_asked("Write", file_path=str(running.where / "plan.md")))

    assert plan is not None
    assert "Plan File Modified" in plan.because
    assert backup is not None
    assert "Plan Backup Protected" in backup.because


def test_a_round_writes_its_own_round_s_summary_and_no_other(tmp_path: Path) -> None:
    """Incrementing the round number is the loop's job, and doing it is how a round is lost."""
    running = _loop(tmp_path, current_round=2)
    guard = guards.Guard(running, tmp_path)

    ahead = guard(_asked("Write", file_path=str(running.where / "round-3-summary.md")))
    mine = guard(_asked("Write", file_path=str(running.where / "round-2-summary.md")))

    assert ahead is not None
    assert "Wrong Round Number" in ahead.because
    assert mine is None


def test_the_summary_goes_in_the_loop_directory(tmp_path: Path) -> None:
    """A summary written anywhere else is a summary the review will not read."""
    running = _loop(tmp_path)
    guard = guards.Guard(running, tmp_path)

    refused = guard(_asked("Write", file_path=str(tmp_path / "round-0-summary.md")))

    assert refused is not None
    assert "Wrong Summary Location" in refused.because


def test_the_builder_may_not_rewrite_its_own_instructions(tmp_path: Path) -> None:
    """The round prompt is what the reviewer said, and is not the builder's to edit."""
    running = _loop(tmp_path)
    guard = guards.Guard(running, tmp_path)

    refused = guard(_asked("Write", file_path=str(running.where / "round-0-prompt.md")))

    assert refused is not None
    assert "Prompt File Write Blocked" in refused.because


def test_the_immutable_half_of_the_tracker_is_immutable_after_round_zero(
    tmp_path: Path,
) -> None:
    """An edit that replaces something above the divider is rewriting what was fixed."""
    running = _loop(tmp_path, current_round=1)
    running.tracker.write_text(
        "## IMMUTABLE SECTION\n### Ultimate Goal\nship the thing\n"
        "## MUTABLE SECTION\n### Plan Version: 1\n"
    )
    guard = guards.Guard(running, tmp_path)

    above = guard(
        _asked("Edit", file_path=str(running.tracker), old_string="ship the thing")
    )
    below = guard(
        _asked("Edit", file_path=str(running.tracker), old_string="### Plan Version: 1")
    )

    assert above is not None
    assert "Goal Tracker Update Blocked" in above.because
    assert below is None


def test_the_tracker_may_be_written_whole_in_round_zero(tmp_path: Path) -> None:
    """Round 0 is where it is initialized, which is a write of the whole file."""
    running = _loop(tmp_path)
    running.tracker.write_text("## IMMUTABLE SECTION\n## MUTABLE SECTION\n")

    assert (
        guards.Guard(running, tmp_path)(_asked("Write", file_path=str(running.tracker)))
        is None
    )


def test_a_push_is_refused_unless_the_run_asked_for_one(tmp_path: Path) -> None:
    """Commits stay local until `--push-every-round` says otherwise."""
    quiet = guards.Guard(_loop(tmp_path / "a"), tmp_path / "a")
    pushing = guards.Guard(_loop(tmp_path / "b", push_every_round=True), tmp_path / "b")
    (tmp_path / "a").mkdir(exist_ok=True)
    (tmp_path / "b").mkdir(exist_ok=True)

    refused = quiet(_asked("Bash", command="git push origin main"))

    assert refused is not None
    assert "Git Push Blocked" in refused.because
    assert pushing(_asked("Bash", command="git push origin main")) is None


def test_git_add_all_is_refused_where_the_loop_has_state_to_lose(
    tmp_path: Path,
) -> None:
    """`.humanize/` is the loop's own, and `git add -A` is how it ends up committed."""
    running = _loop(tmp_path)
    guard = guards.Guard(running, tmp_path)

    refused = guard(_asked("Bash", command="git add -A"))

    assert refused is not None
    assert ".humanize Protection" in refused.because
    assert guard(_asked("Bash", command="git add src/humanize/runner.py")) is None


@pytest.mark.parametrize(
    ("command", "guarded"),
    [
        # A command that only reads the loop's own files is none of the loop's business.
        ("cat .humanize/rlcr/now/state.md", False),
        ("grep -n Action .humanize/rlcr/now/round-0-summary.md", False),
        ("pytest -q tests/test_x.py", False),
        # And one that writes them is, however it writes them.
        ("sed -i s/0/9/ .humanize/rlcr/now/state.md", True),
        ("echo done > .humanize/rlcr/now/round-0-summary.md", True),
        ("cp /tmp/x .humanize/rlcr/now/goal-tracker.md", True),
    ],
)
def test_a_command_is_guarded_by_what_it_would_write(
    tmp_path: Path, command: str, guarded: bool
) -> None:
    """The plugin guards a bash write because a write through bash skips the tool that checks."""
    running = _loop(tmp_path)

    refused = guards.Guard(running, tmp_path)(_asked("Bash", command=command))

    assert (refused is not None) is guarded


@pytest.mark.parametrize(
    ("command", "everything"),
    [
        ("git add -A", True),
        ("git add --all", True),
        ("git add .", True),
        ("git add .humanize", True),
        ("git add ./.humanize/rlcr", True),
        ("git add -p", False),
        ("git add src/humanize/runner.py", False),
        ("git add ./src", False),
        # The plugin says as much: a commit message that says `.humanize` is not a stage.
        ('git commit -m "ignore ."', False),
    ],
)
def test_git_add_is_refused_only_when_it_reaches_for_everything(
    tmp_path: Path, command: str, everything: bool
) -> None:
    """`.humanize/` is the loop's own, and staging everything is how it ends up committed."""
    running = _loop(tmp_path)
    (tmp_path / ".humanize").mkdir(exist_ok=True)

    refused = guards.Guard(running, tmp_path)(_asked("Bash", command=command))

    assert (
        refused is not None and ".humanize Protection" in refused.because
    ) is everything


def test_the_work_itself_is_not_guarded(tmp_path: Path) -> None:
    """Everything that is not the loop's own state is what the builder is here to write."""
    running = _loop(tmp_path)
    guard = guards.Guard(running, tmp_path)

    assert guard(_asked("Write", file_path=str(tmp_path / "src" / "a.py"))) is None
    assert guard(_asked("Read", file_path=str(tmp_path / "README.md"))) is None
    assert guard(_asked("Bash", command="pytest -q")) is None


def _picks(said: str | None) -> tuple[HumanAgent, list[Question]]:
    """Somebody at the prompt who answers every question with the one thing."""
    human = HumanAgent()
    asked: list[Question] = []

    def answering(question: Question) -> str | None:
        asked.append(question)
        return said

    human.ask = answering
    return human, asked


def test_a_quiz_question_is_put_as_a_question_with_its_options() -> None:
    """The road a coding agent's own question takes, so whatever is driving shows it as one."""
    human, asked = _picks("B. the second one")

    picked = humanize1._asked(
        human, "Which is it?", ["the first one", "the second one"]
    )

    assert picked == "B"
    assert asked[0].text == "Which is it?"
    assert asked[0].options == ("A. the first one", "B. the second one")


def test_a_quiz_answered_by_the_letter_is_answered_all_the_same() -> None:
    """Which is how the quiz is written down, and so how somebody reading it would answer."""
    human, _ = _picks("b")

    assert humanize1._asked(human, "Which is it?", ["one", "two"]) == "B"


def test_nobody_at_the_prompt_is_a_quiz_that_is_not_put(tmp_path: Path) -> None:
    """A command line has nobody to quiz, and the quiz is advisory: the run carries on."""
    human = HumanAgent()  # nothing set `ask`, which is how a command line leaves it

    assert humanize1._asked(human, "Which is it?", ["one", "two"]) == ""
