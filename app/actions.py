"""The two things Tiny Me is allowed to do *for* her (MASTERSPEC 3, 4).

Everything else in this app points at the screen and waits. This module is the
exception, so it is the one place where CLAUDE.md rule 3 ("the guard runs before
every automated action") has teeth. Two actions live here, and they are the only
two the permission table in MASTERSPEC 4 allows:

* **Find it for me** -- resolve the real Downloads folder, pick the file she
  means, and open Explorer with it selected. Read-only: nothing here deletes,
  moves, renames or writes. There is no code in this module that could.
* **Book a ticket** (:mod:`app.actions` booking flow, P9) -- drive a *headed*
  browser on the local demo site, fill the route and date, and hand over at the
  password, the OTP and the card. See :class:`BookingFlow`.

Why the Known Folder API and not ``Path.home() / "Downloads"``
--------------------------------------------------------------
Hers is not there. A relocated Downloads folder (moved to D:, or redirected by
OneDrive) is normal on a laptop that has run out of space once, and
``~/Downloads`` would silently search an empty directory and report "I could
not find it" about a file that exists. ``SHGetKnownFolderPath`` asks Windows
where the folder actually is, which is the only answer that is right on her
machine as well as mine.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from app import capture as capture_mod
from app import guard as guard_mod
from app.elements import Bbox
from app.guard import TaskType

log = logging.getLogger(__name__)

# --- the Downloads folder --------------------------------------------------

#: FOLDERID_Downloads. The GUID is the API's name for the folder; the path
#: behind it is whatever she (or OneDrive) moved it to.
FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"

#: SHGetKnownFolderPath flags: 0 means "the current path, do not create it".
_KF_FLAG_DEFAULT = 0


def known_folder_path(folder_id: str = FOLDERID_DOWNLOADS) -> Path | None:
    """Ask Windows where a known folder is. None if the call fails.

    ``ctypes`` rather than ``win32com.shell``: ``shellcon`` does not carry
    ``FOLDERID_Downloads`` on every pywin32 build, and this is three calls
    against a stable API with no extra dependency.
    """
    try:
        ole32 = ctypes.oledll.ole32
        shell32 = ctypes.oledll.shell32
    except (AttributeError, OSError):  # not Windows
        log.info("no shell32/ole32 here; cannot resolve known folders")
        return None

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_byte * 8),
        ]

    guid = _GUID()
    out = ctypes.c_wchar_p()
    try:
        ole32.CLSIDFromString(ctypes.create_unicode_buffer(folder_id),
                              ctypes.byref(guid))
        shell32.SHGetKnownFolderPath(ctypes.byref(guid), _KF_FLAG_DEFAULT,
                                     None, ctypes.byref(out))
    except OSError:
        log.exception("SHGetKnownFolderPath failed for %s", folder_id)
        return None

    try:
        path = out.value
    finally:
        # The shell allocated that string; we own freeing it.
        ctypes.windll.ole32.CoTaskMemFree(out)

    return Path(path) if path else None


def downloads_dir() -> Path:
    """Where her downloads really are.

    Falls back to ``%USERPROFILE%\\Downloads`` only when the API is unavailable
    (so the tests and a non-Windows checkout still work), and says so in the
    log rather than pretending the answer came from Windows.
    """
    resolved = known_folder_path(FOLDERID_DOWNLOADS)
    if resolved is not None:
        return resolved
    fallback = Path(os.environ.get("USERPROFILE", Path.home())) / "Downloads"
    log.warning("falling back to %s; the Known Folder API did not answer",
                fallback.name)
    return fallback


# --- picking the file she means --------------------------------------------

#: Words that are in every version of "find the file I just downloaded" and
#: carry no information about *which* file. Stripped before matching, so what
#: is left is either a real name fragment or nothing at all.
_FILLER = frozenset("""
a an and at can cant cannot could do download downloaded downloads file find
folder for
get give help her here i in is it its just last latest me mine my need new
newest of on one open please recent recently see show something that the them
there this to want was where which with you your
""".split())

#: rapidfuzz WRatio, 0-100, below which a name match is not worth trusting
#: over "the newest file". Set high on purpose: circling the wrong file is
#: worse than saying "here is the newest one", which she can see is wrong.
NAME_MATCH_RATIO = 72.0

#: Names we never offer. Not a safety rule -- just noise she did not download.
_IGNORED_NAMES = frozenset({"desktop.ini", "thumbs.db"})

_TOKENS = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class FileHit:
    """The file we are going to point at, and why we picked it."""

    path: Path
    #: ``"name"`` when her words matched the filename, ``"newest"`` when we
    #: fell back to the most recent download. Shown to her, so she can tell
    #: the difference between an answer and a guess.
    because: str
    score: float = 0.0

    @property
    def name(self) -> str:
        return self.path.name


def name_hints(goal: str) -> list[str]:
    """The words in her goal that could be part of a filename.

    "I can't find the file I downloaded" leaves nothing, which is the signal to
    use the newest file. "where is the electricity bill pdf" leaves
    ``["electricity", "bill", "pdf"]``.
    """
    tokens = [token for token in _TOKENS.split((goal or "").casefold()) if token]
    return [token for token in tokens if token not in _FILLER and len(token) > 1]


def _candidates(folder: Path) -> list[Path]:
    """Ordinary files in one folder, newest first. Never recursive.

    Not recursive on purpose: Downloads has an "old stuff" folder in it on most
    laptops, and reaching into it turns a 3-second answer into a disk crawl.
    """
    try:
        entries = list(folder.iterdir())
    except OSError:
        log.exception("cannot read %s", folder.name)
        return []

    files = []
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.name.casefold() in _IGNORED_NAMES or entry.name.startswith("."):
                continue
            # Partial downloads: pointing at one is pointing at nothing.
            if entry.suffix.casefold() in {".crdownload", ".part", ".tmp"}:
                continue
            files.append((entry.stat().st_mtime, entry))
        except OSError:
            continue

    files.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in files]


def _best_by_name(hints: Sequence[str], files: Iterable[Path]) -> tuple[Path | None, float]:
    """Best filename match for these hint words, and its score."""
    from rapidfuzz import fuzz

    needle = " ".join(hints)
    best: Path | None = None
    best_score = 0.0
    for path in files:
        haystack = _TOKENS.sub(" ", path.name.casefold()).strip()
        score = float(fuzz.WRatio(needle, haystack))
        if score > best_score:
            best, best_score = path, score
    return best, best_score


def find_file(goal: str, folder: Path | None = None) -> FileHit | None:
    """Pick the file her goal is about, or the newest one (MASTERSPEC 3A).

    Returns None only when the folder has no ordinary files in it at all.
    """
    folder = folder if folder is not None else downloads_dir()
    files = _candidates(folder)
    if not files:
        log.info("nothing to point at in the downloads folder")
        return None

    hints = name_hints(goal)
    if hints:
        match, score = _best_by_name(hints, files)
        if match is not None and score >= NAME_MATCH_RATIO:
            log.info("matched her words to a filename (score %.0f)", score)
            return FileHit(path=match, because="name", score=score)
        log.info("no filename matched her words well enough (best %.0f); "
                 "using the newest file", score)

    return FileHit(path=files[0], because="newest")


# --- opening Explorer with it selected -------------------------------------


def _select_command(path: Path) -> str:
    """The exact command line for ``explorer /select``.

    Built as a **string**, not an argument list, and this is the whole reason
    this function exists. ``explorer.exe`` wants one token of the form
    ``/select,"C:\\path\\file.pdf"``: the comma is part of the switch and the
    quotes go around the path only. Handing ``subprocess`` a list makes
    ``list2cmdline`` quote the whole argument -- ``"/select,C:\\My Files\\x.pdf"``
    -- and Explorer then opens Documents and selects nothing, which looks
    exactly like "Tiny Me is broken" rather than "the quoting is wrong".
    """
    return f'explorer /select,"{path}"'


def reveal_in_explorer(path: Path) -> bool:
    """Open Explorer with ``path`` highlighted. Read-only (MASTERSPEC 4).

    Returns False without launching anything if the file is not there, so a
    stale hit cannot open an empty window.

    Explorer's exit code is famously unreliable -- it returns 1 on success when
    it hands the request to an already-running instance -- so the return value
    here means "we launched it", not "she can see it". The watcher's own
    ``window_title_contains`` check is what confirms the folder actually opened.
    """
    if not guard_mod.can_do_it(TaskType.FIND_FILE):
        # Unreachable through the UI today; here so that changing the
        # permission table in guard.py changes the behaviour of this function
        # (CLAUDE.md rule 4) rather than leaving a second copy of the policy.
        log.info("not allowed to open Explorer for her")
        return False

    try:
        if not path.is_file():
            log.info("asked to reveal something that is not a file")
            return False
    except OSError:
        log.exception("cannot stat the file to reveal")
        return False

    command = _select_command(path)
    try:
        subprocess.Popen(command, shell=False)
    except OSError:
        log.exception("could not start Explorer")
        return False
    log.info("opened Explorer with one file selected")
    return True


@dataclass(frozen=True)
class FindResult:
    """What "Find it for me" did, in words she can read."""

    hit: FileHit | None
    opened: bool
    message: str


#: What she sees when the folder is empty. Her Downloads is never empty, so in
#: practice this means the Known Folder lookup pointed somewhere unexpected.
NOTHING_FOUND = "I could not find anything in your Downloads folder."


def find_for_her(goal: str, folder: Path | None = None) -> FindResult:
    """The whole "Find it for me" action: locate, open, and say what happened.

    Runs on the worker thread (CLAUDE.md rule 8) and touches no widget; the
    caller puts :attr:`FindResult.message` in front of her.
    """
    hit = find_file(goal, folder)
    if hit is None:
        return FindResult(hit=None, opened=False, message=NOTHING_FOUND)

    opened = reveal_in_explorer(hit.path)
    if not opened:
        return FindResult(
            hit=hit, opened=False,
            message=f"I found “{hit.name}” but could not open the folder.",
        )
    if hit.because == "newest":
        return FindResult(hit=hit, opened=True,
                          message=f"This is your newest download: “{hit.name}”.")
    return FindResult(hit=hit, opened=True,
                      message=f"Here it is: “{hit.name}”.")


# ===========================================================================
# Booking: fill the easy part, hand over the rest (MASTERSPEC 3B, P9)
# ===========================================================================
#
# The browser is the one place where Tiny Me types for her, and so the one
# place where the guard has to be better than a keyword list. It is: in a page
# we have the DOM, and the DOM says what a field *is*
# (``guard.sensitive_dom_field``).
#
# Two independent refusals, because one of them will eventually be bypassed by
# a page nobody anticipated:
#
# 1. :meth:`BookingFlow._clear_or_hand_over` runs before **every** action and
#    stops the flow dead while any sensitive field is visible anywhere on the
#    page -- a password box three fields below the one being filled still stops
#    the typing above it.
# 2. :meth:`BookingFlow._fill` re-checks the single field it is about to touch
#    and refuses it by name.
#
# Rule 1 is the promise. Rule 2 is what makes the promise survive someone
# adding a new step to the flow and forgetting rule 1.
#
# The browser is **headed**, always, and that is a feature rather than an
# oversight: MASTERSPEC 3B says she watches. A headless booking flow would be
# faster to run and impossible to trust.

#: Where the demo site lives when it is served by ``python -m http.server``
#: from the repo root (MASTERSPEC 3B).
DEMO_SITE_URL = "http://localhost:8000/demo_site/index.html"

#: How long to wait for a page to settle before reading its fields. A local
#: static site answers in milliseconds; this is the "something is wrong"
#: ceiling, not an expected wait.
PAGE_TIMEOUT_MS = 10_000

#: How often to look at the page while she is typing her own password. She is
#: reading a code off her phone, not racing us, and each poll is one DOM read.
HANDOVER_POLL_MS = 500

#: How long we wait at a handover before giving up and stopping the flow. Long
#: enough to find a phone, go and get it, and type six digits.
HANDOVER_TIMEOUT_S = 300.0

#: The first train on the results page is what "Do it for me" picks
#: (MASTERSPEC 3B). Not the cheapest, not the fastest: the first, because that
#: is the one she can see we picked.
FIRST_TRAIN_SELECTOR = "a.button, tr.train a"

#: Field name on the search page -> the value we have for it. Only these three
#: are ever typed; anything else on the page is hers.
_SEARCH_FIELDS = ("from", "to", "date")


@dataclass(frozen=True)
class BookingRequest:
    """What we understood from her words. Any part may be missing."""

    origin: str = ""
    destination: str = ""
    #: ISO ``YYYY-MM-DD``, because that is what ``input[type=date]`` accepts.
    date: str = ""

    @property
    def has_route(self) -> bool:
        return bool(self.origin and self.destination)


#: "book a ticket from Pune to Mumbai on 12 October" and the ways she actually
#: writes it. Kept to the two orderings that occur in practice rather than a
#: grammar: anything this misses is simply left blank, and a blank field is a
#: field she fills herself -- which is the normal state of this app, not a
#: failure.
_ROUTE_PATTERNS = (
    re.compile(r"\bfrom\s+(?P<origin>.+?)\s+to\s+(?P<destination>.+?)"
               r"(?=\s+(?:on|for|at|tomorrow|today)\b|[.,;]|$)", re.I),
    re.compile(r"\b(?:ticket|train|travel|go|going)\s+to\s+(?P<destination>.+?)"
               r"(?=\s+(?:on|for|at|from|tomorrow|today)\b|[.,;]|$)", re.I),
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

#: "12 October", "12th October", "12th of October" -- the "of" is optional
#: because she types it about half the time.
_DAY_MONTH = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>[a-z]+)\b", re.I)
_MONTH_DAY = re.compile(
    r"\b(?P<month>[a-z]+)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_ISO_DATE = re.compile(r"\b(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_DMY_DATE = re.compile(r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>\d{4})\b")


def parse_date(goal: str, today: date | None = None) -> str:
    """Pull a travel date out of her words as ``YYYY-MM-DD``, or ``""``.

    Handles the forms she types: "12 October", "October 12", "2026-10-12",
    "12/10/2026", "tomorrow", "today". A bare day-month with no year is read as
    the *next* such date, because nobody books a train into the past -- so "on
    3 January" typed in October means next January.
    """
    goal = (goal or "").casefold()
    today = today or date.today()

    if "tomorrow" in goal:
        return (today + timedelta(days=1)).isoformat()
    if "today" in goal or "tonight" in goal:
        return today.isoformat()

    iso = _ISO_DATE.search(goal)
    if iso:
        return _as_date(int(iso["year"]), int(iso["month"]), int(iso["day"]))

    dmy = _DMY_DATE.search(goal)
    if dmy:
        # Day first: she writes dates the Indian/British way, and the demo site
        # is not a US site. Documented rather than guessed at.
        return _as_date(int(dmy["year"]), int(dmy["month"]), int(dmy["day"]))

    for pattern in (_DAY_MONTH, _MONTH_DAY):
        match = pattern.search(goal)
        if not match:
            continue
        month = _MONTHS.get(match["month"].casefold())
        if month is None:
            continue
        day = int(match["day"])
        for year in (today.year, today.year + 1):
            resolved = _as_date(year, month, day)
            if resolved and resolved >= today.isoformat():
                return resolved
        return ""

    return ""


def _as_date(year: int, month: int, day: int) -> str:
    """ISO string, or ``""`` if that is not a real date (31 February)."""
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _clean_place(text: str) -> str:
    """Tidy a captured place name without changing what she typed."""
    text = re.sub(r"\s+", " ", text).strip(" .,;:-")
    # Drop a leading article so "from the airport to Pune" gives "airport".
    text = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.I)
    return text[:60]


def parse_booking(goal: str, today: date | None = None) -> BookingRequest:
    """Read a route and a date out of her goal (MASTERSPEC 3B).

    Anything not understood is left empty and simply not typed. That is the
    honest failure here: an empty box she fills herself, rather than a guess
    typed into a form she is about to pay from.
    """
    goal = (goal or "").strip()
    travel_date = parse_date(goal, today)

    for pattern in _ROUTE_PATTERNS:
        match = pattern.search(goal)
        if not match:
            continue
        groups = match.groupdict()
        request = BookingRequest(
            origin=_clean_place(groups.get("origin") or ""),
            destination=_clean_place(groups.get("destination") or ""),
            date=travel_date,
        )
        if request.destination:
            return request

    return BookingRequest(date=travel_date)


# --- reading the page ------------------------------------------------------

#: One DOM read, done in the page rather than over the wire: a field at a time
#: would be a round trip each, and the guard has to see the *whole* page before
#: the first keystroke.
#:
#: Visibility is decided here, where the browser knows it: ``offsetParent`` is
#: null for a hidden field, and a zero-sized rectangle catches the rest. The
#: label is looked up three ways because pages use all three.
_READ_FIELDS_JS = """
() => {
  const describe = (el, index) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    let label = "";
    if (el.id) {
      const tag = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (tag) { label = tag.innerText || ""; }
    }
    if (!label) {
      const wrapping = el.closest("label");
      if (wrapping) { label = wrapping.innerText || ""; }
    }
    if (!label) { label = el.getAttribute("aria-label") || ""; }
    return {
      index: index,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute("type") || "text").toLowerCase(),
      autocomplete: (el.getAttribute("autocomplete") || "").toLowerCase(),
      name: el.getAttribute("name") || "",
      id: el.id || "",
      placeholder: el.getAttribute("placeholder") || "",
      label: label.trim().slice(0, 120),
      visible: !!el.offsetParent && style.visibility !== "hidden"
               && rect.width > 1 && rect.height > 1,
      rect: [rect.left, rect.top, rect.width, rect.height]
    };
  };
  const nodes = document.querySelectorAll("input, textarea, select");
  return Array.from(nodes).map(describe);
}
"""

#: Where the viewport sits on the screen, in the browser's own logical pixels.
#: Turning that into the physical-pixel bbox the overlay draws needs the
#: *screen's* scale, which comes from outside the page -- see
#: :func:`field_bbox_px`.
#:
#: ``outerHeight - innerHeight`` is the browser's own chrome -- tab strip,
#: address bar -- and is the only way to find it from inside the page. It is an
#: approximation: it attributes all the difference to the top, which is right
#: for Chromium with no bottom bar and no sidebar, and is why the circle is
#: drawn with 20 px of padding around it.
_VIEWPORT_JS = """
() => ({
  screenX: window.screenX,
  screenY: window.screenY,
  innerWidth: window.innerWidth,
  innerHeight: window.innerHeight,
  outerWidth: window.outerWidth,
  outerHeight: window.outerHeight,
  dpr: window.devicePixelRatio || 1
})
"""


def read_fields(page: Any) -> list[guard_mod.DomField]:
    """Describe every input on the page, in document order.

    One ``evaluate`` call. The result is plain data, which is what lets
    ``guard.sensitive_dom_field`` be a pure function and what lets the tests
    check the rule without a browser.
    """
    try:
        raw = page.evaluate(_READ_FIELDS_JS)
    except Exception:
        log.exception("could not read the page's fields")
        # A page we cannot read is a page we must not type into.
        raise

    fields = []
    for item in raw or []:
        rect = tuple(float(value) for value in (item.get("rect") or (0, 0, 0, 0)))
        fields.append(guard_mod.DomField(
            tag=str(item.get("tag", "input")),
            type=str(item.get("type", "text")),
            autocomplete=str(item.get("autocomplete", "")),
            name=str(item.get("name", "")),
            id=str(item.get("id", "")),
            placeholder=str(item.get("placeholder", "")),
            label=str(item.get("label", "")),
            visible=bool(item.get("visible")),
            rect=rect,  # type: ignore[arg-type]
            selector=f'input[name="{item.get("name")}"]' if item.get("name") else "",
        ))
    return fields


def field_bbox_px(
    page: Any,
    field: guard_mod.DomField,
    screen_scale: float | None = None,
) -> Bbox | None:
    """Where this field is on the *screen*, in physical pixels.

    ``screen_scale`` is physical pixels per logical pixel (1.25 at 125%
    Windows scaling). The caller passes the overlay's ``dpr`` so the circle is
    drawn in the same coordinate system it was measured in; left out, it is
    measured with ``capture.screen_scale``.

    **It is deliberately not ``window.devicePixelRatio``**, and that cost an
    hour: Playwright launches Chromium with ``deviceScaleFactor`` pinned to 1,
    so the page reports ``devicePixelRatio = 1`` on a 125% display while
    Windows scales the window up anyway. Using the page's own number put the
    circle one field too high -- neatly around the *username* box on
    demo_site/login.html, which is exactly the kind of wrong that looks right
    in a screenshot. The scale that matters belongs to the screen, so it is
    asked of the screen.

    At 100% browser zoom a CSS pixel is a logical pixel, which is what makes
    this arithmetic hold; the flow opens its own browser, so the zoom is 100%.

    Returns None when the viewport cannot be located or the field has no size,
    in which case the caller hands over with words and no circle -- the right
    failure, since a circle in the wrong place on a password box is worse than
    no circle at all.
    """
    try:
        view = page.evaluate(_VIEWPORT_JS)
    except Exception:
        log.exception("could not locate the browser window on screen")
        return None
    if not view:
        return None

    left, top, width, height = field.rect
    if width <= 0 or height <= 0:
        return None

    dpr = float(screen_scale if screen_scale is not None
                else capture_mod.screen_scale())
    # The chrome above the viewport: everything outerHeight has that
    # innerHeight does not.
    chrome_h = max(0.0, float(view.get("outerHeight", 0)) - float(view.get("innerHeight", 0)))
    origin_x = float(view.get("screenX", 0))
    origin_y = float(view.get("screenY", 0)) + chrome_h

    return (
        (origin_x + left) * dpr,
        (origin_y + top) * dpr,
        (origin_x + left + width) * dpr,
        (origin_y + top + height) * dpr,
    )


# --- the flow --------------------------------------------------------------


class HandoverRefused(Exception):
    """She did not take over in time, so the flow stops rather than waits on.

    Not an error in the usual sense: she walked away from a half-filled form,
    which is hers to come back to. Tiny Me's part is to stop touching it.
    """


@dataclass
class BookingOutcome:
    """What the booking flow managed to do, and where it stopped."""

    #: Values actually typed into the search form, by field name.
    filled: dict[str, str] = field(default_factory=dict)
    #: True once a train has been chosen on the results page.
    chose_train: bool = False
    #: Guard reasons, in order, for every handover during the run.
    handovers: list[str] = field(default_factory=list)
    #: Where it ended: ``"handed_over"``, ``"finished"``, ``"stopped"``,
    #: ``"failed"``.
    ended: str = ""
    message: str = ""


class BookingFlow:
    """Drives a headed Chromium through the demo booking site (MASTERSPEC 3B).

    Runs on the worker thread, so it never touches a widget (CLAUDE.md rule 8).
    The overlay is reached through two callbacks the caller wires to Qt signals:

    * ``on_handover(bbox_px, message)`` -- circle this field and say this.
    * ``on_resume()`` -- she has moved on; take the circle down.

    ``is_stopped`` is checked between every action so Ctrl+Alt+P ends the flow
    promptly rather than at the end of the page.
    """

    def __init__(
        self,
        on_handover: Callable[[Bbox | None, str], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        is_stopped: Callable[[], bool] | None = None,
        url: str = DEMO_SITE_URL,
        handover_timeout_s: float = HANDOVER_TIMEOUT_S,
        poll_ms: int = HANDOVER_POLL_MS,
        sleep: Callable[[float], None] = time.sleep,
        screen_scale: float | None = None,
    ) -> None:
        self._on_handover = on_handover or (lambda bbox, message: None)
        self._on_resume = on_resume or (lambda: None)
        self._is_stopped = is_stopped or (lambda: False)
        self._url = url
        self._handover_timeout_s = handover_timeout_s
        self._poll_ms = poll_ms
        self._sleep = sleep
        #: Physical pixels per logical pixel, for turning a field's place in
        #: the page into a circle on her screen. Measured once per flow rather
        #: than per handover: she is not going to change her display scaling
        #: halfway through a booking.
        self._screen_scale = (screen_scale if screen_scale is not None
                              else capture_mod.screen_scale())
        log.info("screen scale for circles: %.2f", self._screen_scale)
        self.outcome = BookingOutcome()

    # --- the guard, between every action ---------------------------------

    def _clear_or_hand_over(self, page: Any) -> None:
        """Nothing happens on this page until it has no field of hers on it.

        This is the function CLAUDE.md rule 5 lives in. It runs before every
        navigation, every fill and every click -- not once per page -- because a
        page can grow a password box without navigating anywhere.
        """
        while True:
            if self._is_stopped():
                raise HandoverRefused("stopped")

            found = guard_mod.first_sensitive_field(read_fields(page))
            if found is None:
                return

            field_, reason = found
            self._wait_while_hers(page, field_, reason)

    def _wait_while_hers(self, page: Any, field_: guard_mod.DomField, reason: str) -> None:
        """Circle her field, say so, and wait until the page has moved on.

        "Moved on" means **that field is no longer on the page** -- she signed
        in and the browser navigated. Two weaker definitions were tried and are
        both wrong:

        * *She typed something* (the box is no longer empty) does not end the
          handover: the password box is still sitting there, still hers, so the
          guard finds it again on the next pass and we hand over to her in a
          loop. It also means asking the page about her field's contents, which
          this flow has no business doing.
        * *The URL changed* misses a single-page checkout that swaps the form
          out without navigating.

        Waiting for the field to go also leaves the submit button to her, which
        is right: pressing "Sign in" is part of signing in, and MASTERSPEC 4
        does not give us that.
        """
        message = guard_mod.handover_message(reason)
        bbox = field_bbox_px(page, field_, self._screen_scale)
        self.outcome.handovers.append(reason)
        log.info("handing over to her (%s); waiting up to %.0f s",
                 reason, self._handover_timeout_s)
        self._on_handover(bbox, message)

        waited = 0.0
        interval = self._poll_ms / 1000.0
        while waited < self._handover_timeout_s:
            if self._is_stopped():
                self._on_resume()
                raise HandoverRefused("stopped")
            self._sleep(interval)
            waited += interval
            if not self._field_still_hers(page, field_):
                log.info("she has moved on; carrying on")
                self._on_resume()
                return

        self._on_resume()
        raise HandoverRefused(reason)

    def _field_still_hers(self, page: Any, field_: guard_mod.DomField) -> bool:
        """Is that same field still visible on the page?

        Nothing here asks what she typed. The only question is whether her box
        is still there, which the page answers without her ever being quoted.
        """
        try:
            fields = read_fields(page)
        except Exception:
            # Mid-navigation reads throw; that itself means the page moved.
            return False

        return any(self._same_field(other, field_) and other.visible
                   for other in fields)

    @staticmethod
    def _same_field(a: guard_mod.DomField, b: guard_mod.DomField) -> bool:
        """Two descriptions of the same box, across two reads of the page."""
        if a.id and b.id:
            return a.id == b.id
        if a.name and b.name:
            return a.name == b.name and a.type == b.type
        return False

    # --- actions, each one guarded ---------------------------------------

    def _fill(self, page: Any, selector: str, value: str) -> bool:
        """Type one value, after checking that field is not hers.

        The second of the two refusals described at the top of this section.
        The whole-page check has already passed by the time we get here, so
        this one is redundant -- and it is the one that will still be here when
        somebody adds a step and forgets the other.
        """
        if not value:
            return False
        self._clear_or_hand_over(page)

        target = self._field_for(page, selector)
        if target is not None:
            reason = guard_mod.sensitive_dom_field(target)
            if reason:
                log.error("refusing to type into a field of hers (%s)", reason)
                raise HandoverRefused(reason)

        page.fill(selector, value, timeout=PAGE_TIMEOUT_MS)
        return True

    @staticmethod
    def _field_for(page: Any, selector: str) -> guard_mod.DomField | None:
        """Find the described field a selector points at, if we can see it."""
        try:
            name = page.eval_on_selector(
                selector,
                "el => el.getAttribute('name') || el.id || ''",
            )
        except Exception:
            return None
        if not name:
            return None
        for candidate in read_fields(page):
            if candidate.name == name or candidate.id == name:
                return candidate
        return None

    def _click(self, page: Any, selector: str) -> None:
        """Click something, after checking the page has nothing of hers on it."""
        self._clear_or_hand_over(page)
        page.click(selector, timeout=PAGE_TIMEOUT_MS)

    # --- the run ---------------------------------------------------------

    def run(self, goal: str, page: Any) -> BookingOutcome:
        """Fill the search form and choose the first train, on an open page.

        Takes an already-open Playwright ``page`` so the browser's lifetime
        belongs to the caller (and so the tests can drive the same code against
        the demo pages). :func:`book_for_her` is the version that opens one.
        """
        request = parse_booking(goal)
        log.info("booking: route known=%s date known=%s",
                 request.has_route, bool(request.date))

        try:
            page.goto(self._url, timeout=PAGE_TIMEOUT_MS)
            self._clear_or_hand_over(page)

            values = {
                "from": request.origin,
                "to": request.destination,
                "date": request.date,
            }
            for name in _SEARCH_FIELDS:
                if self._is_stopped():
                    return self._stopped()
                if self._fill(page, f"#{name}", values[name]):
                    self.outcome.filled[name] = values[name]

            if not self.outcome.filled:
                self.outcome.ended = "handed_over"
                self.outcome.message = (
                    "I could not tell where you want to go, so the form is "
                    "yours to fill in."
                )
                return self.outcome

            self._click(page, "#search")
            page.wait_for_load_state("load", timeout=PAGE_TIMEOUT_MS)

            self._click(page, FIRST_TRAIN_SELECTOR)
            page.wait_for_load_state("load", timeout=PAGE_TIMEOUT_MS)
            self.outcome.chose_train = True

            # The login page. This call is the demo: it finds her password box,
            # circles it, and waits. When she has signed in it carries on to
            # the OTP page and stops again, then the card page and stops again.
            self._clear_or_hand_over(page)

            self.outcome.ended = "finished"
            self.outcome.message = "Your details are filled in. The rest is yours."
            return self.outcome

        except HandoverRefused as refused:
            self.outcome.ended = "stopped" if str(refused) == "stopped" else "handed_over"
            self.outcome.message = (
                "Stopped." if self.outcome.ended == "stopped"
                else "This part is yours. I have left it with you."
            )
            log.info("booking flow ended: %s (%s)", self.outcome.ended, refused)
            return self.outcome
        except Exception as exc:
            log.exception("the booking flow failed")
            self.outcome.ended = "failed"
            self.outcome.message = f"The booking page did not behave: {exc}"
            return self.outcome

    def _stopped(self) -> BookingOutcome:
        self.outcome.ended = "stopped"
        self.outcome.message = "Stopped."
        return self.outcome


def book_for_her(
    goal: str,
    on_handover: Callable[[Bbox | None, str], None] | None = None,
    on_resume: Callable[[], None] | None = None,
    is_stopped: Callable[[], bool] | None = None,
    url: str = DEMO_SITE_URL,
    screen_scale: float | None = None,
) -> BookingOutcome:
    """Open a **headed** Chromium and run the booking flow (MASTERSPEC 3B).

    Headed, always. She watches what is typed on her behalf; that is the
    difference between "it does it for me" and "something is using my laptop".

    The permission check is first, and it is the real one from ``guard.py``
    (CLAUDE.md rule 4): if the table stops allowing ``BROWSE_AND_FILL``, this
    function stops opening browsers, with no second copy of the policy to
    update.

    The browser is left **open** at the end on purpose. She is mid-booking: the
    whole point is that she finishes the sign-in and the payment herself, and
    closing the window out from under her would undo the work we just did.
    """
    outcome = BookingOutcome()
    if not guard_mod.can_do_it(TaskType.BROWSE_AND_FILL):
        outcome.ended = "stopped"
        outcome.message = "I am not allowed to fill in forms for you."
        return outcome

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        outcome.ended = "failed"
        outcome.message = "The browser part is not installed on this laptop."
        log.exception("playwright is not available")
        return outcome

    flow = BookingFlow(on_handover=on_handover, on_resume=on_resume,
                       is_stopped=is_stopped, url=url,
                       screen_scale=screen_scale)
    try:
        # Started, not entered as a context manager, and this is the whole
        # reason the window stays open: leaving a `with sync_playwright()`
        # block stops the driver, and stopping the driver closes every browser
        # it launched -- including the one she is halfway through signing in
        # to. So the driver is kept alive deliberately and shut down when the
        # app quits (:func:`close_browser`).
        playwright = sync_playwright().start()
    except Exception as exc:
        log.exception("could not start the browser driver")
        outcome.ended = "failed"
        outcome.message = f"I could not open the browser: {exc}"
        return outcome

    try:
        browser = playwright.chromium.launch(headless=False)
    except Exception as exc:
        log.exception("could not launch Chromium")
        playwright.stop()
        outcome.ended = "failed"
        outcome.message = (
            "I could not open the browser. It may need installing: "
            "playwright install chromium"
        )
        log.info("hint: %s", exc)
        return outcome

    _OPEN_BROWSERS.append((playwright, browser))
    page = browser.new_page()
    log.info("browser open and visible to her; leaving it open afterwards")
    return flow.run(goal, page)


#: Browsers we opened for her and deliberately did not close, with the driver
#: that owns each one. Module level because their lifetime is the app's, not
#: one task's.
_OPEN_BROWSERS: list[tuple[Any, Any]] = []


def close_browser() -> None:
    """Shut down anything we opened. Called when Tiny Me itself quits.

    Not called at the end of a booking: she is still typing in that window.
    Ctrl+Alt+P stops Tiny Me from *acting*, which is a different thing from
    closing her half-finished booking -- MASTERSPEC 6 promises the first, and
    doing the second would lose her work.
    """
    while _OPEN_BROWSERS:
        playwright, browser = _OPEN_BROWSERS.pop()
        for close, what in ((browser.close, "browser"), (playwright.stop, "driver")):
            try:
                close()
            except Exception:
                log.info("the %s was already gone", what)

