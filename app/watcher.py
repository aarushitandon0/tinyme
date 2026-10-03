"""Did the step work? Decided in code, never by the model (MASTERSPEC 5.4-5.5).

CLAUDE.md rule 2: the model proposes which check to run; this module runs it. No
Gemma call happens in here and none should ever be added - the moment success is
model-judged, a confident wrong answer leaves her stuck looking at a circle on
something that already worked.

Two costs shape the design:

* **OCR is the expensive stage**, 10-22 s per full screen on her class of laptop
  (notes/bench.md). So the two text checks are the only ones that may ask for it,
  :func:`needs_ocr` says so up front, and a poll where OCR was skipped reports
  "don't know" (pending) rather than guessing either way.
* **Qt owns the clock.** Nothing here touches a widget, a QTimer or a thread;
  ``main.py`` drives :meth:`StepWatch.poll` from a timer and gathers the
  :class:`Screen` it is given. That keeps every rule in this file testable
  without a desktop (CLAUDE.md rule 8).

A :class:`Screen` is one poll's worth of evidence. Gathering it is the caller's
job, because only the caller knows what it can afford this tick.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence

import numpy as np

from app import config
from app.brain import CheckType, SuccessCheck
from app.elements import Bbox

log = logging.getLogger(__name__)

#: MASTERSPEC 5.4: fuzzy text match threshold, shared with the element merge.
TEXT_RATIO = 85.0


@dataclass(frozen=True)
class WindowInfo:
    """The foreground window, as win32 reports it.

    ``class_name`` is the identity (CLAUDE.md rule 11: Explorer is
    ``CabinetWClass``, and its title is the folder name). ``hwnd`` is what tells
    us the window changed under us, even to another window of the same app.
    """

    class_name: str = ""
    title: str = ""
    hwnd: int = 0


@dataclass(frozen=True)
class Screen:
    """One poll's evidence. ``None`` means "not gathered", not "empty".

    ``texts`` is ``None`` when OCR was skipped this tick and a tuple (possibly
    empty) when it ran. The distinction matters: "I did not look" must never read
    as "the text is gone".
    """

    window: WindowInfo
    texts: tuple[str, ...] | None = None
    region: np.ndarray | None = None


class Outcome(Enum):
    """What one poll concluded."""

    PENDING = "pending"
    PASSED = "passed"
    #: The screen moved on in a way the check did not predict: re-plan from a
    #: fresh capture rather than repeating a step that no longer applies.
    REPLAN = "replanned"
    #: She pressed "I did it".
    MANUAL = "manual"


@dataclass(frozen=True)
class Poll:
    """The result of one poll, plus whether it is time to say something."""

    outcome: Outcome
    #: True exactly once, on the poll where the step has gone quiet too long.
    show_hint: bool = False
    #: Why, for the log and for the recovery metric. Numbers and enums only.
    reason: str = ""


def needs_ocr(check: SuccessCheck) -> bool:
    """True if this check cannot be answered without a fresh OCR pass."""
    return check.type in (CheckType.TEXT_APPEARS, CheckType.TEXT_DISAPPEARS)


def region_diff(before: np.ndarray, after: np.ndarray) -> float:
    """Mean absolute pixel difference between two captures of the same region.

    Returns ``inf`` when the shapes differ: the window moved or resized, which is
    a change by any reading, and is not something to crash on mid-task.

    Arrays are cast to a signed type first. Subtracting ``uint8`` arrays wraps
    around - ``0 - 255`` is ``1`` - which would report a black-to-white flip as
    no change at all.
    """
    if before is None or after is None:
        return 0.0
    if before.shape != after.shape:
        return float("inf")
    return float(np.abs(before.astype(np.int16) - after.astype(np.int16)).mean())


def _text_matches(value: str, texts: Sequence[str]) -> bool:
    """True if any OCR string is this value, allowing for OCR slips.

    ``ratio``, not ``partial_ratio``: notes/bench.md records a false positive
    where ordinary screen text cleared 85 on ``partial_ratio``, and a false
    success here tells her a step worked when it did not.
    """
    from rapidfuzz import fuzz

    needle = value.strip().casefold()
    if not needle:
        return False
    return any(fuzz.ratio(needle, (text or "").strip().casefold()) >= TEXT_RATIO
               for text in texts)


def check_passed(
    check: SuccessCheck,
    screen: Screen,
    baseline: np.ndarray | None = None,
    region_threshold: float = config.REGION_DIFF_THRESHOLD,
) -> bool:
    """Run one check. False means "not yet" and never "it failed forever".

    Cheap checks only read :class:`WindowInfo`. The text checks need
    ``screen.texts``, and return False if it was not gathered.
    """
    value = (check.value or "").strip()

    if check.type is CheckType.WINDOW_CLASS_IS:
        # Exact identity, case-insensitively. A substring match would accept a
        # different class that happens to start the same way.
        return bool(value) and screen.window.class_name.casefold() == value.casefold()

    if check.type is CheckType.WINDOW_TITLE_CONTAINS:
        # Empty would be contained in every title, i.e. instant false success.
        return bool(value) and value.casefold() in screen.window.title.casefold()

    if check.type is CheckType.TEXT_APPEARS:
        if screen.texts is None:
            return False
        return _text_matches(value, screen.texts)

    if check.type is CheckType.TEXT_DISAPPEARS:
        # An empty reading means OCR found nothing, which is a failed read, not
        # evidence that the text went away.
        if not screen.texts:
            return False
        return not _text_matches(value, screen.texts)

    if check.type is CheckType.REGION_CHANGED:
        if baseline is None or screen.region is None:
            return False
        return region_diff(baseline, screen.region) > region_threshold

    log.warning("unknown check type %r treated as pending", check.type)
    return False


class StepWatch:
    """Watches one step: has it worked, has the screen moved on, is it stuck.

    Holds the step's starting state - the window that was in front and the
    pixels under the circle - so a later poll can tell "nothing has happened
    yet" from "something happened that the check did not predict".
    """

    def __init__(
        self,
        check: SuccessCheck,
        target_bbox: Bbox | None = None,
        baseline: np.ndarray | None = None,
        start_window: WindowInfo | None = None,
        clock: Callable[[], float] = time.monotonic,
        hint_after_s: float = config.STEP_HINT_AFTER_S,
        region_threshold: float = config.REGION_DIFF_THRESHOLD,
    ) -> None:
        self.check = check
        self.target_bbox = target_bbox
        self.baseline = baseline
        self.start_window = start_window
        self.region_threshold = region_threshold
        self._clock = clock
        self._hint_after_s = hint_after_s
        self._started = clock()
        self._hinted = False

    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._started

    def needs_ocr_now(self) -> bool:
        """Whether this tick should pay for OCR."""
        return needs_ocr(self.check)

    def done_by_her(self) -> Poll:
        """She pressed "I did it" - MASTERSPEC 5.5's always-visible escape."""
        return Poll(outcome=Outcome.MANUAL, reason="manual")

    def poll(self, screen: Screen) -> Poll:
        """Look once and decide. Order matters: success is checked first.

        A step that worked must never be reported as an unexpected change just
        because the foreground window moved - opening File Explorer is both at
        once.
        """
        if check_passed(self.check, screen, self.baseline, self.region_threshold):
            return Poll(outcome=Outcome.PASSED, reason=self.check.type.value)

        # A window we were not watching has come to the front: whatever she did,
        # the old step's circle no longer describes this screen.
        if (self.start_window is not None and screen.window.hwnd
                and screen.window.hwnd != self.start_window.hwnd):
            return Poll(outcome=Outcome.REPLAN, reason="window changed")

        # The thing under the circle changed, yet the check still says no. For a
        # region_changed check that would be the success above, so this can only
        # fire for the other four: something happened that the check did not
        # predict, and re-planning beats repeating.
        if (self.check.type is not CheckType.REGION_CHANGED
                and self.baseline is not None and screen.region is not None
                and region_diff(self.baseline, screen.region) > self.region_threshold):
            return Poll(outcome=Outcome.REPLAN, reason="target changed")

        hint = False
        if not self._hinted and self.elapsed_s >= self._hint_after_s:
            self._hinted = True
            hint = True
        return Poll(outcome=Outcome.PENDING, show_hint=hint)


# --- gathering the evidence ------------------------------------------------
# The checks above are pure, which is what makes them testable. These two read
# the real machine, so they live apart, each wrapped with its own fallback
# (CLAUDE.md: every external call has a timeout and a clear fallback). A poll
# that cannot read the window reports an empty WindowInfo, which fails every
# cheap check -- "not yet", never a false success.


def read_window() -> WindowInfo:
    """The foreground window's class, title and handle, or blanks."""
    try:
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return WindowInfo()
        return WindowInfo(
            class_name=win32gui.GetClassName(hwnd) or "",
            title=win32gui.GetWindowText(hwnd) or "",
            hwnd=int(hwnd),
        )
    except Exception:
        log.debug("could not read the foreground window", exc_info=True)
        return WindowInfo()


def crop(
    image: np.ndarray | None,
    bbox: Bbox | None,
    monitor_offset: tuple[int, int] = (0, 0),
    pad: int = config.REGION_PAD_PX,
) -> np.ndarray | None:
    """Cut the padded target area out of a capture, or None if it cannot be.

    ``bbox`` is in physical *screen* pixels and the image starts at the
    monitor's own origin, so the offset is subtracted here -- the only place
    that conversion is needed, since everything else compares two crops taken
    the same way.

    Returns None for an empty or off-image box rather than a zero-size array,
    so :func:`region_diff` is never handed something it cannot compare.
    """
    if image is None or bbox is None:
        return None

    left_offset, top_offset = monitor_offset
    height, width = image.shape[:2]
    left = max(0, int(bbox[0] - left_offset) - pad)
    top = max(0, int(bbox[1] - top_offset) - pad)
    right = min(width, int(bbox[2] - left_offset) + pad)
    bottom = min(height, int(bbox[3] - top_offset) + pad)
    if right <= left or bottom <= top:
        return None
    # Copy: the caller keeps this as a baseline for the whole step, and the
    # capture it came from is dropped after this poll (CLAUDE.md rule 6).
    return np.ascontiguousarray(image[top:bottom, left:right])
