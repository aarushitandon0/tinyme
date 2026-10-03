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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from app import guard as guard_mod
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
