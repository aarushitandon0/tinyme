"""The app: hotkey, prompt window, one guided step (MASTERSPEC 5.1, 5.7).

P4 scope, deliberately: Ctrl+Alt+H opens a prompt box, she types a goal, and
Tiny Me circles the one thing to click. There is no waiting, no success check
and no second step yet -- that is ``watcher.py`` in P6. ``guard.py`` is P7, so
this build must not be pointed at a screen with anything private on it.

Three CLAUDE.md rules decide the shape of this file:

* Rule 8 -- Qt widgets live on the main thread only. ``pynput`` runs its own
  thread and may never touch a widget, so the hotkey goes through
  :class:`HotkeyBridge`, whose only job is to turn a callback on a foreign
  thread into a Qt signal. Capture, OCR and the model call run in a
  :class:`QThread` worker and come back by signal too.
* Rule 9 -- physical pixels stay physical until ``elements.to_logical``, which
  only ``overlay.py`` calls.
* Rule 6 -- screenshots stay in memory.

:func:`plan_next_step` is kept free of Qt and takes its stages as arguments, so
the part that decides *what gets circled* is unit-tested without a screen.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence

import numpy as np

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app import actions as actions_mod
from app import brain as brain_mod
from app import capture as capture_mod
from app import config
from app import guard as guard_mod
from app import ocr as ocr_mod
from app import uia as uia_mod
from app import watcher as watcher_mod
from app.brain import BrainResult, StepPlan, plan_step
from app.capture import Capture
from app.elements import (
    Element,
    Monitor,
    cap_elements,
    find_by_id,
    merge_elements,
    number_elements,
)
from app.overlay import CaptureMode, Overlay
from app.watcher import Outcome as WatchOutcome
from app.watcher import Screen, StepWatch, WindowInfo

log = logging.getLogger(__name__)


# --- one step, without Qt --------------------------------------------------


@dataclass(frozen=True)
class StepOutcome:
    """What one pass of the pipeline produced.

    ``target`` is the element to circle, or None when there is nothing to
    circle (goal reached, or the model could not find it). The timings and
    counts are the primitives MASTERSPEC 8 wants on a Sentry span.
    """

    plan: StepPlan
    target: Element | None
    elements: list[Element]
    capture_ms: float
    read_ms: float
    model_ms: float
    #: The foreground window at the moment this step was planned. The watcher
    #: compares against it to notice that the screen moved on under us.
    window: WindowInfo = WindowInfo()
    #: The pixels under the circle when the step was planned, padded. Kept so
    #: ``region_changed`` and the re-plan trigger have something to diff
    #: against; a small crop, not the screenshot (CLAUDE.md rule 6).
    baseline: np.ndarray | None = None
    #: Primary-monitor origin, needed to turn a screen bbox into image indices.
    monitor_offset: tuple[int, int] = (0, 0)
    #: Non-empty when ``guard.py`` held this screen back: which keywords fired.
    #: No model call happened and nothing was circled (CLAUDE.md rule 3).
    paused_reason: str = ""

    @property
    def element_count(self) -> int:
        return len(self.elements)

    @property
    def total_ms(self) -> float:
        return self.capture_ms + self.read_ms + self.model_ms

    @property
    def paused(self) -> bool:
        return bool(self.paused_reason)


#: Signatures of the injectable stages, so the test doubles stay honest.
GrabStage = Callable[[], Capture]
ReadStage = Callable[..., tuple[list[Element], float]]
PlanStage = Callable[..., BrainResult]
WindowStage = Callable[[], WindowInfo]
GuardStage = Callable[..., guard_mod.Verdict]


def _read_screen(image: np.ndarray, monitor: Monitor) -> tuple[list[Element], float]:
    """Default read stage: OCR plus UI Automation, merged (MASTERSPEC 5.2).

    OCR runs first and always; UIA is the part that can time out or throw, and
    when it does we carry on with OCR only. ``uia_ms`` and ``ocr_ms`` are logged
    separately because MASTERSPEC 8 wants both on the read_screen span, and
    because knowing which of the two costs the most is the whole point of the
    eval.

    Returns:
        (merged elements, read_ms) where read_ms covers both sources.
    """
    ocr_elements, ocr_ms = ocr_mod.read_screen(image, monitor)
    uia_elements, uia_ms = uia_mod.collect(monitor)
    merged = merge_elements(ocr_elements, uia_elements)
    log.info(
        "read screen: ocr %d in %.0f ms, uia %d in %.0f ms, merged %d",
        len(ocr_elements), ocr_ms, len(uia_elements), uia_ms, len(merged),
    )
    return merged, ocr_ms + uia_ms


def plan_next_step(
    goal: str,
    history: Sequence[str] = (),
    teach_notes: Sequence[str] = (),
    grab: GrabStage = capture_mod.grab_primary,
    read: ReadStage = _read_screen,
    planner: PlanStage = plan_step,
    window_reader: WindowStage = watcher_mod.read_window,
    sensitive: GuardStage = guard_mod.is_sensitive,
) -> StepOutcome:
    """Capture, read, **guard**, ask the model, resolve the target.

    The order matters twice over:

    * Elements are capped *before* numbering, so the ids the model sees are
      exactly the ids we look up afterwards. Numbering first and capping second
      would hand the model ids with holes in them.
    * The guard runs *after* reading and *before* ``planner`` -- this is the
      single path to the model, which is what makes CLAUDE.md rule 3 ("nothing
      is sent to the model if the screen is sensitive") true by construction
      rather than by remembering. On a held-back screen this returns with
      ``model_ms`` at zero and ``paused_reason`` set.

    The resolved target is looked up rather than trusted: ``brain`` already
    validates that the id was in the list, and this returns None rather than a
    guess if it somehow was not. Nothing is worse than a confident circle
    around the wrong thing.
    """
    shot = grab()
    found, read_ms = read(shot.image, shot.monitor)

    elements = number_elements(
        cap_elements(
            found,
            taskbar_rect=capture_mod.taskbar_rect(),
            foreground_rect=capture_mod.foreground_rect(),
        )
    )

    window = window_reader()
    offset = (shot.monitor.left, shot.monitor.top)

    verdict = sensitive(elements, window.title)
    if verdict.sensitive:
        log.info("guard held this screen back; no model call (%s)", verdict.reason)
        return StepOutcome(
            plan=guard_mod.handover_plan(verdict.reason),
            target=None,
            elements=elements,
            capture_ms=shot.elapsed_ms,
            read_ms=read_ms,
            model_ms=0.0,
            window=window,
            baseline=None,
            monitor_offset=offset,
            paused_reason=verdict.reason,
        )

    result = planner(goal, elements, history=history, teach_notes=teach_notes)
    target = find_by_id(elements, result.plan.target_id)

    log.info(
        "step planned: %d elements, capture %.0f ms, read %.0f ms, model %.0f ms, "
        "retried=%s fell_back=%s target=%s",
        len(elements), shot.elapsed_ms, read_ms, result.elapsed_ms,
        result.retried, result.fell_back, result.plan.target_id,
    )
    return StepOutcome(
        plan=result.plan,
        target=target,
        elements=elements,
        capture_ms=shot.elapsed_ms,
        read_ms=read_ms,
        model_ms=result.elapsed_ms,
        window=window,
        baseline=watcher_mod.crop(
            shot.image, target.bbox_px if target else None, offset
        ),
        monitor_offset=offset,
    )


# --- the multi-step loop ---------------------------------------------------


class LoopPhase(Enum):
    """Where a task is: watching a step, held back by the guard, or over."""

    WATCHING = "watching"
    #: MASTERSPEC 6: a private screen. No model call happened and none will
    #: until she moves on; the loop re-reads the screen and asks again.
    PAUSED = "paused"
    DONE = "done"


@dataclass(frozen=True)
class Step:
    """One planned step and the watch that decides whether it worked."""

    index: int
    outcome: StepOutcome
    watch: StepWatch

    @property
    def plan(self) -> StepPlan:
        return self.outcome.plan

    @property
    def target(self) -> Element | None:
        return self.outcome.target

    @property
    def paused(self) -> bool:
        return self.outcome.paused

    @property
    def message(self) -> str:
        """What to put in front of her for this step."""
        if self.plan.goal_reached:
            return self.plan.instruction or "That's it - you're there."
        if self.target is None:
            return self.plan.hint_if_missing or self.plan.instruction
        return self.plan.instruction


class TaskLoop:
    """Plan, watch, advance, until the goal or a stop condition (MASTERSPEC 5.5).

    Deliberately free of Qt: ``TinyMe`` owns the QTimer and calls ``start`` and
    ``advance``. That keeps the sequencing rules - what the brain is told she has
    done, what counts as a recovery, when to stop - testable without a desktop,
    and keeps every widget on the main thread (CLAUDE.md rule 8).

    The history is the instructions she actually *completed*. A re-planned step
    never enters it: telling the brain she did something she did not is how a
    guide walks her in circles.
    """

    def __init__(
        self,
        goal: str,
        planner: Callable[..., StepOutcome] = plan_next_step,
        teach_notes: Sequence[str] = (),
        max_steps: int = config.MAX_STEPS,
        watch_factory: Callable[..., StepWatch] = StepWatch,
    ) -> None:
        self.goal = goal
        self._planner = planner
        self._teach_notes = tuple(teach_notes)
        self._max_steps = max_steps
        self._watch_factory = watch_factory

        self.history: list[str] = []
        self.phase = LoopPhase.WATCHING
        self.finished_because = ""
        self.recoveries = 0
        #: How many times the guard held a screen back during this task. A
        #: number, for the Sentry span and the eval -- never a reason string.
        self.pauses = 0
        self.step_number = 0
        self.step: Step | None = None

    # --- driving ----------------------------------------------------------

    def start(self) -> Step | None:
        """Plan the first step."""
        self.step_number = 1
        return self._plan()

    def advance(self, outcome: WatchOutcome) -> Step | None:
        """React to one poll result and plan whatever comes next.

        Returns the new step, or None when the task is over.
        """
        if self.phase is LoopPhase.DONE:
            return None

        if self.phase is LoopPhase.PAUSED:
            # Checked before anything else: there is no step of hers to pass or
            # fail while the guard is holding the screen back, so even an "I did
            # it" must not walk the task past a screen we never looked at.
            # :meth:`retry` is the only way forward.
            return self.step

        if outcome in (WatchOutcome.PASSED, WatchOutcome.MANUAL):
            if self.step is not None and not self.step.plan.cannot_see_it:
                self.history.append(self.step.plan.instruction)
            self.step_number += 1
            if self.step_number > self._max_steps:
                return self._finish("max_steps")
            return self._plan()

        if outcome is WatchOutcome.REPLAN:
            # Same step number: she has not got anywhere, the screen just moved.
            self.recoveries += 1
            log.info("re-planning from a fresh screen (recovery %d)", self.recoveries)
            return self._plan()

        return self.step  # PENDING: nothing to do.

    def retry(self) -> Step | None:
        """Re-read the screen while the guard has us paused (MASTERSPEC 6).

        Same step number: she has not completed anything, she is typing her own
        password. Each call re-runs capture, read and guard, so the moment the
        screen is ordinary again this plans a real step and the loop resumes.
        """
        if self.phase is LoopPhase.DONE:
            return None
        return self._plan()

    def stop(self) -> None:
        """Ctrl+Alt+P, or the window closing."""
        if self.phase is not LoopPhase.DONE:
            self._finish("stopped")

    # --- internals --------------------------------------------------------

    def _finish(self, because: str) -> None:
        self.phase = LoopPhase.DONE
        self.finished_because = because
        log.info("task over after %d steps (%s), %d recoveries",
                 self.step_number, because, self.recoveries)
        return None

    def _plan(self) -> Step | None:
        outcome = self._planner(self.goal, history=tuple(self.history),
                               teach_notes=self._teach_notes)
        self.step = Step(
            index=self.step_number,
            outcome=outcome,
            watch=self._watch_factory(
                outcome.plan.success_check,
                target_bbox=outcome.target.bbox_px if outcome.target else None,
                baseline=outcome.baseline,
                start_window=outcome.window,
            ),
        )
        if outcome.paused:
            # Not an error and not progress: hold here, keep the goal, and let
            # retry() ask again. The phase is recomputed on every plan, so the
            # loop comes out of PAUSED by itself once the screen is ordinary.
            self.pauses += 1 if self.phase is not LoopPhase.PAUSED else 0
            self.phase = LoopPhase.PAUSED
        else:
            self.phase = LoopPhase.WATCHING
            if outcome.plan.goal_reached:
                self._finish("goal_reached")
        return self.step


# --- threading -------------------------------------------------------------


class Job(QObject):
    """Runs one blocking callable on a worker thread (CLAUDE.md rule 8).

    Everything slow in this app is the same shape: capture, OCR and a model
    call, seconds of work on her laptop (notes/bench.md), which would freeze the
    UI if it ran on the main thread. So there is one worker type and the
    callable decides what the work is. Nothing here touches a widget; results
    leave by signal.
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, work: Callable[[], object]) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:
        try:
            result = self._work()
        except Exception as exc:
            log.exception("a background job failed")
            self.failed.emit(str(exc))
        else:
            self.done.emit(result)


class HotkeyBridge(QObject):
    """Turns pynput's foreign-thread callbacks into Qt signals.

    This class is the whole of CLAUDE.md rule 8 at the hotkey boundary: the
    listener thread may only emit, and every widget touch happens in a slot on
    the main thread.
    """

    opened = Signal()
    paused = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._listener = None

    def start(self) -> bool:
        """Begin listening. False if pynput could not grab the hotkeys."""
        try:
            from pynput import keyboard

            self._listener = keyboard.GlobalHotKeys({
                config.HOTKEY_OPEN: self.opened.emit,
                config.HOTKEY_PAUSE: self.paused.emit,
            })
            self._listener.start()
            log.info("hotkeys live: %s open, %s pause",
                     config.HOTKEY_OPEN, config.HOTKEY_PAUSE)
            return True
        except Exception:
            log.exception("could not register global hotkeys")
            return False

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


# --- the window ------------------------------------------------------------


class PromptWindow(QWidget):
    """Her way in: a text box and three buttons.

    A separate, normal, clickable window -- MASTERSPEC 5.6 is explicit that the
    overlay is click-through and this is not.

    "I did it" is always visible during a task (MASTERSPEC 5.5). It is the
    escape hatch for every way a success check can be wrong: she clicked the
    right thing, the check disagreed, and she should not have to argue with a
    circle. "Stop" is the same thing as Ctrl+Alt+P for someone who would rather
    press a button than remember a chord.
    """

    submitted = Signal(str)
    #: "Find it for me": the one thing Tiny Me does *instead* of guiding
    #: (MASTERSPEC 3A, 4). A separate signal rather than a mode flag, so the
    #: guide path cannot accidentally start doing things for her.
    find_requested = Signal(str)
    #: "Do it for me": the booking flow (MASTERSPEC 3B). Its own signal for the
    #: same reason as ``find_requested`` -- what Tiny Me is allowed to *do* is
    #: decided by which button she pressed plus ``guard.can_do_it``, never by
    #: the model.
    do_requested = Signal(str)
    did_it = Signal()
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Tiny Me")

        self._status = QLabel("What would you like to do?")
        self._status.setWordWrap(True)
        self._box = QLineEdit()
        self._box.setPlaceholderText("I can't find the file I downloaded")
        self._box.returnPressed.connect(self._submit)

        self._go = QPushButton("Show me how")
        self._go.clicked.connect(self._submit)
        self._find = QPushButton("Find it for me")
        self._find.clicked.connect(self._find_it)
        self._do = QPushButton("Do it for me")
        self._do.clicked.connect(self._do_it)
        self._done = QPushButton("I did it")
        self._done.clicked.connect(self.did_it.emit)
        self._stop = QPushButton("Stop")
        self._stop.clicked.connect(self.stopped.emit)

        buttons = QHBoxLayout()
        buttons.addWidget(self._stop)
        buttons.addStretch(1)
        buttons.addWidget(self._done)
        buttons.addWidget(self._find)
        buttons.addWidget(self._do)
        buttons.addWidget(self._go)

        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self._box)
        layout.addLayout(buttons)
        self.resize(460, 160)
        self.set_task_active(False)

    def _submit(self) -> None:
        goal = self._box.text().strip()
        if goal:
            self.submitted.emit(goal)

    def _find_it(self) -> None:
        """"Find it for me" works with an empty box: that means "the newest one"."""
        self.find_requested.emit(self._box.text().strip())

    def _do_it(self) -> None:
        goal = self._box.text().strip()
        if goal:
            self.do_requested.emit(goal)

    def ask(self) -> None:
        """Show, focus and raise. Called on the main thread only."""
        self._box.selectAll()
        self.show()
        self.raise_()
        self.activateWindow()
        self._box.setFocus()

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_busy(self, busy: bool) -> None:
        self._go.setEnabled(not busy)
        self._find.setEnabled(not busy)
        self._do.setEnabled(not busy)
        self._box.setEnabled(not busy)

    def set_task_active(self, active: bool) -> None:
        """Only offer "I did it" and "Stop" while there is a task to do it to."""
        self._done.setEnabled(active)
        self._stop.setEnabled(active)


class TinyMe(QObject):
    """The task loop as Qt sees it: a timer, a worker, an overlay.

    The division of labour, which is what keeps this testable and keeps rule 8:

    * :class:`TaskLoop` decides *what happens next* and holds no Qt.
    * :class:`StepWatch` decides *whether a step worked* and holds no Qt.
    * This class owns the clock, the widgets and the thread, and nothing else.

    One job runs at a time. The timer ticks at ~1 Hz (MASTERSPEC 5.5) and a tick
    that lands while a job is still running is simply dropped -- on a slow laptop
    an OCR poll can outlast its own interval, and queueing them up would turn one
    slow step into a backlog of captures.
    """

    #: Emitted from the booking worker when a field is hers to fill. Carries
    #: a physical-pixel bbox (or None) and the words to show. A signal rather
    #: than a direct call because the emitter is on a worker thread and the
    #: overlay is a widget (CLAUDE.md rule 8).
    handover = Signal(object, str)
    #: Emitted from the booking worker once she has moved on.
    handover_over = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.overlay = Overlay()
        self.window = PromptWindow()
        self.window.submitted.connect(self.start_task)
        self.window.find_requested.connect(self.find_for_her)
        self.window.do_requested.connect(self.do_for_her)
        self.window.did_it.connect(self.mark_done)
        self.window.stopped.connect(self.stop_everything)

        self.hotkeys = HotkeyBridge()
        self.hotkeys.opened.connect(self.window.ask)
        self.hotkeys.paused.connect(self.stop_everything)

        self.handover.connect(self._circle_handover)
        self.handover_over.connect(self._handover_done)

        self.loop: TaskLoop | None = None
        #: Read by the booking flow on its own thread between every action, so
        #: Ctrl+Alt+P ends it promptly instead of at the end of the page.
        self._stopping = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self._thread: QThread | None = None
        self._job: Job | None = None
        self._hid_overlay = False

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # Settle which capture path keeps the overlay out of our screenshots
        # before the first real step, so OCR never reads our own label back.
        mode = self.overlay.verify_capture_exclusion()
        log.info("capture mode: %s", mode.value)
        # COM's first call costs ~450 ms. Pay it now, not inside the 300 ms
        # budget of her first step.
        uia_mod.warm_up()
        if not self.hotkeys.start():
            self.window.set_status(
                "Hotkeys are unavailable, so this window stays open."
            )
        self.window.ask()

    def stop_everything(self) -> None:
        """Ctrl+Alt+P, or the Stop button. Everything goes quiet (MASTERSPEC 6).

        The timer stops first: no further capture, which is the promise the
        indicator makes. A job already in flight is left to finish into a loop
        that is already DONE, where its result is ignored.
        """
        log.info("pause requested")
        self._stopping = True
        self._timer.stop()
        if self.loop is not None:
            self.loop.stop()
        self.overlay.set_looking(False)
        self.overlay.clear()
        self.window.set_task_active(False)
        self.window.set_status("Stopped. Press Ctrl+Alt+H when you want me again.")

    # --- running a task -----------------------------------------------------

    @property
    def _busy(self) -> bool:
        return self._thread is not None

    def start_task(self, goal: str) -> None:
        """She typed a goal and pressed "Show me how"."""
        if self._busy:
            return  # A job is already running; ignore a double press.

        self._timer.stop()
        self._stopping = False
        self.overlay.clear()
        # Reading the notes file is a few lines off a warm disk, so it happens
        # here rather than earning its own worker hop; the goal has to be known
        # to choose which notes are relevant, and the loop needs them before
        # its first plan.
        notes = brain_mod.notes_for_goal(goal)
        if notes:
            log.info("including %d teach note(s) with this goal", len(notes))
        self.loop = TaskLoop(goal, planner=self._plan_stage, teach_notes=notes)
        self.window.set_task_active(True)
        self.window.set_status("Looking at your screen...")
        self.window.set_busy(True)
        self._run(self.loop.start, self._step_ready)

    def find_for_her(self, goal: str) -> None:
        """"Find it for me": open Explorer on her file (MASTERSPEC 3A, P8).

        Not a guided task: there is no circle, no success check and no model
        call at all. It ends her current task first, because the Explorer window
        it opens would invalidate whatever step she was being shown anyway.

        No screenshot is taken on this path. The screen is not read, so there is
        nothing for the guard to inspect -- what ``actions`` checks instead is
        the *permission* (``can_do_it(FIND_FILE)``, CLAUDE.md rule 4).
        """
        if self._busy:
            return
        self._timer.stop()
        if self.loop is not None:
            self.loop.stop()
            self.loop = None
        self.overlay.clear()
        self.overlay.set_looking(False)
        self.window.set_task_active(False)
        self.window.set_status("Looking in your Downloads folder...")
        self.window.set_busy(True)
        self._run(lambda: actions_mod.find_for_her(goal), self._found_file)

    def _found_file(self, result: actions_mod.FindResult) -> None:
        """The search came back. Main thread."""
        log.info("find-it-for-me: opened=%s because=%s", result.opened,
                 result.hit.because if result.hit else "nothing")
        self.window.set_status(result.message)

    def do_for_her(self, goal: str) -> None:
        """"Do it for me": the booking flow (MASTERSPEC 3B, P9).

        A headed browser opens, the route and date go in, and it stops at her
        password with a circle around it. Everything after that is hers.

        The flow runs on the worker thread and must never touch the overlay, so
        it is handed two callbacks that only emit (CLAUDE.md rule 8). Qt
        delivers those emissions to :meth:`_circle_handover` and
        :meth:`_handover_done` on the main thread.
        """
        if self._busy:
            return
        self._timer.stop()
        if self.loop is not None:
            self.loop.stop()
            self.loop = None
        self._stopping = False
        self.overlay.clear()
        self.window.set_task_active(True)
        self.window.set_status("Opening the booking page. Watch what I type.")
        self.window.set_busy(True)
        # Read Qt's device pixel ratio here, on the main thread: it is the
        # app's one authority on screen scaling (CLAUDE.md rule 9), and the
        # worker must not touch a QScreen to ask for it.
        self._run(
            lambda scale=self.overlay.dpr: actions_mod.book_for_her(
                goal,
                on_handover=self.handover.emit,
                on_resume=self.handover_over.emit,
                is_stopped=lambda: self._stopping,
                screen_scale=scale,
            ),
            self._booking_done,
        )

    def _circle_handover(self, bbox: object, message: str) -> None:
        """Her field needs her. Draw the circle and say so. Main thread.

        ``bbox`` arrives as ``object`` because it may be None: if the browser
        window could not be located on screen we say the words without a
        circle, rather than drawing a ring somewhere wrong on a password box.
        """
        log.info("overlay: handing over%s", "" if bbox else " (no circle: no rect)")
        if bbox is None:
            self.overlay.show_message(message)
        else:
            self.overlay.show_circle(bbox, message)  # type: ignore[arg-type]
        self.window.set_status(message)

    def _handover_done(self) -> None:
        """She has moved on. Take the circle down. Main thread."""
        self.overlay.clear()
        self.window.set_status("Thank you - carrying on.")

    def _booking_done(self, outcome: actions_mod.BookingOutcome) -> None:
        """The flow finished, stopped or handed over for good. Main thread."""
        log.info("booking: ended=%s filled=%d handovers=%d",
                 outcome.ended, len(outcome.filled), len(outcome.handovers))
        self.overlay.clear()
        self.overlay.set_looking(False)
        self.window.set_task_active(False)
        self.window.set_status(outcome.message)

    def mark_done(self) -> None:
        """"I did it": force this step to succeed and move on (MASTERSPEC 5.5)."""
        if self.loop is None or self._busy:
            return
        if self.loop.phase is LoopPhase.DONE:
            return
        log.info("she says the step is done")
        self._advance(WatchOutcome.MANUAL)

    def _plan_stage(self, goal: str, history: Sequence[str] = (),
                    teach_notes: Sequence[str] = ()) -> StepOutcome:
        """What :class:`TaskLoop` calls to plan a step. Runs on the worker.

        ``capture.grab_primary`` is used directly rather than
        ``overlay.grab_without_me``: hiding and showing the overlay is a widget
        touch, so when that is needed it happens on the main thread, in
        :meth:`_begin_capture`, before this is ever reached.
        """
        return plan_next_step(goal, history=history, teach_notes=teach_notes)

    # --- the poll -----------------------------------------------------------

    def _tick(self) -> None:
        """One beat of the clock. Main thread; does no work itself."""
        if self._busy or self.loop is None:
            return
        if self.loop.phase is LoopPhase.DONE:
            self._timer.stop()
            return
        if self.loop.phase is LoopPhase.PAUSED:
            # Nothing of hers to check: read the screen again and ask the guard.
            self._run(self.loop.retry, self._step_ready)
            return

        step = self.loop.step
        if step is None:
            return
        self._run(lambda: self._gather(step), lambda screen: self._polled(step, screen))

    def _gather(self, step: Step) -> Screen:
        """Collect exactly as much evidence as this step's check needs.

        MASTERSPEC 5.4: cheap checks first, OCR only when the check needs it. A
        window-class check costs one win32 call and no capture at all, which is
        what keeps a CPU laptop responsive while she reads the instruction.
        """
        want_ocr = step.watch.needs_ocr_now()
        want_region = step.watch.baseline is not None
        window = watcher_mod.read_window()
        if not (want_ocr or want_region):
            return Screen(window=window)

        shot = capture_mod.grab_primary()
        texts: tuple[str, ...] | None = None
        if want_ocr:
            found, _ = ocr_mod.read_screen(shot.image, shot.monitor)
            texts = tuple(element.text for element in found)
        region = (
            watcher_mod.crop(shot.image, step.target.bbox_px if step.target else None,
                             step.outcome.monitor_offset)
            if want_region else None
        )
        return Screen(window=window, texts=texts, region=region)

    def _polled(self, step: Step, screen: Screen) -> None:
        """One poll came back. Main thread."""
        if self.loop is None or self.loop.phase is LoopPhase.DONE:
            return
        poll = step.watch.poll(screen)
        if poll.outcome is WatchOutcome.PENDING:
            if poll.show_hint:
                self._rephrase(step)
            return
        log.info("step %d: %s (%s)", step.index, poll.outcome.value, poll.reason)
        self._advance(poll.outcome)

    def _advance(self, outcome: WatchOutcome) -> None:
        loop = self.loop
        if loop is None:
            return
        self._run(lambda: loop.advance(outcome), self._step_ready)

    # --- showing it ---------------------------------------------------------

    def _step_ready(self, step: Step | None) -> None:
        """A plan came back from the worker. Main thread."""
        loop = self.loop
        if loop is None:
            return
        if step is None or loop.phase is LoopPhase.DONE:
            self._finish(step)
            return

        if step.paused:
            # CLAUDE.md rule 3: nothing was sent to the model, nothing is
            # circled, and we keep looking only to notice when she has moved on.
            self.overlay.show_message(step.plan.instruction)
            self.window.set_status(step.plan.instruction)
            self._timer.start(config.PAUSED_POLL_INTERVAL_MS)
            self.overlay.set_looking(True)
            return

        if step.target is None:
            # cannot_see_it: MASTERSPEC 5.3 says no circle, show the hint.
            self.overlay.clear()
        else:
            self.overlay.show_circle(step.target.bbox_px, step.plan.instruction)

        self.window.set_status(self._status_for(step))
        self._timer.start(config.POLL_INTERVAL_MS)
        self.overlay.set_looking(True)

    def _finish(self, step: Step | None) -> None:
        """The task is over: goal reached, step limit, or stopped."""
        loop = self.loop
        self._timer.stop()
        self.overlay.set_looking(False)
        self.window.set_task_active(False)

        if loop is None:
            return
        if loop.finished_because == "goal_reached" and step is not None:
            self.overlay.clear()
            self.window.set_status(step.message)
        elif loop.finished_because == "max_steps":
            self.overlay.clear()
            self.window.set_status(
                "I have run out of steps for this one. Shall we try again?"
            )
        log.info("task finished: %s, %d recoveries, %d pauses",
                 loop.finished_because, loop.recoveries, loop.pauses)

    def _rephrase(self, step: Step) -> None:
        """20 s and nothing has happened: say it once more, differently."""
        log.info("step %d has gone quiet; rephrasing once", step.index)
        extra = step.plan.hint_if_missing or "If you cannot see it, scroll a little."
        self.window.set_status(
            f"{step.message}\n\nStill waiting. {extra}\n"
            "If you have already done it, press “I did it”."
        )

    def _status_for(self, step: Step) -> str:
        message = step.message
        # The timing line is for me during the hackathon, not for her; it is
        # how the PROJECTPLAN checkpoints get checked without a stopwatch. Drop
        # it before she uses this.
        return (
            f"Step {step.index}: {message}\n"
            f"({step.outcome.element_count} things seen · "
            f"{step.outcome.total_ms / 1000:.1f} s)"
        )

    # --- the one worker -----------------------------------------------------

    def _run(self, work: Callable[[], object], on_done: Callable[..., None]) -> None:
        """Run one blocking callable off the main thread, then call back.

        The overlay is taken down first when capture exclusion is not available,
        because hiding a widget is a main-thread job and the worker is about to
        screenshot the screen the overlay is sitting on.
        """
        if self._busy:
            return
        self._begin_capture()

        self._thread = QThread()
        self._job = Job(work)
        self._job.moveToThread(self._thread)
        self._thread.started.connect(self._job.run)
        self._job.done.connect(on_done)
        self._job.failed.connect(self._show_failure)
        self._job.done.connect(self._thread.quit)
        self._job.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._clear_thread)
        self._thread.start()

    def _begin_capture(self) -> None:
        """Hide the overlay if Windows will not keep it out of our screenshots."""
        self._hid_overlay = (
            self.overlay.capture_mode is not CaptureMode.EXCLUDED
            and self.overlay.isVisible()
        )
        if self._hid_overlay:
            self.overlay.hide()

    def _end_capture(self) -> None:
        if self._hid_overlay:
            self._hid_overlay = False
            self.overlay.show()
            self.overlay.raise_()

    def _clear_thread(self) -> None:
        if self._thread is not None:
            self._thread.deleteLater()
        self._thread = None
        self._job = None
        self._end_capture()
        self.window.set_busy(False)

    def _show_failure(self, message: str) -> None:
        """A job raised. Stop the clock rather than retry a broken pipeline."""
        log.error("stopping the task after a failure: %s", message)
        self._timer.stop()
        self.overlay.set_looking(False)
        self.overlay.clear()
        if self.loop is not None:
            self.loop.stop()
        self.window.set_task_active(False)
        self.window.set_status(f"Something went wrong: {message}")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # The hotkey must outlive the window.

    tiny = TinyMe()
    tiny.start()
    try:
        return app.exec()
    finally:
        tiny.hotkeys.stop()
        # Any browser we opened for her closes with the app, not with the
        # task: during a task she is still typing in it (actions.close_browser).
        actions_mod.close_browser()


if __name__ == "__main__":
    raise SystemExit(main())
