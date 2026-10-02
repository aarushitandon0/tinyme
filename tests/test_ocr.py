"""Tests for the pure part of app.ocr: filters and polygon conversion.

No OCR models are loaded here; ``elements_from_raw`` takes raw arrays so the
MASTERSPEC 5.2 filter rules can be tested on their own.
"""
import numpy as np

from app.ocr import MIN_HEIGHT_PX, MIN_SCORE, elements_from_raw, polygon_to_bbox
from app.elements import Monitor

MON = Monitor(left=0, top=0, width=1920, height=1080)


def quad(left, top, right, bottom):
    """A RapidOCR-shaped 4-corner polygon."""
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


class TestPolygonToBbox:
    def test_axis_aligned_quad(self):
        assert polygon_to_bbox(quad(10, 20, 110, 45)) == (10.0, 20.0, 110.0, 45.0)

    def test_rotated_quad_becomes_its_bounding_box(self):
        skewed = [[12, 20], [110, 18], [112, 44], [14, 46]]
        assert polygon_to_bbox(skewed) == (12.0, 18.0, 112.0, 46.0)

    def test_accepts_a_numpy_array(self):
        assert polygon_to_bbox(np.array(quad(0, 0, 5, 9), dtype=np.float32)) == (
            0.0, 0.0, 5.0, 9.0,
        )


class TestElementsFromRaw:
    def test_happy_path(self):
        boxes = np.array([quad(100, 500, 220, 530)], dtype=float)
        out = elements_from_raw(boxes, ["Downloads"], [0.97], MON)
        assert len(out) == 1
        assert out[0].text == "Downloads"
        assert out[0].source == "ocr"
        assert out[0].role == "text"
        assert out[0].region == "left-middle"
        assert out[0].bbox_px == (100.0, 500.0, 220.0, 530.0)

    def test_ids_are_left_unassigned(self):
        boxes = np.array([quad(0, 0, 50, 20)], dtype=float)
        assert elements_from_raw(boxes, ["a"], [0.9], MON)[0].id == 0

    def test_drops_low_confidence_boxes(self):
        boxes = np.array([quad(0, 0, 50, 20), quad(0, 40, 50, 60)], dtype=float)
        out = elements_from_raw(boxes, ["keep", "drop"], [MIN_SCORE, MIN_SCORE - 0.01], MON)
        assert [e.text for e in out] == ["keep"]

    def test_drops_boxes_shorter_than_the_minimum_height(self):
        boxes = np.array(
            [quad(0, 0, 50, MIN_HEIGHT_PX), quad(0, 40, 50, 40 + MIN_HEIGHT_PX - 1)],
            dtype=float,
        )
        out = elements_from_raw(boxes, ["keep", "drop"], [0.9, 0.9], MON)
        assert [e.text for e in out] == ["keep"]

    def test_drops_empty_and_whitespace_text(self):
        boxes = np.array([quad(0, 0, 50, 20), quad(0, 40, 50, 60)], dtype=float)
        out = elements_from_raw(boxes, ["   ", "real"], [0.9, 0.9], MON)
        assert [e.text for e in out] == ["real"]

    def test_strips_surrounding_whitespace(self):
        boxes = np.array([quad(0, 0, 50, 20)], dtype=float)
        assert elements_from_raw(boxes, ["  Downloads \n"], [0.9], MON)[0].text == "Downloads"

    def test_origin_offset_is_applied(self):
        # The image starts at the monitor's origin, so a monitor left of the
        # primary one must produce negative screen-space coordinates.
        mon = Monitor(left=-1920, top=200, width=1920, height=1080)
        boxes = np.array([quad(10, 10, 60, 30)], dtype=float)
        out = elements_from_raw(boxes, ["x"], [0.9], mon, origin=(mon.left, mon.top))
        assert out[0].bbox_px == (-1910.0, 210.0, -1860.0, 230.0)

    def test_nothing_detected_returns_empty(self):
        assert elements_from_raw(None, None, None, MON) == []
        assert elements_from_raw(np.empty((0, 4, 2)), (), (), MON) == []

    def test_none_score_is_dropped_not_crashed(self):
        boxes = np.array([quad(0, 0, 50, 20)], dtype=float)
        assert elements_from_raw(boxes, ["x"], [None], MON) == []
