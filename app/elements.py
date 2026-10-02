"""The element list: the model's whole world (MASTERSPEC 5.2).

Two rules from CLAUDE.md shape this module:

* Rule 1 - the model never sees coordinates. ``bbox_px`` stays in Python, and
  ``render_for_model`` is the only function that produces prompt text.
* Rule 9 - ``mss`` gives physical pixels, Qt wants logical pixels, and
  ``to_logical`` is the single place that converts. Nothing else divides by a
  device pixel ratio.

The bbox field is named ``bbox_px`` rather than the spec's ``bbox`` so that
every use site reads as physical pixels; MASTERSPEC 14 lists DPI mismatch as a
top risk, and the suffix makes the mistake visible.

A bbox is ``(left, top, right, bottom)`` in physical pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Literal, Sequence

Bbox = tuple[float, float, float, float]

#: The nine regions, in reading order. Names match the example in MASTERSPEC 5.2
#: (note the middle row is "left-middle" / "center" / "right-middle").
REGIONS: tuple[str, ...] = (
    "top-left", "top-center", "top-right",
    "left-middle", "center", "right-middle",
    "bottom-left", "bottom-center", "bottom-right",
)

#: Elements whose centres sit within this many physical pixels vertically are
#: treated as one visual row, so OCR jitter does not scramble reading order.
ROW_BAND_PX = 24

#: MASTERSPEC 5.2 caps the list at ~80 elements to keep the prompt short.
MAX_ELEMENTS = 80

#: Longest text we put in a prompt line. Keeps the prompt small on a CPU laptop.
MAX_TEXT_CHARS = 60


@dataclass(frozen=True)
class Monitor:
    """Primary-monitor geometry in physical pixels, as ``mss`` reports it."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass(frozen=True)
class Element:
    """One thing on screen the model may choose.

    ``id`` is assigned by :func:`number_elements` and is what the model returns
    as ``target_id``. ``bbox_px`` never reaches the model.
    """

    id: int
    text: str
    source: Literal["ocr", "uia"]
    role: str
    region: str
    bbox_px: Bbox


def _centre(bbox: Bbox) -> tuple[float, float]:
    left, top, right, bottom = bbox
    return (left + right) / 2.0, (top + bottom) / 2.0


def classify_region(bbox: Bbox, monitor: Monitor) -> str:
    """Return which of the nine regions the bbox's centre falls in.

    The centre is used, not a corner, so a wide element is named by where it
    actually sits. Coordinates outside the monitor are clamped: a stray OCR box
    should not crash a step. A boundary belongs to the later region, so on a
    1920-wide monitor x=640 is centre-column, not left.
    """
    cx, cy = _centre(bbox)

    def third(value: float, origin: int, size: int) -> int:
        if size <= 0:  # Degenerate geometry; treat the screen as one cell.
            return 0
        index = int((value - origin) * 3 // size)
        return max(0, min(2, index))

    col = third(cx, monitor.left, monitor.width)
    row = third(cy, monitor.top, monitor.height)
    return REGIONS[row * 3 + col]


def _reading_order(elements: Sequence[Element]) -> list[Element]:
    """Sort top-to-bottom then left-to-right, banding near-equal rows together.

    Rows are clustered by sweeping down rather than by flooring to a fixed grid,
    so two elements a pixel apart never land in different bands.
    """
    indexed = list(enumerate(elements))
    # Original index is the final tie-break, which keeps this deterministic for
    # elements that share a position.
    by_y = sorted(indexed, key=lambda pair: (_centre(pair[1].bbox_px)[1], pair[0]))

    ordered: list[Element] = []
    band: list[tuple[int, Element]] = []
    band_top: float | None = None

    def flush() -> None:
        band.sort(key=lambda pair: (_centre(pair[1].bbox_px)[0], pair[0]))
        ordered.extend(element for _, element in band)
        band.clear()

    for index, element in by_y:
        cy = _centre(element.bbox_px)[1]
        if band_top is None or cy - band_top < ROW_BAND_PX:
            if band_top is None:
                band_top = cy
        else:
            flush()
            band_top = cy
        band.append((index, element))
    flush()
    return ordered


def number_elements(elements: Iterable[Element]) -> list[Element]:
    """Return copies numbered 1..N in reading order. Inputs are not mutated."""
    ordered = _reading_order(list(elements))
    return [replace(element, id=number) for number, element in enumerate(ordered, start=1)]


def _contains(rect: Bbox | None, bbox: Bbox) -> bool:
    """True if the bbox's centre lies inside the rect."""
    if rect is None:
        return False
    cx, cy = _centre(bbox)
    left, top, right, bottom = rect
    return left <= cx <= right and top <= cy <= bottom


def cap_elements(
    elements: Iterable[Element],
    limit: int = MAX_ELEMENTS,
    taskbar_rect: Bbox | None = None,
    foreground_rect: Bbox | None = None,
) -> list[Element]:
    """Trim to ``limit`` elements, keeping taskbar and foreground ones first.

    MASTERSPEC 5.2: cap at ~80, prioritising the foreground window and the
    taskbar. The taskbar outranks the foreground window because its icons are
    the only way to start scene A, and they are the elements OCR cannot see.
    The survivors come back in reading order so numbering stays sensible.
    """
    ordered = _reading_order(list(elements))
    if limit <= 0:
        return []
    if len(ordered) <= limit:
        return ordered

    def priority(element: Element) -> int:
        if _contains(taskbar_rect, element.bbox_px):
            return 2
        if _contains(foreground_rect, element.bbox_px):
            return 1
        return 0

    ranked = sorted(
        enumerate(ordered),
        key=lambda pair: (-priority(pair[1]), pair[0]),
    )
    kept = sorted(ranked[:limit], key=lambda pair: pair[0])
    return [element for _, element in kept]


def to_logical(bbox: Bbox, dpr: float) -> Bbox:
    """Convert a physical-pixel bbox to Qt's logical pixels (MASTERSPEC 5.6).

    This is the one and only conversion point (CLAUDE.md rule 9). Floats come
    back so the caller decides how to round when painting.

    Raises:
        ValueError: if ``dpr`` is not positive, which means we failed to read
            the screen. Better to fail loudly than to draw the circle nowhere.
    """
    if dpr <= 0:
        raise ValueError(f"devicePixelRatio must be positive, got {dpr!r}")
    left, top, right, bottom = bbox
    return (left / dpr, top / dpr, right / dpr, bottom / dpr)


def _safe_text(text: str) -> str:
    """Flatten OCR text to one safe prompt column.

    Screen text is untrusted input. Newlines are collapsed so it cannot forge
    extra element lines, pipes and double quotes are swapped out so it cannot
    forge extra columns, and the result is truncated to keep the prompt short.
    """
    flattened = " ".join(text.split())
    flattened = flattened.replace("|", "/").replace('"', "'")
    if len(flattened) > MAX_TEXT_CHARS:
        flattened = flattened[: MAX_TEXT_CHARS - 1].rstrip() + "…"
    return flattened


def render_for_model(elements: Sequence[Element]) -> str:
    """Render the element list exactly as MASTERSPEC 5.2 shows it.

    ``17 | "Downloads" | uia:ListItem | left-middle``

    Coordinates are never included. This is the only function that turns
    elements into prompt text.
    """
    return "\n".join(
        f'{element.id} | "{_safe_text(element.text)}" | '
        f"{element.source}:{element.role} | {element.region}"
        for element in elements
    )
