"""OCR: RapidOCR text boxes to Elements (MASTERSPEC 5.2).

Installed API, checked before writing this (CLAUDE.md asks for exactly that):

    rapidocr 3.9.2, onnxruntime 1.30.0
    RapidOCR(config_path=None, params=None)
    ocr(img, use_det=..., text_score=..., ...) -> RapidOCROutput
    RapidOCROutput.boxes   -> (N, 4, 2) array of quadrilateral corners
    RapidOCROutput.txts    -> tuple[str, ...]
    RapidOCROutput.scores  -> tuple[float, ...]

This is the newer ``rapidocr`` package, *not* ``rapidocr_onnxruntime``; the two
differ, which is why CLAUDE.md tells us to check first. The API above is pinned
by the lower bound in pyproject.toml.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

import numpy as np

from app.elements import Bbox, Element, Monitor, classify_region

log = logging.getLogger(__name__)

#: MASTERSPEC 5.2: drop boxes the recogniser is unsure about.
MIN_SCORE = 0.5
#: MASTERSPEC 5.2: drop boxes too short to be real UI text.
MIN_HEIGHT_PX = 8

_engine = None


def _get_engine():
    """Load RapidOCR once and keep it. First load is slow; steps after are not.

    MASTERSPEC 10 says to report the slow first load honestly rather than hide
    it, so the load time is logged.
    """
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR  # Imported lazily: it pulls in onnxruntime.

        started = time.perf_counter()
        _engine = RapidOCR()
        log.info("RapidOCR loaded in %.0f ms", (time.perf_counter() - started) * 1000)
    return _engine


def polygon_to_bbox(polygon) -> Bbox:
    """Collapse a 4-point quadrilateral to an axis-aligned (l, t, r, b) box."""
    points = np.asarray(polygon, dtype=float).reshape(-1, 2)
    xs, ys = points[:, 0], points[:, 1]
    return (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))


def elements_from_raw(
    boxes,
    txts: Sequence[str] | None,
    scores: Sequence[float] | None,
    monitor: Monitor,
    origin: tuple[int, int] = (0, 0),
) -> list[Element]:
    """Turn raw RapidOCR output into unnumbered Elements, applying the filters.

    Pure and model-free, so the filtering rules are unit-testable.

    Args:
        boxes: (N, 4, 2) corner array, or None when nothing was detected.
        txts: recognised strings, parallel to ``boxes``.
        scores: confidences, parallel to ``boxes``.
        monitor: geometry used for region classification.
        origin: physical-pixel offset of the image's top-left corner. The image
            starts at the monitor's origin, so bbox coordinates are shifted by
            it to stay in screen space (a monitor left of the primary one has a
            negative ``left``).

    Returns:
        Elements with ``id=0``; call ``elements.number_elements`` to number them.
    """
    if boxes is None or txts is None or scores is None:
        return []

    offset_x, offset_y = origin
    out: list[Element] = []
    for polygon, text, score in zip(boxes, txts, scores):
        if score is None or float(score) < MIN_SCORE:
            continue
        cleaned = (text or "").strip()
        if not cleaned:
            continue
        left, top, right, bottom = polygon_to_bbox(polygon)
        if (bottom - top) < MIN_HEIGHT_PX:
            continue
        bbox = (left + offset_x, top + offset_y, right + offset_x, bottom + offset_y)
        out.append(
            Element(
                id=0,
                text=cleaned,
                source="ocr",
                role="text",
                region=classify_region(bbox, monitor),
                bbox_px=bbox,
            )
        )
    return out


def read_screen(image: np.ndarray, monitor: Monitor) -> tuple[list[Element], float]:
    """OCR one captured image.

    Returns:
        (elements, ocr_ms). On any OCR failure this returns an empty list rather
        than raising: a step with no OCR elements can still fall back to UIA
        elements or a keyboard hint, but a crash loses her whole task.
    """
    started = time.perf_counter()
    try:
        result = _get_engine()(image, text_score=MIN_SCORE)
    except Exception:
        ocr_ms = (time.perf_counter() - started) * 1000
        log.exception("OCR failed after %.0f ms; continuing without OCR elements", ocr_ms)
        return [], ocr_ms

    elements = elements_from_raw(
        getattr(result, "boxes", None),
        getattr(result, "txts", None),
        getattr(result, "scores", None),
        monitor,
        origin=(monitor.left, monitor.top),
    )
    ocr_ms = (time.perf_counter() - started) * 1000
    log.debug("OCR produced %d elements in %.0f ms", len(elements), ocr_ms)
    return elements, ocr_ms
