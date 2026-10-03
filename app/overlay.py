"""The marks she actually sees (MASTERSPEC 5.6).

Everything Tiny Me draws on her screen lives here. The vocabulary is small on
purpose -- she is trying to use File Explorer, not read our annotations:

* a **frame** (rounded rectangle) on things that are already rectangles: a
  list row, a button, a taskbar label. It sits on the real edges, so there is
  no doubt which row is meant.
* a **ring** (ellipse) on everything else: a word in running text, an icon,
  anything whose bounds OCR only roughly knows. Looser on purpose, because
  "around here" is the honest claim for a fuzzy bound.
* a **callout** -- the instruction, in a card with a tail pointing at the
  mark, or a leader line when the card had to be clamped out of tail range.
* a **step counter**, bottom-centre, which climbs out of the way of a callout
  rather than sitting on it.
* the **"Tiny Me is looking" indicator**, visible for exactly as long as the
  screen is being read (MASTERSPEC 6).

:func:`choose_shape` picks between the first two from the target's
proportions. The model is not consulted and never could be: it returns an
element id, never geometry (CLAUDE.md rule 1).

Three things make this module fiddly, and all three are listed as risks in
MASTERSPEC 14:

1. ``mss`` reports physical pixels, Qt paints in logical pixels. Everything
   here paints in logical pixels, converted once through
   ``elements.to_logical`` (CLAUDE.md rule 9).
2. Her clicks must pass straight through the overlay, so it is
   ``WindowTransparentForInput``.
3. The overlay must not appear in our own screenshots, or the next OCR pass
   reads our own instruction text back and the model plans against it. We ask
   Windows to exclude it from capture, then *verify* that it worked, and fall
   back to hide->capture->show when it did not.

CLAUDE.md rule 8: every method here touches Qt widgets, so every method must be
called on the Qt main thread. Worker threads talk to it through signals.
"""

from __future__ import annotations

import ctypes
import logging
import math
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QApplication, QWidget

from app import theme
from app.elements import Bbox, to_logical

log = logging.getLogger(__name__)

#: SetWindowDisplayAffinity: hide the window from screen capture entirely.
#: Windows 10 2004 and later. 0x11 = WDA_EXCLUDEFROMCAPTURE.
WDA_EXCLUDEFROMCAPTURE = 0x11

#: Padding around the target, in logical pixels, so the ring sits outside the
#: thing itself rather than on top of the text she is meant to read.
CIRCLE_PAD = 20
#: Thick enough to find on a bright screen at arm's length.
STROKE_WIDTH = 6
#: The same warm orange the window uses to mark where she is, so "look here"
#: means one colour everywhere in the app.
RING_COLOUR = QColor(theme.HIGHLIGHT)
RING_GLOW = QColor(theme.HIGHLIGHT_WARM)
LABEL_BG = QColor(theme.FOREST_DEEP)
LABEL_FG = QColor(theme.CREAM)
LABEL_FONT_PT = 15
LABEL_PAD = 12
LABEL_GAP = 16
LABEL_RADIUS = 12
#: Height of the little triangle that points the label at its circle.
LABEL_POINTER = 7
SCREEN_MARGIN = 8

#: The ring breathes rather than sitting still: on a busy screen a static
#: outline reads as part of the app underneath, and a moving one does not.
#: 20 fps, and only while something is actually circled -- this is a
#: full-screen translucent window on a CPU laptop, so it must not repaint
#: when there is nothing to animate.
#: A frame hugs the target more tightly than a ring: it sits on the real
#: edges, so it needs less air around it to stay legible.
FRAME_PAD = 7
FRAME_RADIUS = 7

#: Choosing between the two shapes (see :func:`choose_shape`). A list row, a
#: button or a taskbar label is wide and short; a word in running text is not.
#: These are logical pixels and a plain ratio, both deliberately loose -- the
#: cost of getting it wrong is a slightly odd-looking mark, never a wrong
#: click, so there is nothing here worth tuning finely.
FRAME_MIN_WIDTH = 56.0
FRAME_MIN_HEIGHT = 14.0
FRAME_MIN_RATIO = 2.6

#: Bottom-centre progress pill. Bottom, because the taskbar is the one strip
#: of screen she is never reading, and centre because she may have the window
#: on either side.
STEP_FONT_PT = 11
STEP_BOTTOM_MARGIN = 86
STEP_DOT = 7.0
STEP_DOT_GAP = 5.0
#: Space left between the counter and whatever it had to climb over.
STEP_CLEARANCE = 12.0

#: When the callout has to be clamped so far sideways that its tail would
#: point at thin air, we draw a line to the mark instead. This is how far the
#: mark's centre may sit outside the card's flat edge before that happens.
LEADER_SLACK = 4.0
LEADER_WIDTH = 2.4
#: ... and this is how far the card's edge may sit from the mark before the
#: tail is too short to bridge it. ``LABEL_GAP`` is the gap it is normally
#: placed at, so this is "a bit more than usual, but not a different part of
#: the screen".
LEADER_MAX_GAP = LABEL_GAP * 1.8

PULSE_INTERVAL_MS = 50
PULSE_SPEED = 0.10
#: How far the ring swells, in logical pixels.
PULSE_AMPLITUDE = 3.5
#: How long a new circle takes to appear, in frames of the pulse timer.
APPEAR_FRAMES = 7.0
#: Where a circle-less message sits, in logical pixels from the top.
BANNER_TOP = 48

#: MASTERSPEC 6's promise, in the words she reads.
LOOKING_TEXT = "Tiny Me is looking"
LOOKING_BG = QColor(theme.FOREST_DEEP)
LOOKING_DOT = QColor(theme.HIGHLIGHT)
#: Room reserved on the left of the indicator for its breathing dot.
INDICATOR_DOT_SPACE = 18.0
LOOKING_FONT_PT = 11

#: Probe text for the capture-exclusion check. Deliberately nonsense: an
#: earlier version used "TINYME OVERLAY CHECK", and fuzzy-matching a needle of
#: common words against a hundred arbitrary screen strings produced false
#: positives, which pinned the app to the slow hide->capture->show path even
#: though exclusion was working fine. A token no real UI contains cannot.
EXCLUSION_PROBE = "XQZ7-TINYME-PROBE-XQZ7"
#: High, because the probe is distinctive; this only absorbs OCR character slips.
EXCLUSION_MATCH_RATIO = 90


class CaptureMode(Enum):
    """Which route keeps the overlay out of our screenshots."""

    #: Windows excludes the overlay for us. Nothing to do at capture time.
    EXCLUDED = "excluded"
    #: Exclusion did not take. Hide, capture, show again.
    HIDE_SHOW = "hide_show"
    #: Not checked yet.
    UNKNOWN = "unknown"


class MarkShape(Enum):
    """How a target gets marked."""

    #: A hand-drawn circle. The default, and right for a word in running text
    #: or anything whose bounds OCR only roughly knows.
    RING = "ring"
    #: A rounded rectangle on the target's real edges. Right for things that
    #: are already rectangles: a list row, a button, a taskbar icon.
    FRAME = "frame"


def choose_shape(bbox_px: Bbox, dpr: float) -> MarkShape:
    """Pick the mark that fits the target's shape.

    Kept out of :class:`Overlay` and free of Qt so it can be tested without a
    screen. It takes physical pixels and the scale factor, like everything
    else that crosses that boundary (CLAUDE.md rule 9).

    The rule: a target that is distinctly wider than it is tall, and tall
    enough to be a real row rather than a thin sliver of text, gets a frame.
    An ellipse around a 300 px wide, 20 px tall list row is enormous and
    points at three rows at once; a rectangle on its edges points at one.
    """
    left, top, right, bottom = to_logical(bbox_px, dpr)
    width = abs(right - left)
    height = abs(bottom - top)
    if height <= 0 or width <= 0:
        return MarkShape.RING
    if width < FRAME_MIN_WIDTH or height < FRAME_MIN_HEIGHT:
        return MarkShape.RING
    return MarkShape.FRAME if width / height >= FRAME_MIN_RATIO else MarkShape.RING


@dataclass(frozen=True)
class Mark:
    """One mark and the words that go with it."""

    bbox_px: Bbox
    instruction: str = ""
    #: ``None`` means "decide from the shape of the target", which is what the
    #: task loop always wants. An explicit value is for the calibration script
    #: and the capture probe, which want a predictable mark.
    shape: MarkShape | None = None


def probe_leaked(elements, probe: str = EXCLUSION_PROBE) -> bool:
    """True if the probe text shows up in OCR of a screenshot.

    Matching is deliberately strict. A loose fuzzy match here is worse than no
    check at all: a false positive silently doubles capture cost for the whole
    session, on a laptop where capture and OCR are already the slow part.
    """
    from rapidfuzz import fuzz

    needle = probe.casefold()
    for element in elements:
        text = element.text.casefold()
        if needle in text:
            return True
        # Only consider strings long enough to plausibly contain the probe, so
        # a short unrelated label cannot score highly on partial_ratio.
        if len(text) >= 0.6 * len(needle) and fuzz.partial_ratio(needle, text) >= EXCLUSION_MATCH_RATIO:
            return True
    return False


class Overlay(QWidget):
    """A full-screen, click-through, capture-excluded drawing surface."""

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Belt and braces with WindowTransparentForInput: never take focus and
        # never steal a click from the app underneath.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._marks: list[Mark] = []
        #: Words shown without a circle: the guard's handover (MASTERSPEC 6),
        #: where there is deliberately nothing to point at.
        self._message = ""
        #: MASTERSPEC 6: visible whenever capture is active, so she is never
        #: being looked at without being told. Drawn here rather than in its own
        #: window so it inherits this one's capture exclusion for free -- and so
        #: there is exactly one window that can leak text into our own OCR.
        self._looking = False
        self._capture_mode = CaptureMode.UNKNOWN
        self._exclusion_applied = False
        #: Phase of the breathing ring, and how far a new circle has appeared.
        self._phase = 0.0
        self._appear = 1.0
        #: "Step 2 of 3", or (0, 0) for nothing. Shown only while something is
        #: circled: a counter with no mark beside it is just clutter.
        self._step = 0
        self._steps = 0
        self._pulse = QTimer(self)
        self._pulse.setInterval(PULSE_INTERVAL_MS)
        self._pulse.timeout.connect(self._animate)
        self.cover_primary_screen()

    # --- geometry ---------------------------------------------------------

    def cover_primary_screen(self) -> None:
        """Size the overlay to the whole primary screen, in logical pixels."""
        screen = QApplication.primaryScreen()
        if screen is None:
            raise RuntimeError("no primary screen")
        self.setGeometry(screen.geometry())

    @property
    def dpr(self) -> float:
        """The primary screen's device pixel ratio (1.0 at 100%, 1.25 at 125%)."""
        screen = self.screen() or QApplication.primaryScreen()
        return float(screen.devicePixelRatio())

    @property
    def capture_mode(self) -> CaptureMode:
        return self._capture_mode

    # --- showing marks ----------------------------------------------------

    def show_marks(self, marks: list[Mark], *, animated: bool = True) -> None:
        """Replace what is drawn and make sure the overlay is visible.

        ``animated=False`` is for the capture-exclusion probe, which captures
        the screen one frame after showing its mark: a ring that is still
        fading in would be a weaker test than a ring at full strength, and the
        whole point of that check is that it is not optimistic.
        """
        self._marks = list(marks)
        self._appear = 0.0 if (animated and marks) else 1.0
        if not self.isVisible():
            self.show()
        self._apply_capture_exclusion()
        self.raise_()
        self._sync_pulse()
        self.update()

    def _animate(self) -> None:
        """One frame: advance the breath, and finish any appearance."""
        self._phase = (self._phase + PULSE_SPEED) % (2 * math.pi)
        if self._appear < 1.0:
            self._appear = min(1.0, self._appear + 1.0 / APPEAR_FRAMES)
        self.update()

    def _sync_pulse(self) -> None:
        """Run the timer only while there is something that moves."""
        wanted = bool(self._marks) or self._looking
        if wanted and not self._pulse.isActive():
            self._pulse.start()
        elif not wanted and self._pulse.isActive():
            self._pulse.stop()

    def show_circle(self, bbox_px: Bbox, instruction: str = "",
                    shape: MarkShape | None = None) -> None:
        """Mark one thing. This is what the task loop uses.

        Named ``show_circle`` still, because that is what it is to her and
        every caller already says it; ``shape`` is how a caller asks for a
        frame, and ``None`` lets :func:`choose_shape` decide.
        """
        self._message = ""
        self.show_marks([Mark(bbox_px=bbox_px, instruction=instruction,
                              shape=shape)])

    def show_message(self, text: str) -> None:
        """Say something with no circle, for when there is nothing to point at.

        Used by the guard's handover: on a password screen we have deliberately
        not looked closely enough to know where the box is.
        """
        self._message = (text or "").strip()
        self.show_marks([])

    def set_looking(self, looking: bool) -> None:
        """Show or hide the "Tiny Me is looking" indicator (MASTERSPEC 6)."""
        if looking == self._looking:
            return
        self._looking = looking
        if looking and not self.isVisible():
            self.show()
            self._apply_capture_exclusion()
        self._sync_pulse()
        self.update()

    @property
    def looking(self) -> bool:
        return self._looking

    def set_step(self, index: int, total: int) -> None:
        """Say which step she is on, bottom-centre of her screen.

        ``total`` is a floor, not a promise: the loop plans one step at a time
        and does not know how many there will be, so it passes the best guess
        it has. Showing "Step 4 of 3" would be worse than showing a total that
        grows, so the total is nudged up to the index when it has to be.
        """
        index = max(0, int(index))
        self._step = index
        self._steps = max(0, int(total), index)
        self.update()

    def clear_step(self) -> None:
        self.set_step(0, 0)

    def clear(self) -> None:
        self._marks = []
        self._message = ""
        self._step = 0
        self._steps = 0
        self._appear = 1.0
        self._sync_pulse()
        self.update()

    # --- capture exclusion ------------------------------------------------

    def _apply_capture_exclusion(self) -> bool:
        """Ask Windows to keep this window out of screen captures.

        Safe to call repeatedly; only the first successful call does work.
        """
        if self._exclusion_applied:
            return True
        try:
            hwnd = int(self.winId())
            ok = bool(
                ctypes.windll.user32.SetWindowDisplayAffinity(
                    ctypes.c_void_p(hwnd), ctypes.c_uint(WDA_EXCLUDEFROMCAPTURE)
                )
            )
        except Exception:
            log.warning("SetWindowDisplayAffinity unavailable", exc_info=True)
            return False
        if ok:
            self._exclusion_applied = True
            log.info("overlay excluded from capture via SetWindowDisplayAffinity")
        else:
            error = ctypes.windll.kernel32.GetLastError()
            log.warning("SetWindowDisplayAffinity failed (GetLastError=%d)", error)
        return ok

    def affinity_is_set(self) -> bool:
        """Read the affinity back from Windows, as a cheap sanity check.

        This confirms the flag is on the window we think it is. It does not
        prove mss cannot see us, which is what verify_capture_exclusion is for.
        """
        try:
            value = ctypes.c_uint(0)
            ok = ctypes.windll.user32.GetWindowDisplayAffinity(
                ctypes.c_void_p(int(self.winId())), ctypes.byref(value)
            )
            return bool(ok) and value.value == WDA_EXCLUDEFROMCAPTURE
        except Exception:
            log.debug("GetWindowDisplayAffinity unavailable", exc_info=True)
            return False

    def verify_capture_exclusion(self, probe_text: str = EXCLUSION_PROBE) -> CaptureMode:
        """Prove the overlay is invisible to ``mss`` instead of assuming it.

        Shows a mark carrying a distinctive probe string, captures the screen,
        OCRs it, and looks for that string. Finding it means exclusion silently
        failed and we must hide before every capture.

        Returns the mode now in force, and logs which path is active so the
        README and the post can state it truthfully.
        """
        from app import capture as capture_mod
        from app import ocr as ocr_mod

        screen_rect = self.geometry()
        dpr = self.dpr
        # Put the probe in the middle of the screen, in physical pixels.
        centre_x = (screen_rect.x() + screen_rect.width() / 2) * dpr
        centre_y = (screen_rect.y() + screen_rect.height() / 2) * dpr
        self.show_marks([
            Mark(
                bbox_px=(centre_x - 200, centre_y - 40, centre_x + 200, centre_y + 40),
                instruction=probe_text,
                shape=MarkShape.RING,
            )
        ], animated=False)
        QApplication.processEvents()

        shot = capture_mod.grab_primary()
        elements, _ = ocr_mod.read_screen(shot.image, shot.monitor)
        leaked = probe_leaked(elements, probe_text)
        self.clear()

        if leaked:
            self._capture_mode = CaptureMode.HIDE_SHOW
            log.warning(
                "overlay IS visible to mss; using the hide->capture->show fallback"
            )
        else:
            self._capture_mode = CaptureMode.EXCLUDED
            log.info(
                "overlay is invisible to mss; capture exclusion is active "
                "(affinity flag readable: %s)", self.affinity_is_set(),
            )
        return self._capture_mode

    def grab_without_me(self):
        """Capture the screen without the overlay appearing in it.

        Uses whichever path :meth:`verify_capture_exclusion` proved works.

        **Main thread only.** The fallback path hides and shows a widget, so the
        task loop does not use this: ``main.TinyMe`` takes the overlay down on
        the main thread before handing the capture to a worker (CLAUDE.md rule
        8). This stays for the one-shot scripts (``scripts/calibrate.py``) and
        for the exclusion check itself, which both run on the main thread.
        """
        from app import capture as capture_mod

        if self._capture_mode is CaptureMode.EXCLUDED or not self.isVisible():
            return capture_mod.grab_primary()

        # Unknown or known-broken exclusion: take the overlay down first. The
        # flicker is worth not reading our own text back into the model.
        self.hide()
        QApplication.processEvents()
        try:
            return capture_mod.grab_primary()
        finally:
            self.show()
            self._apply_capture_exclusion()
            self.raise_()
            QApplication.processEvents()

    # --- painting ---------------------------------------------------------

    def _label_rect(self, ring: QRectF, text: str, metrics: QFontMetricsF) -> tuple[QRectF, bool]:
        """Place the label near the ring but always fully on screen.

        Returns the box and whether it ended up *below* the ring, which is what
        decides which way its pointer faces.
        """
        width = metrics.horizontalAdvance(text) + 2 * LABEL_PAD
        height = metrics.height() + 2 * LABEL_PAD
        # Prefer below the ring; flip above when there is no room.
        below = True
        top = ring.bottom() + LABEL_GAP
        if top + height > self.height() - SCREEN_MARGIN:
            below = False
            top = ring.top() - LABEL_GAP - height
        left = ring.center().x() - width / 2

        left = max(SCREEN_MARGIN, min(left, self.width() - width - SCREEN_MARGIN))
        top = max(SCREEN_MARGIN, min(top, self.height() - height - SCREEN_MARGIN))
        return QRectF(left, top, width, height), below

    def _banner_rect(self, text: str, metrics: QFontMetricsF) -> QRectF:
        """A wide label across the top-centre, for words with no circle."""
        width = metrics.horizontalAdvance(text) + 2 * LABEL_PAD
        height = metrics.height() + 2 * LABEL_PAD
        left = max(SCREEN_MARGIN, (self.width() - width) / 2)
        return QRectF(left, BANNER_TOP, width, height)

    def _indicator_rect(self, metrics: QFontMetricsF) -> QRectF:
        """Top-right corner, out of the way of almost every app's toolbar."""
        width = (metrics.horizontalAdvance(LOOKING_TEXT) + 2 * LABEL_PAD
                 + INDICATOR_DOT_SPACE)
        height = metrics.height() + 2 * LABEL_PAD
        return QRectF(self.width() - width - SCREEN_MARGIN, SCREEN_MARGIN,
                      width, height)

    @staticmethod
    def _tail_reaches(box: QRectF, point_at: QPointF, below: bool = True) -> bool:
        """Can the card's tail actually touch the mark?

        Two ways it cannot, both caused by the card being clamped on screen:

        * *sideways* -- the mark's centre falls outside the card's flat edge,
          so a tail pinned to a rounded corner points at empty screen.
        * *vertically* -- the card could not sit its usual ``LABEL_GAP`` from
          the mark (the mark runs off the bottom of the screen, or a tall card
          was pushed back inside it), so a 7 px tail cannot bridge the gap.

        A tail that points somewhere wrong is worse than no tail, so when this
        is False the caller draws a leader line instead.
        """
        flat_left = box.left() + LABEL_RADIUS + LABEL_POINTER
        flat_right = box.right() - LABEL_RADIUS - LABEL_POINTER
        if flat_right < flat_left:
            return False
        if not (flat_left - LEADER_SLACK) <= point_at.x() <= (flat_right + LEADER_SLACK):
            return False
        edge_y = box.top() if below else box.bottom()
        return abs(edge_y - point_at.y()) <= LEADER_MAX_GAP

    def _draw_leader(self, painter: QPainter, box: QRectF, point_at: QPointF,
                     below: bool, opacity: float) -> None:
        """A short line from the card's near corner to the mark."""
        painter.save()
        painter.setOpacity(painter.opacity() * opacity)
        start_x = box.right() if point_at.x() > box.center().x() else box.left()
        start = QPointF(start_x, box.top() if below else box.bottom())
        pen = QPen(QColor(LABEL_BG), LEADER_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(start, point_at)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(LABEL_BG))
        painter.drawEllipse(point_at, 3.2, 3.2)
        painter.restore()

    def _step_rect(self, text: str, metrics: QFontMetricsF) -> QRectF:
        """The progress pill, bottom-centre and clear of the taskbar."""
        dots = self._steps * STEP_DOT + max(0, self._steps - 1) * STEP_DOT_GAP
        width = metrics.horizontalAdvance(text) + 2 * LABEL_PAD + dots + 10
        height = metrics.height() + 2 * LABEL_PAD - 4
        return QRectF(max(SCREEN_MARGIN, (self.width() - width) / 2),
                      self.height() - STEP_BOTTOM_MARGIN - height,
                      width, height)

    @staticmethod
    def _lift_clear(box: QRectF, occupied: list[QRectF]) -> QRectF:
        """Move ``box`` straight up until it clears everything in ``occupied``.

        The counter sits bottom-centre, which is exactly where the callout for
        a target low on the screen lands. Rather than pick a corner nothing
        ever uses, the counter gives way: it is the least important thing on
        screen, and it is the one that can move without losing its meaning.
        """
        home = QRectF(box)
        for _ in range(8):
            hit = next((r for r in occupied if r.intersects(box)), None)
            if hit is None:
                return box
            lifted = QRectF(box.left(), hit.top() - box.height() - STEP_CLEARANCE,
                            box.width(), box.height())
            if lifted.top() < SCREEN_MARGIN:
                # Nowhere left to climb: the screen is full of marks. Leave it
                # where it started rather than push it off the top edge -- a
                # counter half under a callout still reads, one off-screen
                # does not.
                return home
            box = lifted
        return box

    def _draw_step(self, painter: QPainter, occupied: list[QRectF] | None = None) -> None:
        """"Step 2 of 3", with one dot per step: done, now, still to come."""
        if self._step <= 0 or self._steps <= 0:
            return
        font = QFont()
        font.setPointSize(STEP_FONT_PT)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        text = f"Step {self._step} of {self._steps}"
        box = self._step_rect(text, metrics)
        if occupied:
            box = self._lift_clear(box, occupied)

        painter.setPen(Qt.PenStyle.NoPen)
        halo = QColor(LABEL_BG)
        halo.setAlpha(60)
        painter.setBrush(halo)
        painter.drawRoundedRect(box.translated(0, 2), box.height() / 2,
                                box.height() / 2)
        solid = QColor(LABEL_BG)
        solid.setAlpha(242)
        painter.setBrush(solid)
        painter.drawRoundedRect(box, box.height() / 2, box.height() / 2)

        x = box.left() + LABEL_PAD
        for i in range(self._steps):
            centre = QPointF(x + STEP_DOT / 2, box.center().y())
            if i + 1 < self._step:
                colour = QColor(theme.ONLINE_DOT)          # done
            elif i + 1 == self._step:
                colour = QColor(RING_COLOUR)               # now
            else:
                colour = QColor(LABEL_FG)                  # still to come
                colour.setAlphaF(0.3)
            painter.setBrush(colour)
            painter.drawEllipse(centre, STEP_DOT / 2, STEP_DOT / 2)
            x += STEP_DOT + STEP_DOT_GAP

        painter.setPen(QPen(LABEL_FG))
        painter.drawText(QRectF(x + 5, box.top(), box.right() - x - 5 - LABEL_PAD,
                                box.height()),
                         Qt.AlignmentFlag.AlignCenter, text)

    def _draw_label(self, painter: QPainter, box: QRectF, text: str,
                    background: QColor, *, point_at: QPointF | None = None,
                    below: bool = True, opacity: float = 1.0,
                    left_inset: float = 0.0) -> None:
        """A rounded card of words, optionally with a tail pointing at a ring."""
        painter.save()
        painter.setOpacity(painter.opacity() * opacity)
        painter.setPen(Qt.PenStyle.NoPen)

        shape = QPainterPath()
        shape.addRoundedRect(box, LABEL_RADIUS, LABEL_RADIUS)
        if point_at is not None and self._tail_reaches(box, point_at, below):
            # The tail is clamped to the flat part of the edge so it never
            # grows out of a rounded corner.
            tip_x = max(box.left() + LABEL_RADIUS + LABEL_POINTER,
                        min(point_at.x(), box.right() - LABEL_RADIUS - LABEL_POINTER))
            edge_y = box.top() if below else box.bottom()
            tip_y = edge_y - LABEL_POINTER if below else edge_y + LABEL_POINTER
            tail = QPainterPath()
            tail.moveTo(tip_x - LABEL_POINTER, edge_y)
            tail.lineTo(tip_x, tip_y)
            tail.lineTo(tip_x + LABEL_POINTER, edge_y)
            tail.closeSubpath()
            shape = shape.united(tail)

        # A soft halo so the words stay readable on a white or a dark app.
        halo = QColor(background)
        halo.setAlpha(60)
        painter.setBrush(halo)
        painter.drawPath(shape.translated(0, 2))

        solid = QColor(background)
        solid.setAlpha(242)
        painter.setBrush(solid)
        painter.drawPath(shape)

        painter.setPen(QPen(LABEL_FG))
        # ``left_inset`` keeps the words clear of anything drawn inside the
        # card on the left, such as the indicator's breathing dot.
        painter.drawText(box.adjusted(left_inset, 0, 0, 0),
                         Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._marks and not self._message and not self._looking:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        font = QFont()
        font.setPointSize(LABEL_FONT_PT)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetricsF(font)

        dpr = self.dpr
        origin = self.geometry().topLeft()
        # 0 to 1 and back: one slow breath, shared by every ring on screen.
        breath = 0.5 + 0.5 * math.sin(self._phase)
        appear = self._appear
        #: Everything already drawn for a mark, so the step counter can avoid
        #: it. A target low on the screen puts its callout exactly where the
        #: counter wants to sit.
        occupied: list[QRectF] = []

        for mark in self._marks:
            shape = mark.shape or choose_shape(mark.bbox_px, dpr)
            left, top, right, bottom = to_logical(mark.bbox_px, dpr)
            # Widget-local coordinates: the overlay's own origin may not be 0,0.
            base_pad = CIRCLE_PAD if shape is MarkShape.RING else FRAME_PAD
            pad = base_pad + PULSE_AMPLITUDE * breath
            # A new mark lands from slightly too big, which reads as it
            # settling onto the thing rather than blinking into existence.
            pad += 26.0 * (1.0 - appear)
            ring = QRectF(
                left - origin.x() - pad,
                top - origin.y() - pad,
                (right - left) + 2 * pad,
                (bottom - top) + 2 * pad,
            )

            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Outer glow first, then the mark itself on top of it.
            glow = QColor(RING_GLOW)
            glow.setAlphaF(0.30 * (0.45 + 0.55 * breath) * appear)
            stroke = QColor(RING_COLOUR)
            stroke.setAlphaF(appear)

            if shape is MarkShape.FRAME:
                radius = FRAME_RADIUS + pad * 0.5
                painter.setPen(QPen(glow, STROKE_WIDTH + 8))
                painter.drawRoundedRect(ring, radius, radius)
                painter.setPen(QPen(stroke, STROKE_WIDTH))
                painter.drawRoundedRect(ring, radius, radius)
            else:
                painter.setPen(QPen(glow, STROKE_WIDTH + 8))
                painter.drawEllipse(ring)
                painter.setPen(QPen(stroke, STROKE_WIDTH))
                painter.drawEllipse(ring)

            text = (mark.instruction or "").strip()
            if not text:
                continue
            box, below = self._label_rect(ring, text, metrics)
            anchor = QPointF(ring.center().x(),
                             ring.bottom() if below else ring.top())
            if not self._tail_reaches(box, anchor, below):
                self._draw_leader(painter, box, anchor, below, appear)
            self._draw_label(painter, box, text, LABEL_BG,
                             point_at=anchor, below=below, opacity=appear)
            occupied.append(box)
            occupied.append(ring)

        if self._marks:
            self._draw_step(painter, occupied)
            painter.setFont(font)

        if self._message:
            self._draw_label(painter, self._banner_rect(self._message, metrics),
                             self._message, LABEL_BG)

        if self._looking:
            # Smaller than the instruction: it is a reassurance, not a step.
            small = QFont()
            small.setPointSize(LOOKING_FONT_PT)
            painter.setFont(small)
            small_metrics = QFontMetricsF(small)
            box = self._indicator_rect(small_metrics)
            self._draw_label(painter, box, LOOKING_TEXT, LOOKING_BG,
                             left_inset=INDICATOR_DOT_SPACE)
            # A dot that breathes in time with the ring, so the two read as one
            # system: this is why the circle is there.
            painter.setPen(Qt.PenStyle.NoPen)
            dot = QColor(LOOKING_DOT)
            dot.setAlphaF(0.45 + 0.55 * breath)
            painter.setBrush(dot)
            centre = QPointF(box.left() + LABEL_PAD + 4, box.center().y())
            painter.drawEllipse(centre, 4.0, 4.0)

        painter.end()
