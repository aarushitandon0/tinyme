"""UI Automation elements: names for the icons OCR cannot read (MASTERSPEC 5.2).

Why this module exists at all: the File Explorer taskbar icon has no text, so
scene A's first step is impossible with OCR alone. UI Automation knows it is
called "File Explorer".

Installed API, checked before writing this (CLAUDE.md asks for exactly that):

    uiautomation 2.0.29
    ControlFromHandle(handle: int) -> Control | None
    WalkControl(control, includeTop=False, maxDepth=...) -> yields (Control, depth)
    Control.Name / .ControlTypeName / .IsEnabled / .IsOffscreen
    Control.BoundingRectangle -> Rect with .left .top .right .bottom
    InitializeUIAutomationInCurrentThread() == comtypes.CoInitializeEx()

Three rules shape the design:

* **Scope.** Only the taskbar (``Shell_TrayWnd``) and the foreground window are
  walked, depth-limited. A full desktop walk takes seconds.
* **A hard time budget** of :data:`BUDGET_MS`. The deadline is checked inside the
  walk, before every control is touched, so a long run of controls we would throw
  away cannot overrun it. We return what we have when it expires.
* **Never fatal.** If COM throws, the window is gone, or a property read fails,
  this returns no elements and the step continues on OCR only.

Coordinates come back in the same physical-pixel screen space as
``capture.taskbar_rect`` and ``mss`` (CLAUDE.md rule 9). Nothing here converts
to logical pixels; ``elements.to_logical`` is still the only place that does.

**Do not move ``import uiautomation`` to the top of this module.** Importing it
runs ``SetProcessDpiAwareness(PerMonitorDpiAware)`` at module level
(``uiautomation.py:2958``), which is process-wide and first-caller-wins. Since
``main.py`` does ``from app import uia``, a top-level import would set the
process DPI awareness before ``QApplication`` exists and change the
``devicePixelRatio()`` that ``elements.to_logical`` is calibrated against -
MASTERSPEC 14's "circle in the wrong place" risk, one tidy-up away. Keeping the
import inside the functions means Qt goes first. (It also defers COM startup,
which is the smaller reason.)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator, Sequence

from app.elements import Bbox, Element, Monitor, classify_region

log = logging.getLogger(__name__)

#: MASTERSPEC 5.2: hard time budget for the whole UIA pass.
BUDGET_MS = 300.0

#: Reserved at the end of the budget for the last root (the foreground window),
#: so an expensive taskbar cannot leave it with nothing. Measured: the taskbar
#: walk is the one that runs long (85-275 ms depending on load) and it is the
#: reason this module exists, so it gets everything except this reserve rather
#: than a fixed share. A loaded run that spends the lot on the taskbar still
#: beats one that circles nothing in scene A's first step.
FOREGROUND_RESERVE_MS = 80.0

#: Depth limits. The taskbar buries its pinned buttons about five levels down;
#: app windows go far deeper than is useful, and depth is the cheapest brake.
TASKBAR_MAX_DEPTH = 7
FOREGROUND_MAX_DEPTH = 6

#: Ceiling on controls pulled from the walk, before filtering. The element list
#: is capped at 80 later (``elements.cap_elements``); this just stops a huge
#: tree from spending the budget on things we would throw away.
MAX_CONTROLS = 300

#: Container roles she never clicks. Circling the whole taskbar pane or the
#: window frame would be worse than drawing nothing.
SKIP_ROLES = frozenset({
    "Pane", "Window", "TitleBar", "ScrollBar", "Separator", "Thumb",
    "ToolTip", "Group",
})

#: Taskbar names carry state suffixes: "File Explorer pinned",
#: "Microsoft Edge - 1 running window pinned".
_RUNNING_WINDOWS = re.compile(r"\s*-\s*\d+\s+running\s+windows?\s*$", re.IGNORECASE)
_PINNED = re.compile(r"\s+pinned$", re.IGNORECASE)

#: Icon fonts (Segoe Fluent Icons) put private-use codepoints in Name. They
#: render as a glyph she can see but nobody can read as text.
_PRIVATE_USE = re.compile(r"[-]")

_initialised = threading.local()


@dataclass(frozen=True)
class ControlInfo:
    """A snapshot of one UIA control, taken off the COM object immediately.

    Properties are read once, here. Holding a ``Control`` and reading it later
    means another cross-process COM call per access, and the object may be dead
    by then.
    """

    name: str
    role: str  # ControlTypeName as uiautomation reports it, e.g. "ButtonControl"
    bbox_px: Bbox
    enabled: bool
    offscreen: bool


def clean_name(raw: str) -> str:
    """Trim a UIA name down to what she would call the thing.

    Only the first line is kept: tray buttons pack a status report into Name
    ("Network PiyaliDen\\nInternet access"), and the first line is the name.
    """
    first_line = (raw or "").replace("\r", "\n").split("\n", 1)[0]
    collapsed = " ".join(first_line.split())
    collapsed = _PINNED.sub("", collapsed)
    collapsed = _RUNNING_WINDOWS.sub("", collapsed)
    return collapsed.strip(" -")


def is_useful_text(text: str) -> bool:
    """True if there is something readable left once icon glyphs are removed."""
    return bool(_PRIVATE_USE.sub("", text or "").strip())


def role_name(control_type_name: str) -> str:
    """``"ListItemControl"`` -> ``"ListItem"``, as MASTERSPEC 5.2 renders it."""
    name = (control_type_name or "").strip()
    if name.endswith("Control") and name != "Control":
        name = name[: -len("Control")]
    return name or "Unknown"


def _area(bbox: Bbox) -> float:
    left, top, right, bottom = bbox
    return max(0.0, right - left) * max(0.0, bottom - top)


def _on_monitor(bbox: Bbox, monitor: Monitor) -> bool:
    """True if the box overlaps the primary monitor (MASTERSPEC 2: that only)."""
    left, top, right, bottom = bbox
    return (left < monitor.right and right > monitor.left
            and top < monitor.bottom and bottom > monitor.top)


def _covers(outer: Bbox, inner: Bbox) -> bool:
    """True if ``inner`` sits inside ``outer`` (one pixel of slack each side)."""
    o_left, o_top, o_right, o_bottom = outer
    i_left, i_top, i_right, i_bottom = inner
    return (o_left <= i_left + 1 and o_top <= i_top + 1
            and o_right >= i_right - 1 and o_bottom >= i_bottom - 1)


def elements_from_infos(infos: Iterable[ControlInfo], monitor: Monitor) -> list[Element]:
    """Apply MASTERSPEC 5.2's keep rules and turn snapshots into Elements.

    Kept: named, on-screen, enabled, non-container, inside the primary monitor,
    with a real area. Dropped as well: a control whose cleaned name repeats its
    ancestor's inside the ancestor's box, which is how a taskbar Search button
    and its Text child arrive. The walk is pre-order, so the first of a pair is
    the outer, clickable one.

    Returns:
        Elements with ``id=0``; ``elements.number_elements`` numbers them, after
        the OCR merge.
    """
    out: list[Element] = []
    for item in infos:
        if not item.enabled or item.offscreen:
            continue
        role = role_name(item.role)
        if role in SKIP_ROLES:
            continue
        text = clean_name(item.name)
        if not is_useful_text(text):
            continue
        if _area(item.bbox_px) <= 0 or not _on_monitor(item.bbox_px, monitor):
            continue
        lowered = text.casefold()
        if any(kept.text.casefold() == lowered and _covers(kept.bbox_px, item.bbox_px)
               for kept in out):
            continue
        # A Text control inside something we already kept is a label on it, not
        # a separate thing to click: the taskbar clock and the weather widget
        # each contribute several, and they would hold slots in the 80-element
        # cap ahead of real foreground elements (cap_elements ranks the taskbar
        # first). The container we kept is the clickable thing.
        if role == "Text" and any(_covers(kept.bbox_px, item.bbox_px) for kept in out):
            continue
        out.append(
            Element(
                id=0,
                text=text,
                source="uia",
                role=role,
                region=classify_region(item.bbox_px, monitor),
                bbox_px=item.bbox_px,
            )
        )
    return out


def take_within_budget(
    infos: Iterable[ControlInfo],
    deadline: float,
    clock: Callable[[], float] = time.perf_counter,
    limit: int = MAX_CONTROLS,
    label: str = "walk",
) -> list[ControlInfo]:
    """Pull snapshots until the deadline passes or ``limit`` is reached.

    ``infos`` is expected to be lazy: the cost is in producing each item, so the
    clock is checked after every one and the generator is simply abandoned.
    MASTERSPEC 5.2 says return what you have when the budget expires, so a
    partial list is a success, not an error.

    This is the outer half of the budget; :func:`_walk` checks the same deadline
    on controls it discards, which never reach here. ``label`` names the root in
    the log, because "which walk ran out of time" is the question the bench notes
    need answered.
    """
    taken: list[ControlInfo] = []
    if clock() >= deadline:
        log.debug("UIA budget already spent before the %s walk", label)
        return taken
    for item in infos:
        taken.append(item)
        if len(taken) >= limit:
            log.debug("UIA control limit %d reached in the %s walk", limit, label)
            break
        if clock() >= deadline:
            log.info("UIA budget expired in the %s walk after %d controls",
                     label, len(taken))
            break
    return taken


# --- the COM side ----------------------------------------------------------
# Everything below talks to uiautomation. It is imported lazily (it starts COM
# on import) and is kept behind small functions so the logic above stays
# testable on any machine.


def _ensure_com() -> None:
    """Start COM once per thread.

    The read stage runs in a fresh QThread per step (MASTERSPEC 5.7, and
    ``main.TinyMe.start_step`` really does build a new one each time), and
    uiautomation only initialises COM on the thread that imports it.

    No matching ``CoUninitialize`` is made here, which leaks one apartment's
    bookkeeping per step. That is deliberate for now and measured as harmless:
    ``IUIAutomation`` is agile, the expensive client is built once per *process*
    (see notes/bench.md), and three successive fresh threads collected without
    error or a cost penalty. The tidy version is for the worker to uninitialise
    when its ``run`` returns, which belongs to main.py, not here.
    """
    if getattr(_initialised, "done", False):
        return
    import uiautomation as auto

    if threading.current_thread() is not threading.main_thread():
        auto.InitializeUIAutomationInCurrentThread()
    _initialised.done = True


def _snapshot(control) -> ControlInfo | None:
    """Read one control's properties, or None if it is of no use to us.

    Every property read is a cross-process COM call, so they are read in the
    order that discards the most for the fewest calls: ``Name`` first (roughly
    half the taskbar tree is unnamed ``ImageControl`` children, and MASTERSPEC
    5.2 keeps only named controls), then the control type (containers in
    :data:`SKIP_ROLES` are nearly all named, so without this they would pay for
    three more reads before being thrown away).

    Both of those are a *fast path* only. :func:`elements_from_infos` applies the
    same two rules authoritatively, where a test can reach them without COM; a
    filter that lived only in here would be invisible to the suite.

    Returns None for a control that is of no use and for one that died mid-walk.
    """
    try:
        name = clean_name(control.Name or "")
        if not is_useful_text(name):
            return None
        role = control.ControlTypeName or ""
        if role_name(role) in SKIP_ROLES:
            return None
        rect = control.BoundingRectangle
        return ControlInfo(
            name=name,
            role=role,
            bbox_px=(float(rect.left), float(rect.top),
                     float(rect.right), float(rect.bottom)),
            enabled=bool(control.IsEnabled),
            offscreen=bool(control.IsOffscreen),
        )
    except Exception:
        log.debug("skipped a control that could not be read", exc_info=True)
        return None


def _walk(
    hwnd: int,
    max_depth: int,
    deadline: float,
    clock: Callable[[], float] = time.perf_counter,
    label: str = "walk",
) -> Iterator[ControlInfo]:
    """Yield snapshots under a window handle, lazily, pre-order.

    The deadline is checked here, before each control is touched, and not only by
    the consumer: most controls are discarded by :func:`_snapshot` and never
    yielded, so a consumer-side check alone would let a long unnamed stretch -
    and the whole tail of the tree after the last named control - run past the
    budget unmeasured. That overrun is a property of whichever window happens to
    be in the foreground, which is exactly what a hard budget must not depend on.

    ``includeTop=False``: the root is the window itself, which is a container
    nobody clicks.
    """
    import uiautomation as auto

    root = auto.ControlFromHandle(int(hwnd))
    if root is None:
        return
    touched = 0
    for control, _depth in auto.WalkControl(root, includeTop=False, maxDepth=max_depth):
        if clock() >= deadline:
            # Say so: once the walk stops itself, the consumer's loop ends
            # normally and would otherwise log nothing, leaving "which walk ran
            # out of time" unanswerable from the logs.
            log.info("UIA %s walk hit its deadline after %d controls", label, touched)
            return
        touched += 1
        info = _snapshot(control)
        if info is not None:
            yield info
    log.debug("UIA %s walk finished inside its deadline, %d controls", label, touched)


def _roots() -> list[tuple[str, int, int]]:
    """(label, hwnd, max_depth) for the taskbar and the foreground window.

    The taskbar is found by class ``Shell_TrayWnd``, never by title (CLAUDE.md
    rule 11). A foreground window that *is* the taskbar is not walked twice.
    """
    import win32gui

    found: list[tuple[str, int, int]] = []
    tray = win32gui.FindWindow("Shell_TrayWnd", None)
    if tray:
        found.append(("taskbar", tray, TASKBAR_MAX_DEPTH))
    foreground = win32gui.GetForegroundWindow()
    if foreground and foreground != tray:
        found.append(("foreground", foreground, FOREGROUND_MAX_DEPTH))
    return found


def _collect_infos(
    budget_ms: float,
    clock: Callable[[], float] = time.perf_counter,
    roots: Sequence[tuple[str, int, int]] | None = None,
) -> list[ControlInfo]:
    """Walk the taskbar then the foreground window inside one shared budget.

    Every root but the last may use the budget except
    :data:`FOREGROUND_RESERVE_MS`; the last gets whatever is left. With one root
    it gets the lot.
    """
    _ensure_com()
    started = clock()
    hard_deadline = started + budget_ms / 1000.0
    if roots is None:
        roots = _roots()
    if not roots:
        log.debug("no taskbar or foreground window to walk")
        return []

    infos: list[ControlInfo] = []
    for index, (label, hwnd, max_depth) in enumerate(roots):
        last = index == len(roots) - 1
        deadline = hard_deadline if last else max(
            started, hard_deadline - FOREGROUND_RESERVE_MS / 1000.0
        )
        limit = MAX_CONTROLS - len(infos)
        if limit <= 0:
            break
        taken = take_within_budget(
            _walk(hwnd, max_depth, deadline, clock, label),
            deadline, clock, limit, label,
        )
        log.debug("UIA %s walk: %d controls", label, len(taken))
        infos.extend(taken)
    return infos


def collect(monitor: Monitor, budget_ms: float = BUDGET_MS) -> tuple[list[Element], float]:
    """Collect UIA elements for this step.

    Returns:
        (elements, uia_ms). On any failure this returns an empty list rather
        than raising: without UIA she loses the icon names, but with an
        exception she loses the whole task (MASTERSPEC 5.2's fallback).
    """
    started = time.perf_counter()
    try:
        infos = _collect_infos(budget_ms)
        elements = elements_from_infos(infos, monitor)
    except Exception:
        uia_ms = (time.perf_counter() - started) * 1000.0
        log.exception("UIA failed after %.0f ms; continuing with OCR only", uia_ms)
        return [], uia_ms
    uia_ms = (time.perf_counter() - started) * 1000.0
    log.debug("UIA produced %d elements in %.0f ms", len(elements), uia_ms)
    return elements, uia_ms


def warm_up() -> None:
    """Pay COM's first-call cost at startup instead of inside her first step.

    Measured on the dev laptop: the first ``ControlFromHandle`` in a process
    costs 230-490 ms, which alone would blow the 300 ms budget on step one.

    **Only the taskbar is walked**, never the foreground window. This runs at
    launch, before she has pressed the hotkey and with no "Tiny Me is looking"
    indicator on screen, and MASTERSPEC 6 promises she is read **only** after the
    hotkey or during a task. Enumerating the controls of whatever she had open
    would break that promise in spirit even though no screenshot is taken. The
    taskbar holds nothing of hers, and it warms the expensive parts - the
    automation client and the first ``ControlFromHandle`` - just as well.

    Expect this to log ``UIA taskbar walk hit its deadline after 0 controls``:
    the cold ``ControlFromHandle`` can cost the whole 300 ms on its own, so the
    warm-up often collects nothing. That is the point - the cost has been paid,
    and the next ``collect`` is warm.
    """
    started = time.perf_counter()
    try:
        import win32gui

        tray = win32gui.FindWindow("Shell_TrayWnd", None)
        if not tray:
            log.debug("no taskbar to warm up against")
            return
        _collect_infos(
            budget_ms=BUDGET_MS,
            roots=[("taskbar", tray, TASKBAR_MAX_DEPTH)],
        )
    except Exception:
        log.debug("UIA warm-up failed; the first step will fall back to OCR",
                  exc_info=True)
        return
    log.info("UIA warmed up in %.0f ms", (time.perf_counter() - started) * 1000.0)
