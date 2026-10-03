"""Freeze a screen as eval data: the screenshot plus the element list (P10).

This is the **one** place in the repo allowed to write an image to disk.
CLAUDE.md rule 6 says screenshots stay in memory, with eval tooling run on the
developer's own machine as the stated exception, so the exception lives here and
nowhere near ``app/``.

    python eval\\capture_tool.py --app explorer            # hotkey mode
    python eval\\capture_tool.py --app explorer --once     # save one and exit
    python eval\\capture_tool.py --app notepad --once --delay 4

In hotkey mode: **Ctrl+Alt+S** saves the current screen, **Ctrl+Alt+Q** quits.
The hotkey exists so the screen being frozen is a real one -- arrange the window
you want, then press the key without this terminal in the foreground. ``--once``
with ``--delay`` is the scriptable version of the same thing.

Two files come out per shot:

* ``eval/screenshots/<app>_<nn>.png`` -- the capture, exactly as ``mss`` gave it.
* ``eval/elements/<app>_<nn>.json``   -- the numbered element list, *with*
  bboxes, plus the window and the timings.

The element list is produced by importing the live pipeline's read stage
(``app.main._read_screen``) rather than a copy of it. If the pipeline changes,
the eval data changes with it, which is the only way a frozen list can honestly
be called "what the model would have seen".

**Never point this at the real user's screen.** Dummy files, the developer's own
laptop, nothing private on display. The PNGs are committed, so anything visible
here is public.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import capture as capture_mod  # noqa: E402
from app import uia as uia_mod  # noqa: E402
from app import watcher as watcher_mod  # noqa: E402
from app.elements import Element, cap_elements, number_elements  # noqa: E402

log = logging.getLogger("capture_tool")

SCREENSHOT_DIR = REPO_ROOT / "eval" / "screenshots"
ELEMENTS_DIR = REPO_ROOT / "eval" / "elements"

#: App slugs go in filenames and group the per-app results in the table, so
#: they stay boring: lowercase letters, digits, underscore.
APP_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")

#: Hotkeys, in pynput GlobalHotKeys syntax. Deliberately *not* the app's own
#: Ctrl+Alt+H / Ctrl+Alt+P, so this tool can run while Tiny Me is running.
HOTKEY_SAVE = "<ctrl>+<alt>+s"
HOTKEY_QUIT = "<ctrl>+<alt>+q"

#: Schema version of the elements JSON. Bump it if the fields change, so an old
#: frozen list cannot be silently re-scored against new assumptions.
FORMAT_VERSION = 1


def read_elements() -> tuple[list[Element], object, dict[str, float], dict[str, int]]:
    """Capture the primary monitor and build the numbered element list.

    The sequence is the one from ``main.plan_next_step``: read both sources and
    merge, cap with the taskbar and foreground window prioritised, *then*
    number. Capping before numbering matters -- numbering first would leave
    holes in the ids the model sees (MASTERSPEC 5.2).

    Returns:
        (elements, capture, timings_ms, counts). The capture is returned so the
        caller can save the same pixels the elements were read from, rather than
        grabbing the screen a second time and getting a different one.
    """
    from app.main import _read_screen  # Imported late: pulls in Qt and pynput.

    shot = capture_mod.grab_primary()
    found, read_ms = _read_screen(shot.image, shot.monitor)

    taskbar = capture_mod.taskbar_rect()
    foreground = capture_mod.foreground_rect()
    kept = cap_elements(found, taskbar_rect=taskbar, foreground_rect=foreground)
    elements = number_elements(kept)

    timings = {"capture_ms": round(shot.elapsed_ms, 1), "read_ms": round(read_ms, 1)}
    counts = {"merged": len(found), "kept": len(elements)}
    return elements, shot, timings, counts


def next_index(app: str, directory: Path = SCREENSHOT_DIR) -> int:
    """Lowest free ``<app>_<nn>`` number, so a re-run never overwrites data."""
    pattern = re.compile(rf"^{re.escape(app)}_(\d+)\.png$")
    used = {
        int(match.group(1))
        for path in directory.glob(f"{app}_*.png")
        if (match := pattern.match(path.name))
    }
    index = 1
    while index in used:
        index += 1
    return index


def element_to_json(element: Element) -> dict:
    """One element as a plain dict, bbox included.

    The bbox is *in* the eval file on purpose: ``vision_coords`` scoring needs
    it to decide whether the model's (x, y) landed on the right thing. It still
    never reaches a prompt -- ``run_eval.py`` rebuilds Elements and renders them
    through ``elements.render_for_model``, the same single function the app uses.
    """
    return {
        "id": element.id,
        "text": element.text,
        "source": element.source,
        "role": element.role,
        "region": element.region,
        "bbox_px": [round(value, 1) for value in element.bbox_px],
    }


def save_shot(app: str, index: int | None = None) -> tuple[Path, Path]:
    """Freeze one screen. Returns the (png, json) paths written."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ELEMENTS_DIR.mkdir(parents=True, exist_ok=True)

    elements, shot, timings, counts = read_elements()
    window = watcher_mod.read_window()

    if index is None:
        index = next_index(app)
    stem = f"{app}_{index:02d}"
    png_path = SCREENSHOT_DIR / f"{stem}.png"
    json_path = ELEMENTS_DIR / f"{stem}.json"

    from PIL import Image

    Image.fromarray(shot.image).save(png_path, format="PNG", optimize=True)

    payload = {
        "format_version": FORMAT_VERSION,
        "shot": png_path.name,
        "app": app,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # Screen geometry, so bboxes can be turned into image indices: a bbox is
        # in screen space and the image starts at the monitor's origin.
        "monitor": {
            "left": shot.monitor.left,
            "top": shot.monitor.top,
            "width": shot.monitor.width,
            "height": shot.monitor.height,
        },
        "image_size_px": list(shot.size_px),
        "screen_scale": round(capture_mod.screen_scale(), 4),
        "window": {"class": window.class_name, "title": window.title},
        "timings_ms": timings,
        "counts": counts,
        "elements": [element_to_json(element) for element in elements],
    }
    json_path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")

    log.info(
        "saved %s: %d elements (%d merged), read %.0f ms, window %s / %r",
        stem, counts["kept"], counts["merged"], timings["read_ms"],
        window.class_name or "?", window.title,
    )
    return png_path, json_path


def run_hotkey_loop(app: str) -> int:
    """Block until Ctrl+Alt+Q, saving a shot on every Ctrl+Alt+S.

    Saving happens on pynput's own thread. That is fine *here* and only here:
    there are no Qt widgets in this tool (CLAUDE.md rule 8 is about widgets),
    and the work is a capture plus a file write. The app itself must still go
    through a signal.
    """
    from pynput import keyboard

    print(f"Ctrl+Alt+S saves a shot as {app}_nn, Ctrl+Alt+Q quits.")
    print("Arrange the window you want, then press the hotkey.\n")

    stop = False

    def on_save() -> None:
        try:
            save_shot(app)
        except Exception:
            log.exception("that shot failed; the rest of the session is unaffected")

    def on_quit() -> None:
        nonlocal stop
        stop = True

    with keyboard.GlobalHotKeys({HOTKEY_SAVE: on_save, HOTKEY_QUIT: on_quit}) as hotkeys:
        while not stop and hotkeys.is_alive():
            time.sleep(0.1)
    print("done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", required=True,
                        help="app slug for the filename and the per-app table (e.g. explorer)")
    parser.add_argument("--once", action="store_true",
                        help="save one shot immediately instead of waiting for the hotkey")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="with --once, seconds to wait first so you can focus a window")
    parser.add_argument("--index", type=int, default=None,
                        help="force the <nn> suffix instead of taking the next free one")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    if not APP_SLUG.match(args.app):
        parser.error("--app must be a lowercase slug like explorer or demo_site")

    # ~450 ms on the first COM call, which would otherwise eat the whole UIA
    # budget and make the first shot of a session look worse than the app does.
    uia_mod.warm_up()

    if args.once:
        if args.delay > 0:
            print(f"capturing in {args.delay:.0f} s -- focus the window you want")
            time.sleep(args.delay)
        png_path, json_path = save_shot(args.app, args.index)
        print(f"{png_path.relative_to(REPO_ROOT)}\n{json_path.relative_to(REPO_ROOT)}")
        return 0

    return run_hotkey_loop(args.app)


if __name__ == "__main__":
    raise SystemExit(main())
