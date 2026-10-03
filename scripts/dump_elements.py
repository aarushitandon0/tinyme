"""Capture the primary monitor once and print the numbered element list.

This is the P2 acceptance check: what it prints is exactly what the model will
see (MASTERSPEC 5.2), with no coordinates anywhere in the output.

    python scripts\\dump_elements.py
    python scripts\\dump_elements.py --limit 40 --timings
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import capture, ocr, uia  # noqa: E402
from app.elements import (  # noqa: E402
    MAX_ELEMENTS,
    cap_elements,
    merge_elements,
    number_elements,
    render_for_model,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=MAX_ELEMENTS,
                        help=f"max elements to keep (default {MAX_ELEMENTS})")
    parser.add_argument("--timings", action="store_true", help="print stage timings")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--no-uia", action="store_true",
                        help="OCR only, to see what UI Automation adds")
    args = parser.parse_args()

    # The Windows console defaults to cp1252, and OCR regularly returns
    # characters outside it (box-drawing glyphs now, Devanagari once Tier 2
    # lands). Without this, printing the element list raises UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    shot = capture.grab_primary()
    ocr_found, ocr_ms = ocr.read_screen(shot.image, shot.monitor)

    if args.no_uia:
        uia_found, uia_ms = [], 0.0
    else:
        # COM's first call costs ~450 ms, which would eat the whole 300 ms
        # budget and make this dump look worse than a running app, where
        # main.py warms up at startup.
        uia.warm_up()
        uia_found, uia_ms = uia.collect(shot.monitor)

    merged = merge_elements(ocr_found, uia_found)
    taskbar = capture.taskbar_rect()
    foreground = capture.foreground_rect()
    kept = cap_elements(merged, limit=args.limit, taskbar_rect=taskbar,
                        foreground_rect=foreground)
    elements = number_elements(kept)

    width, height = shot.size_px
    print(f"# monitor {width}x{height} physical px at "
          f"({shot.monitor.left}, {shot.monitor.top})")
    print(f"# ocr {len(ocr_found)} + uia {len(uia_found)} -> merged "
          f"{len(merged)}, kept {len(elements)} (limit {args.limit})")
    if taskbar is None:
        print("# taskbar not found; its icons will be missing from this list")
    elif not uia_found and not args.no_uia:
        print("# UI Automation returned nothing; icon-only buttons have no names")
    print()
    print(render_for_model(elements) or "(no elements)")

    if args.timings:
        print()
        print(f"# capture {shot.elapsed_ms:.0f} ms | ocr {ocr_ms:.0f} ms | "
              f"uia {uia_ms:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
