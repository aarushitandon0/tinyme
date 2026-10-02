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

from app import capture, ocr  # noqa: E402
from app.elements import MAX_ELEMENTS, cap_elements, number_elements, render_for_model  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=MAX_ELEMENTS,
                        help=f"max elements to keep (default {MAX_ELEMENTS})")
    parser.add_argument("--timings", action="store_true", help="print stage timings")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
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
    raw, ocr_ms = ocr.read_screen(shot.image, shot.monitor)

    taskbar = capture.taskbar_rect()
    foreground = capture.foreground_rect()
    kept = cap_elements(raw, limit=args.limit, taskbar_rect=taskbar,
                        foreground_rect=foreground)
    elements = number_elements(kept)

    width, height = shot.size_px
    print(f"# monitor {width}x{height} physical px at "
          f"({shot.monitor.left}, {shot.monitor.top})")
    print(f"# ocr found {len(raw)}, kept {len(elements)} (limit {args.limit})")
    if taskbar is None:
        print("# taskbar not found; its icons will be missing until P5 adds UIA")
    print()
    print(render_for_model(elements) or "(no elements)")

    if args.timings:
        print()
        print(f"# capture {shot.elapsed_ms:.0f} ms | ocr {ocr_ms:.0f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
