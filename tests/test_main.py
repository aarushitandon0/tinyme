"""Tests for the Qt-free part of app.main: the one-step pipeline.

Only ``plan_next_step`` is covered. It takes its capture, read and planning
stages as arguments precisely so this runs with no screen, no OCR models and
no Ollama -- the Qt widgets and the pynput hotkey need a display and a real
desktop, so they are checked by hand (see the P4 notes in the handover).

What this protects: the hinge where the model's ``target_id`` becomes the bbox
we draw a circle around. MASTERSPEC 14 lists "circle in the wrong place" as a
top risk, and an off-by-one between numbering and lookup is how that happens.
"""
import numpy as np

from app.brain import BrainResult, CheckType, StepPlan, SuccessCheck
from app.capture import Capture
from app.elements import Element, Monitor, find_by_id, number_elements
from app.main import _read_screen, plan_next_step

MON = Monitor(left=0, top=0, width=1920, height=1080)


def fake_capture() -> Capture:
    return Capture(
        image=np.zeros((1080, 1920, 3), dtype=np.uint8),
        monitor=MON,
        elapsed_ms=18.0,
    )


def raw_elements() -> list[Element]:
    """Unnumbered, deliberately not in reading order."""
    return [
        Element(id=0, text="bill.pdf", source="ocr", role="text",
                region="center", bbox_px=(700, 400, 820, 424)),
        Element(id=0, text="Downloads", source="uia", role="TreeItem",
                region="left-middle", bbox_px=(60, 120, 180, 144)),
    ]


def plan(target_id: int, **overrides) -> StepPlan:
    body = dict(
        instruction="Click Downloads in the left panel",
        target_id=target_id,
        success_check=SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS,
                                   value="Downloads"),
    )
    body.update(overrides)
    return StepPlan(**body)


def planner_returning(step_plan: StepPlan, **result_kwargs):
    """A stand-in for brain.plan_step that records what it was given."""
    seen: dict = {}

    def planner(goal, elements, history=(), teach_notes=()):
        seen["goal"] = goal
        seen["elements"] = list(elements)
        seen["history"] = list(history)
        defaults = dict(elapsed_ms=1234.0, prompt_tokens=800, output_tokens=40)
        defaults.update(result_kwargs)
        return BrainResult(plan=step_plan, **defaults)

    planner.seen = seen
    return planner


def run(step_plan: StepPlan, elements=None, goal="find my bill", history=(),
        **result_kwargs):
    planner = planner_returning(step_plan, **result_kwargs)
    outcome = plan_next_step(
        goal,
        history=history,
        grab=fake_capture,
        read=lambda image, monitor, span=None: (
            raw_elements() if elements is None else elements, 95.0
        ),
        planner=planner,
    )
    return outcome, planner


class TestPlanNextStep:
    def test_resolves_the_target_to_the_element_the_model_meant(self):
        # "Downloads" sits higher on screen, so numbering puts it at id 1.
        outcome, _ = run(plan(target_id=1))
        assert outcome.target is not None
        assert outcome.target.text == "Downloads"
        assert outcome.target.bbox_px == (60, 120, 180, 144)

    def test_the_planner_sees_numbered_elements(self):
        _, planner = run(plan(target_id=1))
        assert [element.id for element in planner.seen["elements"]] == [1, 2]

    def test_passes_the_goal_and_history_through(self):
        _, planner = run(plan(target_id=1), goal="open my downloads",
                         history=["Click the folder icon"])
        assert planner.seen["goal"] == "open my downloads"
        assert planner.seen["history"] == ["Click the folder icon"]

    def test_no_target_when_the_model_cannot_see_it(self):
        outcome, _ = run(plan(target_id=0, cannot_see_it=True,
                              hint_if_missing="Scroll down a little"))
        assert outcome.target is None
        assert outcome.plan.hint_if_missing == "Scroll down a little"

    def test_no_target_when_the_goal_is_reached(self):
        outcome, _ = run(plan(target_id=0, goal_reached=True))
        assert outcome.target is None
        assert outcome.plan.goal_reached is True

    def test_an_id_that_vanished_from_the_list_draws_no_circle(self):
        """Belt and braces behind brain's validation: never circle a guess."""
        outcome, _ = run(plan(target_id=99, cannot_see_it=True))
        assert outcome.target is None

    def test_reports_stage_timings(self):
        outcome, _ = run(plan(target_id=1))
        assert outcome.capture_ms == 18.0
        assert outcome.read_ms == 95.0
        assert outcome.model_ms == 1234.0

    def test_counts_the_elements_the_model_was_shown(self):
        outcome, _ = run(plan(target_id=1))
        assert outcome.element_count == 2

    def test_survives_an_empty_screen(self):
        outcome, _ = run(plan(target_id=0, cannot_see_it=True), elements=[])
        assert outcome.target is None
        assert outcome.element_count == 0


class TestReadScreen:
    """The default read stage: OCR plus UIA, merged (MASTERSPEC 5.2).

    The sources are monkeypatched, so this runs with no screen and no COM. What
    it protects is the fallback CLAUDE.md asks for: a UIA failure must cost her
    the icon names, not the step.
    """

    def ocr_box(self):
        return Element(id=0, text="Mostly sunny", source="ocr", role="text",
                       region="bottom-left", bbox_px=(40, 1040, 190, 1064))

    def uia_box(self):
        return Element(id=0, text="File Explorer", source="uia", role="Button",
                       region="bottom-center", bbox_px=(1046, 1020, 1101, 1080))

    def test_merges_both_sources_and_sums_the_time(self, monkeypatch):
        monkeypatch.setattr("app.main.ocr_mod.read_screen",
                            lambda *_a: ([self.ocr_box()], 120.0))
        monkeypatch.setattr("app.main.uia_mod.collect",
                            lambda *_a: ([self.uia_box()], 230.0))
        elements, read_ms = _read_screen(np.zeros((4, 4, 3), dtype=np.uint8), MON)
        assert {element.text for element in elements} == {"Mostly sunny", "File Explorer"}
        assert read_ms == 350.0

    def test_keeps_ocr_elements_when_uia_finds_nothing(self, monkeypatch):
        monkeypatch.setattr("app.main.ocr_mod.read_screen",
                            lambda *_a: ([self.ocr_box()], 120.0))
        monkeypatch.setattr("app.main.uia_mod.collect", lambda *_a: ([], 300.0))
        elements, _ = _read_screen(np.zeros((4, 4, 3), dtype=np.uint8), MON)
        assert [element.text for element in elements] == ["Mostly sunny"]

    def test_the_same_thing_in_both_sources_is_one_element(self, monkeypatch):
        twin = Element(id=0, text="Downloads", source="uia", role="ListItem",
                       region="left-middle", bbox_px=(60, 120, 180, 144))
        ocr_twin = Element(id=0, text="Downloads", source="ocr", role="text",
                           region="left-middle", bbox_px=(62, 122, 178, 142))
        monkeypatch.setattr("app.main.ocr_mod.read_screen",
                            lambda *_a: ([ocr_twin], 120.0))
        monkeypatch.setattr("app.main.uia_mod.collect", lambda *_a: ([twin], 90.0))
        elements, _ = _read_screen(np.zeros((4, 4, 3), dtype=np.uint8), MON)
        assert [(e.text, e.source) for e in elements] == [("Downloads", "uia")]


# --- the multi-step task loop (MASTERSPEC 5.5) ------------------------------
# TaskLoop holds no Qt: TinyMe drives it from a QTimer. So the step sequencing,
# the history the brain receives, the recovery counting and the stop conditions
# are all testable with no desktop, which is where the P6 acceptance criterion
# ("3 steps, and a wrong click recovers") actually lives.

from app.brain import CheckType, SuccessCheck  # noqa: E402
from app.main import TaskLoop, LoopPhase  # noqa: E402
from app import watcher  # noqa: E402


def outcome_for(instruction, target_id=1, goal_reached=False, cannot_see_it=False,
                check=None, elements=None):
    step_plan = StepPlan(
        instruction=instruction,
        target_id=target_id,
        success_check=check or SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS,
                                           value="Downloads"),
        goal_reached=goal_reached,
        cannot_see_it=cannot_see_it,
        hint_if_missing="Scroll down a little" if cannot_see_it else "",
    )
    numbered = elements if elements is not None else number_elements(raw_elements())
    from app.main import StepOutcome
    return StepOutcome(
        plan=step_plan,
        target=find_by_id(numbered, target_id),
        elements=numbered,
        capture_ms=10.0,
        read_ms=20.0,
        model_ms=30.0,
    )


def queued_planner(*outcomes):
    """A planner that hands back prepared outcomes and records the history it
    was given, which is how we check the brain really sees the step history."""
    calls = []
    remaining = list(outcomes)

    def planner(goal, history=(), teach_notes=()):
        calls.append({"goal": goal, "history": list(history)})
        return remaining.pop(0) if remaining else outcomes[-1]

    planner.calls = calls
    return planner


def loop_with(*outcomes, **kwargs):
    planner = queued_planner(*outcomes)
    loop = TaskLoop("find my bill", planner=planner, **kwargs)
    return loop, planner


class TestTaskLoop:
    def test_starts_by_planning_the_first_step_with_no_history(self):
        loop, planner = loop_with(outcome_for("Open this"))
        step = loop.start()
        assert step.plan.instruction == "Open this"
        assert planner.calls[0]["history"] == []
        assert loop.phase is LoopPhase.WATCHING

    def test_a_passed_check_moves_to_the_next_step(self):
        loop, planner = loop_with(outcome_for("Open this"), outcome_for("Click Downloads"))
        loop.start()
        step = loop.advance(watcher.Outcome.PASSED)
        assert step.plan.instruction == "Click Downloads"
        assert loop.step_number == 2

    def test_the_brain_receives_what_she_has_already_done(self):
        loop, planner = loop_with(
            outcome_for("Open this"),
            outcome_for("Click Downloads"),
            outcome_for("That is your file"),
        )
        loop.start()
        loop.advance(watcher.Outcome.PASSED)
        loop.advance(watcher.Outcome.PASSED)
        assert planner.calls[2]["history"] == ["Open this", "Click Downloads"]

    def test_i_did_it_advances_the_same_way(self):
        loop, planner = loop_with(outcome_for("Open this"), outcome_for("Click Downloads"))
        loop.start()
        step = loop.advance(watcher.Outcome.MANUAL)
        assert step.plan.instruction == "Click Downloads"
        assert planner.calls[1]["history"] == ["Open this"]

    def test_goal_reached_finishes_the_task(self):
        loop, _ = loop_with(outcome_for("You are there", goal_reached=True))
        loop.start()
        assert loop.phase is LoopPhase.DONE
        assert loop.finished_because == "goal_reached"

    def test_a_replan_does_not_count_as_a_step_she_completed(self):
        """MASTERSPEC 5.5: re-plan from the new screen, and count a recovery.
        The instruction she never completed must not enter the history, or the
        brain is told she did something she did not."""
        loop, planner = loop_with(outcome_for("Open this"), outcome_for("Open this instead"))
        loop.start()
        step = loop.advance(watcher.Outcome.REPLAN)
        assert step.plan.instruction == "Open this instead"
        assert planner.calls[1]["history"] == []
        assert loop.recoveries == 1
        assert loop.step_number == 1

    def test_stops_after_the_step_limit(self):
        """MASTERSPEC 5.5 allows max_steps steps, so the limit bites on the
        advance that would plan one more -- not on the last allowed step."""
        loop, planner = loop_with(outcome_for("Keep going"), max_steps=3)
        loop.start()
        loop.advance(watcher.Outcome.PASSED)
        loop.advance(watcher.Outcome.PASSED)
        assert loop.phase is LoopPhase.WATCHING
        assert len(planner.calls) == 3
        assert loop.advance(watcher.Outcome.PASSED) is None
        assert loop.phase is LoopPhase.DONE
        assert loop.finished_because == "max_steps"

    def test_stop_ends_the_loop_and_later_polls_do_nothing(self):
        loop, planner = loop_with(outcome_for("Open this"), outcome_for("Next"))
        loop.start()
        loop.stop()
        assert loop.phase is LoopPhase.DONE
        assert loop.finished_because == "stopped"
        assert loop.advance(watcher.Outcome.PASSED) is None
        assert len(planner.calls) == 1

    def test_a_cannot_see_it_step_keeps_watching_rather_than_giving_up(self):
        """She may scroll and bring the thing into view; that is a screen change
        the watcher will notice."""
        loop, _ = loop_with(outcome_for("", target_id=0, cannot_see_it=True))
        step = loop.start()
        assert step.target is None
        assert loop.phase is LoopPhase.WATCHING

    def test_every_step_gets_a_watch_for_its_own_check(self):
        check = SuccessCheck(type=CheckType.WINDOW_CLASS_IS, value="CabinetWClass")
        loop, _ = loop_with(outcome_for("Open this", check=check))
        step = loop.start()
        assert step.watch.check.value == "CabinetWClass"
        assert step.watch.target_bbox == step.target.bbox_px


# --- the guard in the one path to the model (CLAUDE.md rule 3) --------------

from app import guard  # noqa: E402


def sensitive_elements():
    return [
        Element(id=0, text="Username", source="ocr", role="text",
                region="center", bbox_px=(700, 380, 820, 404)),
        Element(id=0, text="Password", source="ocr", role="text",
                region="center", bbox_px=(700, 420, 820, 444)),
    ]


class TestTheGuardGatesTheModel:
    """P7's acceptance check: a password screen pauses *before* any model call.

    These go through ``plan_next_step`` rather than calling ``guard`` directly,
    because the thing worth protecting is not the keyword list - it is that
    there is no route to the model that skips it.
    """

    def run_on(self, elements, window_title=""):
        called = []

        def planner(*args, **kwargs):
            called.append(True)
            return BrainResult(plan=plan(target_id=1), elapsed_ms=1000.0)

        outcome = plan_next_step(
            "log in to my bank",
            grab=fake_capture,
            read=lambda image, monitor, span=None: (elements, 95.0),
            planner=planner,
            window_reader=lambda: watcher.WindowInfo(title=window_title),
        )
        return outcome, called

    def test_a_password_screen_never_reaches_the_model(self):
        outcome, called = self.run_on(sensitive_elements())
        assert called == []
        assert outcome.paused is True
        assert outcome.model_ms == 0.0

    def test_the_paused_step_circles_nothing_and_hands_over(self):
        outcome, _ = self.run_on(sensitive_elements())
        assert outcome.target is None
        assert outcome.plan.instruction == guard.HANDOVER_MESSAGE

    def test_the_reason_names_keywords_and_not_her_screen(self):
        """CLAUDE.md rule 7: what we log and trace is our own enums, never her
        screen text."""
        outcome, _ = self.run_on(sensitive_elements())
        assert "password" in outcome.paused_reason
        assert "Username" not in outcome.paused_reason

    def test_the_window_title_alone_is_enough_to_hold_it_back(self):
        outcome, called = self.run_on(raw_elements(), window_title="Enter OTP")
        assert called == []
        assert outcome.paused is True

    def test_an_ordinary_screen_goes_through_to_the_model(self):
        outcome, called = self.run_on(raw_elements(), window_title="Downloads")
        assert called == [True]
        assert outcome.paused is False
        assert outcome.paused_reason == ""

    def test_the_elements_are_still_reported_when_paused(self):
        """We read the screen before the guard ran, so the count is real. It is
        a number, which is all the Sentry span may carry anyway."""
        outcome, _ = self.run_on(sensitive_elements())
        assert outcome.element_count == 2


class TestWhatTheWatchIsGiven:
    """The watcher can only notice an unexpected change if the planning step
    hands it something to compare against (MASTERSPEC 5.5)."""

    def run(self, target_id=1):
        return plan_next_step(
            "find my bill",
            grab=fake_capture,
            read=lambda image, monitor, span=None: (raw_elements(), 95.0),
            planner=planner_returning(plan(target_id=target_id)),
            window_reader=lambda: watcher.WindowInfo(
                class_name="CabinetWClass", title="Downloads", hwnd=77),
        )

    def test_records_the_window_that_was_in_front(self):
        assert self.run().window.hwnd == 77

    def test_keeps_a_padded_crop_of_what_it_circled(self):
        from app import config as cfg

        outcome = self.run()
        assert outcome.baseline is not None
        # "Downloads" is 24 px tall and 120 wide in raw_elements, plus padding.
        pad = cfg.REGION_PAD_PX
        assert outcome.baseline.shape == (24 + 2 * pad, 120 + 2 * pad, 3)

    def test_no_crop_when_there_is_nothing_circled(self):
        outcome = plan_next_step(
            "find my bill",
            grab=fake_capture,
            read=lambda image, monitor, span=None: (raw_elements(), 95.0),
            planner=planner_returning(plan(target_id=0, cannot_see_it=True)),
            window_reader=lambda: watcher.WindowInfo(),
        )
        assert outcome.baseline is None

    def test_records_the_monitor_origin_for_the_crop(self):
        assert self.run().monitor_offset == (0, 0)


# --- the loop while the guard holds it back ---------------------------------


def paused_outcome(reason="strong:password"):
    from app.main import StepOutcome
    return StepOutcome(
        plan=guard.handover_plan(reason),
        target=None,
        elements=number_elements(raw_elements()),
        capture_ms=10.0,
        read_ms=20.0,
        model_ms=0.0,
        paused_reason=reason,
    )


class TestTheLoopWhenPaused:
    def test_a_paused_step_puts_the_loop_in_the_paused_phase(self):
        loop, _ = loop_with(paused_outcome())
        step = loop.start()
        assert loop.phase is LoopPhase.PAUSED
        assert step.paused is True
        assert loop.pauses == 1

    def test_the_task_is_not_over_it_is_waiting(self):
        loop, _ = loop_with(paused_outcome())
        loop.start()
        assert loop.finished_because == ""

    def test_polls_do_nothing_while_paused(self):
        """There is no step of hers to pass or fail, so a stray poll must not
        advance the task past a screen we never looked at."""
        loop, planner = loop_with(paused_outcome(), outcome_for("Click Downloads"))
        loop.start()
        assert loop.advance(watcher.Outcome.PASSED).paused is True
        assert loop.step_number == 1
        assert len(planner.calls) == 1

    def test_retry_re_reads_and_resumes_once_the_screen_is_ordinary(self):
        loop, planner = loop_with(paused_outcome(), outcome_for("Click Downloads"))
        loop.start()
        step = loop.retry()
        assert loop.phase is LoopPhase.WATCHING
        assert step.plan.instruction == "Click Downloads"
        assert loop.step_number == 1  # She completed nothing while paused.

    def test_retry_stays_paused_while_the_screen_is_still_private(self):
        loop, _ = loop_with(paused_outcome(), paused_outcome())
        loop.start()
        loop.retry()
        assert loop.phase is LoopPhase.PAUSED

    def test_a_run_of_paused_polls_counts_as_one_pause(self):
        """The number is for the Sentry span and the eval: how often a task hit
        a private screen, not how many times we looked at the same one."""
        loop, _ = loop_with(paused_outcome(), paused_outcome(), paused_outcome())
        loop.start()
        loop.retry()
        loop.retry()
        assert loop.pauses == 1

    def test_history_is_untouched_by_a_pause(self):
        loop, planner = loop_with(paused_outcome(), outcome_for("Click Downloads"))
        loop.start()
        loop.retry()
        assert planner.calls[1]["history"] == []

    def test_stop_works_while_paused(self):
        loop, _ = loop_with(paused_outcome())
        loop.start()
        loop.stop()
        assert loop.phase is LoopPhase.DONE
        assert loop.retry() is None


class TestSceneA:
    """PROJECTPLAN's Saturday acceptance check, as a test: three steps run
    unaided, and a deliberate wrong click recovers (MASTERSPEC 3A, 5.5).

    This is the whole P6 criterion expressed without a desktop: TaskLoop is
    Qt-free precisely so the sequencing can be checked here instead of by
    clicking through it once and hoping.
    """

    def steps(self):
        return (
            outcome_for("Open this", check=SuccessCheck(
                type=CheckType.WINDOW_CLASS_IS, value="CabinetWClass")),
            outcome_for("Click Downloads", check=SuccessCheck(
                type=CheckType.WINDOW_TITLE_CONTAINS, value="Downloads")),
            outcome_for("That is your file", check=SuccessCheck(
                type=CheckType.TEXT_APPEARS, value="bill.pdf")),
            outcome_for("You are there", goal_reached=True),
        )

    def test_three_steps_then_the_goal(self):
        loop, planner = loop_with(*self.steps())
        loop.start()
        for _ in range(3):
            loop.advance(watcher.Outcome.PASSED)
        assert loop.phase is LoopPhase.DONE
        assert loop.finished_because == "goal_reached"
        assert loop.recoveries == 0
        assert planner.calls[-1]["history"] == [
            "Open this", "Click Downloads", "That is your file",
        ]

    def test_a_wrong_click_re_plans_instead_of_repeating_the_old_step(self):
        """She opened Settings instead of Explorer. The old circle describes a
        screen that is no longer there, so the step is re-planned from the new
        one and nothing enters the history."""
        wrong_turn = outcome_for("Close this and try again")
        loop, planner = loop_with(self.steps()[0], wrong_turn,
                                 self.steps()[1], self.steps()[3])
        first = loop.start()

        # The watcher sees a different window in front and no passing check.
        poll = first.watch.poll(watcher.Screen(
            window=watcher.WindowInfo(class_name="ApplicationFrameWindow",
                                      title="Settings", hwnd=999)))
        assert poll.outcome is watcher.Outcome.REPLAN

        recovered = loop.advance(poll.outcome)
        assert recovered.plan.instruction == "Close this and try again"
        assert loop.recoveries == 1
        assert loop.step_number == 1
        assert planner.calls[1]["history"] == []

        # And from there the task carries on normally.
        loop.advance(watcher.Outcome.PASSED)
        assert loop.step_number == 2
        assert planner.calls[2]["history"] == ["Close this and try again"]

    def test_the_first_step_passes_on_the_window_class_not_the_title(self):
        """CLAUDE.md rule 11, end to end: Explorer's title is the folder name,
        so the taskbar step can only be confirmed by class."""
        loop, _ = loop_with(*self.steps())
        step = loop.start()
        passed = step.watch.poll(watcher.Screen(
            window=watcher.WindowInfo(class_name="CabinetWClass",
                                      title="Home", hwnd=5)))
        assert passed.outcome is watcher.Outcome.PASSED
