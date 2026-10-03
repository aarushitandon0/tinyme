"""Collect the eval screens deliberately, with dummy content only (P10).

``capture_tool.py`` freezes *whatever is on screen*. That is the right tool when
you are reproducing something you just saw, and the wrong one for building the
dataset: the shots get committed, so a stray window title or a real filename in
one of them is published. This script arranges each screen itself, from a
throwaway home folder it creates, so every PNG in ``eval/screenshots/`` contains
only things this file put there.

    python eval\\collect.py --group all           # ~25 screens, takes a while
    python eval\\collect.py --group explorer
    python eval\\collect.py --list

**It takes over the screen.** Each screen means launching or focusing a window,
so for the duration the foreground window is not yours. OCR costs ~20 s per shot
on this laptop (notes/bench.md), which is nearly all of the runtime.

The dummy home lives in the system temp directory, not in the repo, and its
Downloads folder is named "Downloads" on purpose: scene A's success check is
``window_title_contains "Downloads"`` and Explorer titles are folder names
(CLAUDE.md rule 11), so a folder called anything else would not exercise it.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from eval.capture_tool import save_shot  # noqa: E402

log = logging.getLogger("collect")

#: The throwaway home. Outside the repo, so nothing here is ever committed, and
#: stable across runs so a re-collection reuses the same dummy files.
SANDBOX = Path(tempfile.gettempdir()) / "tinyme_eval_home"

#: Dummy files, with the kind of names she actually has: bills, forms, photos,
#: spaces in the names. Nothing resembling a real document of hers, and nothing
#: the guard's keyword list would fire on (no "bank", no "card"), because a
#: sensitive filename belongs in a guard test, not in element-picking data.
DOWNLOADS_FILES = (
    "electricity bill september.pdf",
    "water bill august.pdf",
    "insurance renewal form.pdf",
    "family photo diwali.jpg",
    "recipe notes.txt",
    "train ticket october.pdf",
)
DOWNLOADS_SUBFOLDER = "old stuff"

#: What Notepad opens, and the text scene C ("make this text bigger") works on.
LETTER_NAME = "letter to the society office.txt"
LETTER_TEXT = """To the Society Office

Please note that the water tank on the third floor has been leaking since
Monday. I have reported this twice on the phone already.

Kindly send someone to look at it this week.

Thank you,
Flat 402
"""

#: Fragments that must never appear in a kept shot, one per line, matched
#: case-insensitively against the OCR and UIA text of the capture. The file is
#: gitignored, because it is a list of the things on *this* developer's machine
#: that must not be published -- document names, a client's name, a window that
#: happens to float above everything.
#:
#: This exists because reviewing 23 screenshots by eye does not scale and did not
#: work: the first full collection quietly caught another app's window floating
#: on top in one shot, and a text editor showing personal documents in three
#: more. A published screenshot cannot be un-published, so the check is a
#: machine's job.
FORBIDDEN_PATH = REPO_ROOT / "eval" / "forbidden.txt"


def forbidden_fragments(path: Path = FORBIDDEN_PATH) -> list[str]:
    """Lines from the deny-list, lower-cased. Empty when there is no file."""
    if not path.exists():
        return []
    return [
        line.strip().casefold()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def contamination(elements: list[dict], fragments: Sequence[str]) -> list[str]:
    """Which forbidden fragments this capture's text contains."""
    haystack = " | ".join(element["text"] for element in elements).casefold()
    return [fragment for fragment in fragments if fragment in haystack]


#: How long to wait after launching before the window is drawn and focused.
#: Generous on purpose: a half-painted window produces OCR text that nothing on
#: the real screen ever shows, and one bad shot poisons a label row quietly.
SETTLE_S = 3.0


@dataclass(frozen=True)
class Screen:
    """One screen to freeze.

    ``app`` becomes the filename prefix and the per-app row in the results
    table, so the apps are the axis the eval is honest about: a system that only
    works in Explorer should be visibly a system that only works in Explorer.
    """

    app: str
    what: str
    #: Shell command to launch or re-focus the screen. Run through the shell
    #: because most of these are protocol handlers (``ms-settings:``) rather
    #: than executables.
    launch: str
    #: Window class to wait for and bring forward. None = trust the launcher.
    window_class: str | None = None
    #: Substring the window title must contain before the shot is taken. This is
    #: what catches Explorer still navigating: the window exists, is focused and
    #: is showing the *previous* folder. Case-insensitive.
    expect_title: str = ""
    #: Keystrokes to send once it is focused, for the states a URL cannot reach
    #: (an open menu). WScript.Shell SendKeys syntax.
    keys: tuple[str, ...] = ()
    settle_s: float = SETTLE_S
    #: Maximise before capturing. On by default, for two reasons that turned out
    #: to be the same reason: a windowed app leaves the editor and the browser
    #: visible behind it, which fills the element list with text from apps the
    #: goal has nothing to do with, and puts whatever those windows were showing
    #: into a PNG that gets committed.
    maximize: bool = True


def build_sandbox() -> Path:
    """Create the dummy home and return its Downloads folder."""
    downloads = SANDBOX / "Downloads"
    (downloads / DOWNLOADS_SUBFOLDER).mkdir(parents=True, exist_ok=True)
    (SANDBOX / "Documents").mkdir(parents=True, exist_ok=True)

    for name in DOWNLOADS_FILES:
        path = downloads / name
        if not path.exists():
            # Non-empty, so Explorer shows a size rather than 0 KB everywhere,
            # and the PDFs are not real PDFs -- nothing here is ever opened.
            path.write_text(f"dummy file for Tiny Me eval: {name}\n", encoding="utf-8")
    for name in ("meeting notes.txt", "address proof.pdf"):
        path = SANDBOX / "Documents" / name
        if not path.exists():
            path.write_text(f"dummy file for Tiny Me eval: {name}\n", encoding="utf-8")
    inside_old = downloads / DOWNLOADS_SUBFOLDER / "photos from last year.zip"
    if not inside_old.exists():
        inside_old.write_text("dummy file for Tiny Me eval\n", encoding="utf-8")

    letter = SANDBOX / LETTER_NAME
    if not letter.exists():
        letter.write_text(LETTER_TEXT, encoding="utf-8")
    log.info("dummy home ready at %s", SANDBOX)
    return downloads


def screens() -> list[Screen]:
    """Every screen in the dataset, in collection order.

    Five apps, which is the MASTERSPEC 10 target, chosen for what they break
    differently: Explorer is the demo scene, Settings is a modern XAML app whose
    labels OCR reads but whose controls UIA names, Notepad is the generic
    "any app" case for scene C, Calculator is almost entirely icon-only buttons
    (the stated weakness), and the demo site is scene B in a browser.
    """
    downloads = SANDBOX / "Downloads"
    letter = SANDBOX / LETTER_NAME
    return [
        # --- Explorer: scene A, plus the folders a wrong turn lands in --------
        Screen("explorer", "Downloads folder, the target file among others",
               f'explorer.exe "{downloads}"', "CabinetWClass", "Downloads"),
        Screen("explorer", "inside 'old stuff', the wrong folder",
               f'explorer.exe "{downloads / DOWNLOADS_SUBFOLDER}"',
               "CabinetWClass", "old stuff"),
        Screen("explorer", "Documents folder, target not here",
               f'explorer.exe "{SANDBOX / "Documents"}"', "CabinetWClass", "Documents"),
        Screen("explorer", "Downloads with the View menu open",
               f'explorer.exe "{downloads}"', "CabinetWClass", "Downloads", keys=("%v",)),
        Screen("explorer", "the dummy home, Downloads visible as a folder",
               f'explorer.exe "{SANDBOX}"', "CabinetWClass", SANDBOX.name),

        # --- Paint: the generic "any app" case for scene C --------------------
        # Notepad was the obvious choice here and had to be dropped: Windows 11
        # Notepad is a single-instance tabbed app, so opening our letter adds a
        # tab beside whatever the developer already had open, and three shots
        # came back carrying the names of personal documents. Paint opens a fresh
        # window with nothing in it, and its icon-heavy toolbar exercises the
        # UIA-names-what-OCR-cannot-read case besides.
        Screen("paint", "blank canvas, toolbar and ribbon",
               "mspaint.exe", "MSPaintApp", "Paint"),
        Screen("paint", "blank canvas, File menu open",
               "mspaint.exe", "MSPaintApp", "Paint", keys=("%f",)),

        # --- Settings: modern XAML, the UIA-vs-OCR case ----------------------
        # Every Settings page is one window titled "Settings", so the title can
        # only confirm the app, not the page. The page is confirmed by eye in the
        # PNG when labelling.
        Screen("settings", "Display settings, scale and resolution",
               "start ms-settings:display", "ApplicationFrameWindow", "Settings"),
        Screen("settings", "Printers and scanners",
               "start ms-settings:printers", "ApplicationFrameWindow", "Settings"),
        Screen("settings", "Accessibility, text size",
               "start ms-settings:easeofaccess-display", "ApplicationFrameWindow", "Settings"),
        Screen("settings", "Bluetooth and devices",
               "start ms-settings:bluetooth", "ApplicationFrameWindow", "Settings"),
        Screen("settings", "Wi-Fi and network",
               "start ms-settings:network", "ApplicationFrameWindow", "Settings"),
        Screen("settings", "Settings home page",
               "start ms-settings:", "ApplicationFrameWindow", "Settings"),

        # --- Calculator: icon-only buttons, the documented weak spot ----------
        Screen("calculator", "standard calculator, keypad",
               "start calculator:", "ApplicationFrameWindow", "Calculator"),
        Screen("calculator", "navigation menu open, modes listed",
               "start calculator:", "ApplicationFrameWindow", "Calculator", keys=("%+n",)),
    ]


#: Demo-site pages are collected separately, through Playwright, because they
#: need a server and a browser driven to a specific URL rather than a launcher.
DEMO_PAGES = (
    ("search form, empty", "index.html"),
    ("results list, trains to choose from", "results.html"),
    ("login page, password box visible", "login.html"),
    ("OTP page", "otp.html"),
    ("payment page, card fields", "payment.html"),
    ("booking done", "done.html"),
)


def windows_of_class(window_class: str) -> list[int]:
    """Visible top-level windows of this class, oldest first.

    Zero-sized windows are skipped: ``ApplicationFrameWindow`` in particular has
    invisible cloaked instances left over from closed Store apps, and focusing
    one of those gives a screen with nothing on it.
    """
    import win32gui

    found: list[int] = []

    def visit(hwnd: int, _: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        if win32gui.GetClassName(hwnd) != window_class:
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 200 or bottom - top < 120:
            return
        found.append(hwnd)

    win32gui.EnumWindows(visit, None)
    return found


def force_focus(hwnd: int) -> bool:
    """Make ``hwnd`` the foreground window. True only if it actually became it.

    ``SetForegroundWindow`` is refused outright for a process that does not
    already own the foreground window, which is always our case here -- the
    first version of this script called it, ignored the result, and quietly
    captured the editor instead of the app in four shots out of six.

    Two documented ways round the restriction, tried in order:

    1. ``AttachThreadInput`` to the current foreground window's thread. Windows
       then treats us as part of that input queue and allows the change.
    2. Minimise and restore. A window being restored is given the foreground by
       the shell, which does not need our permission.

    The return value is the measured outcome, not the API's, so a caller can
    skip a screen instead of saving a wrong one.
    """
    import win32con
    import win32gui
    import win32process

    def is_foreground() -> bool:
        return win32gui.GetForegroundWindow() == hwnd

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)
    if is_foreground():
        return True

    try:
        target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
        current_thread, _ = win32process.GetWindowThreadProcessId(
            win32gui.GetForegroundWindow()
        )
        win32process.AttachThreadInput(current_thread, target_thread, True)
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            win32process.AttachThreadInput(current_thread, target_thread, False)
    except Exception:
        log.debug("AttachThreadInput route failed", exc_info=True)

    time.sleep(0.4)
    if is_foreground():
        return True

    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(0.3)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.6)
    except Exception:
        log.debug("minimise/restore route failed", exc_info=True)
    return is_foreground()


def settle_on(window_class: str, expect_title: str, timeout_s: float = 12.0) -> bool:
    """Wait until a window of this class is focused and showing ``expect_title``.

    Both halves are needed. Explorer reuses one window, so after launching a new
    folder the window is already there, already focused, and still painting the
    *previous* folder for a second or more -- the class check passes and the shot
    is still of the wrong screen. The title is what tells us the navigation
    finished (CLAUDE.md rule 11: the title of an Explorer window is the folder
    name).
    """
    import win32gui

    deadline = time.monotonic() + timeout_s
    wanted = expect_title.casefold()
    while time.monotonic() < deadline:
        candidates = windows_of_class(window_class)
        for hwnd in reversed(candidates):
            title = (win32gui.GetWindowText(hwnd) or "").casefold()
            if wanted and wanted not in title:
                continue
            if force_focus(hwnd):
                return True
        time.sleep(0.5)
    return False


def send_keys(keys: tuple[str, ...]) -> None:
    """Send keystrokes to the focused window (WScript.Shell SendKeys syntax)."""
    if not keys:
        return
    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        for stroke in keys:
            shell.SendKeys(stroke)
            time.sleep(0.6)
    except Exception:
        log.warning("could not send keys %s; capturing the screen as it is", keys)


def minimize_everything() -> int:
    """Minimise every visible top-level window, and report how many.

    Maximising the target is not enough on this machine: another window sits
    above everything, so it appeared *on top of* a full-screen File Explorer in
    one shot and put its text into that screen's element list. Rather than guess
    which windows are always-on-top, start from an empty desktop.

    Minimising is reversible -- the windows are still on the taskbar -- which is
    why this does it rather than closing anything. It does take over the
    developer's desktop for the length of the run, which the docstring at the top
    of this file warns about.
    """
    import win32con
    import win32gui

    minimized = 0

    def visit(hwnd: int, _: object) -> None:
        nonlocal minimized
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return
        if not win32gui.GetWindowText(hwnd):
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left < 200 or bottom - top < 120:
            return
        # Progman is the desktop itself; minimising it does nothing useful and
        # on some machines repaints the icons.
        if win32gui.GetClassName(hwnd) in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            minimized += 1
        except Exception:
            log.debug("could not minimise a window", exc_info=True)

    win32gui.EnumWindows(visit, None)
    time.sleep(1.0)
    return minimized


def maximize_foreground() -> None:
    """Maximise whatever is in front, if it can be maximised.

    Calculator refuses (it has a fixed maximum size) and that is fine: the point
    is to cover the other windows, and a refusal just means this screen keeps
    whatever is behind it.
    """
    try:
        import win32con
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    except Exception:
        log.debug("could not maximise the foreground window", exc_info=True)


def keep_if_right_window(app: str, index: int, window_class: str | None,
                         expect_title: str, reassert: bool = True) -> bool:
    """Take the shot, and delete it again unless it caught the right window.

    Checking *before* the grab is not enough and this is the only check that
    cannot be raced: focus came back to the editor between a passing pre-flight
    check and the grab itself, and the saved PNG was of the editor. The capture
    records the foreground window next to the pixels, so the saved file can be
    held to account for what it actually contains.

    Both collection paths go through here. The browser path originally did not,
    and that is exactly where the one bad shot of the first full run came from.
    """
    # Re-assert focus in the last moment before the grab. Something on this
    # machine takes the foreground back a second or so after it is handed over,
    # and the gap between "focus confirmed" and "pixels read" is where shots were
    # being lost. Cheap when the window is already focused.
    if reassert and window_class:
        settle_on(window_class, expect_title, timeout_s=3.0)

    png_path, json_path = save_shot(app, index)
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    window = saved["window"]

    def discard(why: str) -> bool:
        log.warning("discarding %s: %s", png_path.name, why)
        png_path.unlink(missing_ok=True)
        json_path.unlink(missing_ok=True)
        return False

    if window_class and window["class"] != window_class:
        return discard(f"it captured {window['class']} / {window['title']!r}, "
                       f"not {window_class} / {expect_title!r}")
    if expect_title and expect_title.casefold() not in window["title"].casefold():
        return discard(f"the window was titled {window['title']!r}, "
                       f"which does not contain {expect_title!r}")

    found = contamination(saved["elements"], forbidden_fragments())
    if found:
        # The fragment itself is not logged: it is on the deny-list precisely
        # because it should not be written down anywhere that gets committed,
        # and this script's output ends up in a terminal transcript.
        return discard(f"{len(found)} forbidden fragment(s) were visible on screen "
                       "(see eval/forbidden.txt)")
    return True


def collect_screen(screen: Screen, index: int) -> bool:
    """Arrange one screen and freeze it. False if it could not be arranged.

    Nothing is saved unless the right window is confirmed in the foreground
    first. OCR costs ~20 s a shot, so checking before capturing is also the fast
    path, but the real reason is that a wrong shot is worse than a missing one:
    it gets labelled, scored, and quoted in the results table as if it were data.
    """
    log.info("--- %s_%02d: %s", screen.app, index, screen.what)
    subprocess.Popen(screen.launch, shell=True)
    time.sleep(screen.settle_s)

    if screen.window_class and not settle_on(screen.window_class, screen.expect_title):
        log.warning("never got %s / %r into the foreground; skipping this screen",
                    screen.window_class, screen.expect_title)
        return False

    if screen.maximize:
        maximize_foreground()
        # Maximising before the keys, so a menu opens at the size it will be
        # captured at rather than being re-laid-out underneath us.
        time.sleep(0.8)

    send_keys(screen.keys)
    time.sleep(0.6)

    return keep_if_right_window(screen.app, index, screen.window_class, screen.expect_title)


def collect_demo_site() -> int:
    """Serve ``demo_site/`` and freeze each page in a headed browser.

    The same server and the same headed Chromium the booking flow uses, so the
    chrome around the page (and therefore the element list) matches what scene B
    actually drives.
    """
    from playwright.sync_api import sync_playwright

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8000"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    saved = 0
    try:
        time.sleep(1.5)
        with sync_playwright() as play:
            # --start-maximized plus viewport=None so the page fills the screen:
            # a windowed browser leaves the editor visible beside it, and every
            # word of the editor lands in the element list as a distractor.
            browser = play.chromium.launch(headless=False, args=["--start-maximized"])
            page = browser.new_page(viewport=None)

            # Warm-up: a browser that has only just launched does not come
            # forward on the first try, so without this the *first* page in the
            # list pays for the launch and is the one that gets dropped. Costs a
            # second; the alternative was losing the search form from the data.
            page.goto(f"http://localhost:8000/demo_site/{DEMO_PAGES[0][1]}")
            page.wait_for_load_state("load")
            page.bring_to_front()
            settle_on("Chrome_WidgetWin_1", page.title(), timeout_s=20.0)

            for what, filename in DEMO_PAGES:
                index = saved + 1
                log.info("--- demo_site_%02d: %s", index, what)
                for attempt in (1, 2, 3):
                    page.goto(f"http://localhost:8000/demo_site/{filename}")
                    page.wait_for_load_state("load")
                    page.bring_to_front()
                    # bring_to_front moves the tab inside the browser; it does
                    # not make the browser the foreground window on Windows, so
                    # the launched apps' focus handling applies here too.
                    title = page.title()
                    if not settle_on("Chrome_WidgetWin_1", title):
                        log.warning("browser never came forward for %s (attempt %d)",
                                    filename, attempt)
                        continue
                    time.sleep(0.4)
                    if keep_if_right_window("demo_site", index,
                                            "Chrome_WidgetWin_1", title):
                        saved += 1
                        break
                else:
                    log.warning("giving up on %s", filename)
            browser.close()
    finally:
        server.terminate()
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--group", default="all",
                        help="app slug to collect, or 'all' (default)")
    parser.add_argument("--list", action="store_true",
                        help="print the planned screens and exit without touching the screen")
    parser.add_argument("--no-minimize-others", dest="minimize_others",
                        action="store_false",
                        help="leave your other windows open (they may end up in the shots)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    planned = screens()
    if args.list:
        for app in sorted({screen.app for screen in planned} | {"demo_site"}):
            group = [s for s in planned if s.app == app]
            if app == "demo_site":
                group = [Screen("demo_site", what, "") for what, _ in DEMO_PAGES]
            print(f"\n{app} ({len(group)} screens)")
            for index, screen in enumerate(group, start=1):
                print(f"  {app}_{index:02d}  {screen.what}")
        print(f"\n{len(planned) + len(DEMO_PAGES)} screens in total")
        return 0

    build_sandbox()
    if args.minimize_others:
        count = minimize_everything()
        log.info("minimised %d window(s) so each screen starts from an empty "
                 "desktop; they are still on the taskbar", count)

    wanted = args.group
    saved = 0
    if wanted in ("all", "demo_site"):
        demo_first = wanted == "demo_site"
        if demo_first:
            return 0 if collect_demo_site() else 1

    # The index counts *kept* shots, so a discarded screen is retried into the
    # same slot and the numbering stays contiguous. Two attempts: focus theft is
    # intermittent, and a screen that fails twice is failing for a reason a third
    # try will not fix.
    kept: dict[str, int] = {}
    skipped: list[str] = []
    for screen in planned:
        if wanted not in ("all", screen.app):
            continue
        index = kept.get(screen.app, 0) + 1
        for attempt in (1, 2):
            if collect_screen(screen, index):
                kept[screen.app] = index
                saved += 1
                break
            log.info("attempt %d of 2 failed for %s: %s", attempt, screen.app, screen.what)
        else:
            skipped.append(f"{screen.app}: {screen.what}")

    if wanted == "all":
        saved += collect_demo_site()

    print(f"\n{saved} screens saved to eval/screenshots and eval/elements")
    if skipped:
        # Printed rather than swallowed: a thin app in the table should be
        # traceable to screens that could not be arranged, not read as a result.
        print(f"{len(skipped)} screens could not be arranged twice over:")
        for line in skipped:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
