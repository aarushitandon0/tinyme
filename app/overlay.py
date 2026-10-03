"""The circle she actually sees (MASTERSPEC 5.6).

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
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

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
RING_COLOUR = QColor(255, 92, 0)
LABEL_BG = QColor(20, 20, 20, 235)
LABEL_FG = QColor(255, 255, 255)
LABEL_FONT_PT = 15
LABEL_PAD = 10
LABEL_GAP = 14
SCREEN_MARGIN = 8
#: Where a circle-less message sits, in logical pixels from the top.
BANNER_TOP = 48

#: MASTERSPEC 6's promise, in the words she reads.
LOOKING_TEXT = "Tiny Me is looking"
LOOKING_BG = QColor(255, 92, 0, 225)
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


@dataclass(frozen=True)
class Mark:
    """One circle and the words that go with it."""

    bbox_px: Bbox
    instruction: str = ""


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

    def show_marks(self, marks: list[Mark]) -> None:
        """Replace what is drawn and make sure the overlay is visible."""
        self._marks = list(marks)
        if not self.isVisible():
            self.show()
        self._apply_capture_exclusion()
        self.raise_()
        self.update()

    def show_circle(self, bbox_px: Bbox, instruction: str = "") -> None:
        """Circle one thing. This is what the task loop uses."""
        self._message = ""
        self.show_marks([Mark(bbox_px=bbox_px, instruction=instruction)])

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
        self.update()

    @property
    def looking(self) -> bool:
        return self._looking

    def clear(self) -> None:
        self._marks = []
        self._message = ""
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
            )
        ])
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

    def _label_rect(self, ring: QRectF, text: str, metrics: QFontMetricsF) -> QRectF:
        """Place the label near the ring but always fully on screen."""
        width = metrics.horizontalAdvance(text) + 2 * LABEL_PAD
        height = metrics.height() + 2 * LABEL_PAD
        # Prefer below the ring; flip above when there is no room.
        top = ring.bottom() + LABEL_GAP
        if top + height > self.height() - SCREEN_MARGIN:
            top = ring.top() - LABEL_GAP - height
        left = ring.center().x() - width / 2

        left = max(SCREEN_MARGIN, min(left, self.width() - width - SCREEN_MARGIN))
        top = max(SCREEN_MARGIN, min(top, self.height() - height - SCREEN_MARGIN))
        return QRectF(left, top, width, height)

    def _banner_rect(self, text: str, metrics: QFontMetricsF) -> QRectF:
        """A wide label across the top-centre, for words with no circle."""
        width = metrics.horizontalAdvance(text) + 2 * LABEL_PAD
        height = metrics.height() + 2 * LABEL_PAD
        left = max(SCREEN_MARGIN, (self.width() - width) / 2)
        return QRectF(left, BANNER_TOP, width, height)

    def _indicator_rect(self, metrics: QFontMetricsF) -> QRectF:
        """Top-right corner, out of the way of almost every app's toolbar."""
        width = metrics.horizontalAdvance(LOOKING_TEXT) + 2 * LABEL_PAD
        height = metrics.height() + 2 * LABEL_PAD
        return QRectF(self.width() - width - SCREEN_MARGIN, SCREEN_MARGIN,
                      width, height)

    def _draw_label(self, painter: QPainter, box: QRectF, text: str,
                    background: QColor) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(background)
        painter.drawRoundedRect(box, 8, 8)
        painter.setPen(QPen(LABEL_FG))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if not self._marks and not self._message and not self._looking:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        font = QFont()
        font.setPointSize(LABEL_FONT_PT)
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetricsF(font)

        dpr = self.dpr
        origin = self.geometry().topLeft()

        for mark in self._marks:
            left, top, right, bottom = to_logical(mark.bbox_px, dpr)
            # Widget-local coordinates: the overlay's own origin may not be 0,0.
            ring = QRectF(
                left - origin.x() - CIRCLE_PAD,
                top - origin.y() - CIRCLE_PAD,
                (right - left) + 2 * CIRCLE_PAD,
                (bottom - top) + 2 * CIRCLE_PAD,
            )
            painter.setPen(QPen(RING_COLOUR, STROKE_WIDTH))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(ring)

            text = (mark.instruction or "").strip()
            if not text:
                continue
            self._draw_label(painter, self._label_rect(ring, text, metrics),
                             text, LABEL_BG)

        if self._message:
            self._draw_label(painter, self._banner_rect(self._message, metrics),
                             self._message, LABEL_BG)

        if self._looking:
            # Smaller than the instruction: it is a reassurance, not a step.
            small = QFont()
            small.setPointSize(LOOKING_FONT_PT)
            painter.setFont(small)
            small_metrics = QFontMetricsF(small)
            self._draw_label(painter, self._indicator_rect(small_metrics),
                             LOOKING_TEXT, LOOKING_BG)

        painter.end()
