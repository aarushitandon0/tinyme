"""Primary-monitor screen capture (MASTERSPEC 5.1).

CLAUDE.md rule 6: screenshots stay in memory. Nothing here writes a file, and
nothing here should learn how to. Only ``eval/`` tooling, run on the
developer's own machine, is allowed to save images.

CLAUDE.md rule 9: what ``mss`` hands back is in *physical* pixels. It is never
converted here; conversion happens once, in ``elements.to_logical``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import mss
import numpy as np

from app.elements import Bbox, Monitor

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Capture:
    """One screenshot, in memory, plus the geometry it was taken from."""

    #: (height, width, 3) uint8 array in RGB order.
    image: np.ndarray
    #: Primary-monitor geometry in physical pixels.
    monitor: Monitor
    #: How long the grab took, for the Sentry span in MASTERSPEC 8.
    elapsed_ms: float

    @property
    def size_px(self) -> tuple[int, int]:
        """(width, height) of the captured image in physical pixels."""
        return self.image.shape[1], self.image.shape[0]


def _to_rgb(shot: mss.base.ScreenShot) -> np.ndarray:
    """Convert an mss BGRA buffer to a contiguous RGB array.

    The copy is deliberate: the raw buffer belongs to mss and is reused on the
    next grab, so slicing a view would alias the next screenshot.
    """
    bgra = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
    return np.ascontiguousarray(bgra[:, :, 2::-1])


def grab_primary() -> Capture:
    """Grab the primary monitor. In memory only.

    MASTERSPEC 2 locks scope to the primary monitor, so ``monitors[1]`` is used
    rather than ``monitors[0]`` (which is the whole virtual screen across every
    display).
    """
    started = time.perf_counter()
    with mss.mss() as sct:
        if len(sct.monitors) < 2:
            raise RuntimeError("mss reported no individual monitors")
        geometry = sct.monitors[1]
        shot = sct.grab(geometry)
        image = _to_rgb(shot)

    monitor = Monitor(
        left=int(geometry["left"]),
        top=int(geometry["top"]),
        width=int(geometry["width"]),
        height=int(geometry["height"]),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    log.debug(
        "captured %dx%d physical px at offset (%d, %d) in %.1f ms",
        monitor.width, monitor.height, monitor.left, monitor.top, elapsed_ms,
    )
    return Capture(image=image, monitor=monitor, elapsed_ms=elapsed_ms)


# --- window geometry -------------------------------------------------------
# These live here, next to monitor geometry, because they are physical-pixel
# screen layout read straight from win32. They are not UI Automation, so they
# do not belong in uia.py. Both are used to prioritise the element cap
# (MASTERSPEC 5.2) and later by watcher.py.


def foreground_rect() -> Bbox | None:
    """Physical-pixel (l, t, r, b) of the foreground window, or None."""
    try:
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return (float(left), float(top), float(right), float(bottom))
    except Exception:
        log.debug("could not read the foreground window rect", exc_info=True)
        return None


def screen_scale() -> float:
    """Physical pixels per logical pixel on the primary monitor (1.25 at 125%).

    Qt is the app's authority on this (``overlay.dpr``, CLAUDE.md rule 9) and
    callers that have an overlay should pass that instead. This exists for the
    code that has no Qt to ask: the booking flow on a worker thread, and the
    one-shot scripts.

    Measured rather than queried, because the obvious queries lie in this
    process. ``GetDpiForSystem`` and ``GetDeviceCaps(LOGPIXELSX)`` both return
    96 here -- Python is not marked DPI-aware, so Windows virtualises the
    answer and reports 100% on a 125% display. What it does *not* virtualise is
    ``mss``, which goes to the desktop surface and sees 1920x1080, while
    ``GetSystemMetrics`` sees the virtualised 1536x864. The ratio of those two
    is the real scale.

    Returns 1.0 if either measurement is unavailable, which is the safe
    direction: an un-scaled circle is in the right place on an un-scaled
    screen, and this is the only caller-visible failure.
    """
    try:
        import ctypes

        logical_w = int(ctypes.windll.user32.GetSystemMetrics(0))
        with mss.mss() as sct:
            physical_w = int(sct.monitors[1]["width"])
    except Exception:
        log.debug("could not measure the screen scale", exc_info=True)
        return 1.0

    if logical_w <= 0 or physical_w <= 0:
        return 1.0
    return physical_w / logical_w


def taskbar_rect() -> Bbox | None:
    """Physical-pixel (l, t, r, b) of the taskbar, or None.

    Found by window class ``Shell_TrayWnd``, never by title (CLAUDE.md rule 11
    makes the same point about Explorer).
    """
    try:
        import win32gui

        hwnd = win32gui.FindWindow("Shell_TrayWnd", None)
        if not hwnd:
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return (float(left), float(top), float(right), float(bottom))
    except Exception:
        log.debug("could not read the taskbar rect", exc_info=True)
        return None
