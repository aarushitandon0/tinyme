"""Day-one DPI calibration: do the circles land on the words?

MASTERSPEC 5.6 and 14 both say to do this before building anything that
depends on the overlay. It captures the screen, OCRs it, circles the three
largest words it found, and holds them for five seconds so you can eyeball
whether each ring sits on its word.

It also proves the capture-exclusion path, because a calibration run that
circles our own instruction text would look fine and mean nothing.

    python scripts\\calibrate.py
    python scripts\\calibrate.py --seconds 10 --words 5

Read the printed report. "circle offset" should be near zero at every scaling.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import capture, ocr  # noqa: E402
from app.elements import number_elements, to_logical  # noqa: E402
from app.overlay import FRAME_PAD, Mark, MarkShape, Overlay  # noqa: E402

log = logging.getLogger("calibrate")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=5.0, help="how long to show")
    parser.add_argument("--words", type=int, default=3, help="how many words to circle")
    parser.add_argument("--skip-exclusion-check", action="store_true",
                        help="do not run the capture-exclusion probe (faster)")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Qt must own the main thread before anything asks a QScreen for its dpr.
    app = QApplication(sys.argv)
    overlay = Overlay()

    screen = app.primaryScreen()
    dpr = overlay.dpr
    print(f"Qt screen: {screen.geometry().width()}x{screen.geometry().height()} logical, "
          f"devicePixelRatio {dpr:.4f}  (Windows scaling {dpr * 100:.0f}%)")

    if not args.skip_exclusion_check:
        mode = overlay.verify_capture_exclusion()
        print(f"Capture exclusion: {mode.value}")

    shot = overlay.grab_without_me()
    print(f"mss capture: {shot.size_px[0]}x{shot.size_px[1]} physical px "
          f"at ({shot.monitor.left}, {shot.monitor.top})")

    ratio = shot.size_px[0] / max(1, screen.geometry().width())
    print(f"physical/logical width ratio: {ratio:.4f} "
          f"(should match devicePixelRatio {dpr:.4f})")
    if abs(ratio - dpr) > 0.01:
        print("  WARNING: these disagree. Every circle will be offset. Check that "
              "Qt and mss are both per-monitor DPI aware before trusting the overlay.")

    elements, ocr_ms = ocr.read_screen(shot.image, shot.monitor)
    if not elements:
        print("OCR found no text. Open something with large words and retry.")
        return 1

    def area(element) -> float:
        left, top, right, bottom = element.bbox_px
        return (right - left) * (bottom - top)

    largest = number_elements(sorted(elements, key=area, reverse=True)[: args.words])

    print(f"\nOCR found {len(elements)} elements in {ocr_ms:.0f} ms. "
          f"Circling the {len(largest)} largest:")
    for element in largest:
        left, top, right, bottom = element.bbox_px
        logical = to_logical(element.bbox_px, dpr)
        print(f"  physical ({left:.0f},{top:.0f})-({right:.0f},{bottom:.0f})  ->  "
              f"logical ({logical[0]:.0f},{logical[1]:.0f})-({logical[2]:.0f},{logical[3]:.0f})"
              f"   frame pad {FRAME_PAD} logical px")

    # FRAME, not the usual auto-chosen shape: this script exists to catch a
    # physical->logical conversion that is off by a few pixels, and a frame
    # sits on the bbox's real edges where a ring hides that error inside 20 px
    # of padding.
    marks = [
        Mark(bbox_px=element.bbox_px, instruction=f"{element.id}. this word",
             shape=MarkShape.FRAME)
        for element in largest
    ]
    overlay.show_marks(marks)

    print(f"\nHolding for {args.seconds:.0f} s. Each frame should sit squarely on its word, "
          "not offset down-right or shrunk toward the top-left corner.")
    QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec()
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
