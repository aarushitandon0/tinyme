"""Tests for the capture-exclusion probe matcher.

Regression cover for a real bug: the first version of this check used the probe
text "TINYME OVERLAY CHECK" and ``fuzz.partial_ratio(...) >= 85``, which matched
ordinary screen text. On a live screen it reported that the overlay had leaked
into the screenshot when it had not, and that false positive forces the slow
hide->capture->show path for the whole session.

Only the pure matcher is tested here; constructing the widget needs a display.
"""
import pytest

from app.elements import Element
from app.overlay import EXCLUSION_PROBE, probe_leaked


def els(*texts):
    return [
        Element(id=i, text=t, source="ocr", role="text", region="center",
                bbox_px=(0, 0, 10, 10))
        for i, t in enumerate(texts, start=1)
    ]


def test_finds_the_probe_when_the_overlay_really_leaked():
    assert probe_leaked(els("something", EXCLUSION_PROBE, "else")) is True


def test_finds_the_probe_inside_a_longer_ocr_line():
    assert probe_leaked(els(f"noise {EXCLUSION_PROBE} noise")) is True


def test_tolerates_a_few_ocr_character_slips():
    # OCR commonly confuses O/0 and I/1; the check must still fire.
    assert probe_leaked(els(EXCLUSION_PROBE.replace("O", "0").replace("I", "1"))) is True


def test_no_false_positive_on_an_empty_screen():
    assert probe_leaked(els()) is False


def test_no_false_positive_on_ordinary_screen_text():
    # Generic Windows shell and Explorer chrome, invented rather than taken
    # from anyone's screen (CLAUDE.md: no real user data in the repo). None of
    # them is the probe, so none may match.
    assert probe_leaked(els(
        "Downloads", "File Explorer", "Date modified", "Type", "Size",
        "Quick access", "Favourites", "Share", "Close", "Minimise",
        "This PC", "Network", "Documents", "Pictures", "15 items",
        "electricity bill.pdf", "New folder", "Show more options",
        "X", "Q", "+", "...", "", "   ",
    )) is False


def test_short_labels_cannot_fuzzy_match_a_long_probe():
    # The old implementation scored these highly via partial_ratio.
    assert probe_leaked(els("X", "QZ", "PRO", "ME", "7", "TINY")) is False


def test_generic_probe_text_is_not_used_as_the_default():
    # The default must stay distinctive, which is what makes the strict
    # matching above safe.
    assert "XQZ7" in EXCLUSION_PROBE
    assert "CHECK" not in EXCLUSION_PROBE.upper().replace("XQZ7", "")


# --- choosing the mark -----------------------------------------------------
# ``choose_shape`` decides between a circle and a rectangle from the target's
# proportions alone. It is pure and takes physical pixels plus the scale
# factor, so it is testable without a screen -- which matters, because getting
# it wrong at 125% scaling is exactly the class of bug MASTERSPEC 14 warns
# about.

from app.overlay import MarkShape, choose_shape


def box(width, height, dpr=1.0):
    """A bbox of the given LOGICAL size, expressed in physical pixels."""
    return (100 * dpr, 100 * dpr, (100 + width) * dpr, (100 + height) * dpr)


class TestChooseShape:
    def test_a_list_row_gets_a_frame(self):
        # An Explorer sidebar row: wide, short, and genuinely a rectangle.
        assert choose_shape(box(220, 24), 1.0) is MarkShape.FRAME

    def test_a_word_gets_a_ring(self):
        # A word in running text: too small to frame without looking fussy.
        assert choose_shape(box(48, 18), 1.0) is MarkShape.RING

    def test_a_square_icon_gets_a_ring(self):
        # A taskbar icon is a square: a frame on it is indistinguishable from
        # the icon's own hover highlight, so the ring stays.
        assert choose_shape(box(40, 40), 1.0) is MarkShape.RING

    def test_a_wide_button_gets_a_frame(self):
        assert choose_shape(box(160, 40), 1.0) is MarkShape.FRAME

    def test_a_thin_sliver_gets_a_ring(self):
        # Wide but only a few pixels tall: OCR noise, not a row.
        assert choose_shape(box(300, 6), 1.0) is MarkShape.RING

    def test_the_decision_is_made_in_logical_pixels(self):
        # The same on-screen row at 150% scaling arrives 1.5x bigger in
        # physical pixels. It is the same row and must get the same mark.
        at_100 = choose_shape(box(220, 24, dpr=1.0), 1.0)
        at_150 = choose_shape(box(220, 24, dpr=1.5), 1.5)
        assert at_100 is at_150 is MarkShape.FRAME

    def test_a_row_too_small_at_high_scaling_is_still_judged_by_its_real_size(self):
        # 40x10 logical is below the frame floor whatever the scale factor.
        assert choose_shape(box(40, 10, dpr=1.5), 1.5) is MarkShape.RING

    def test_a_degenerate_bbox_does_not_raise(self):
        assert choose_shape((10, 10, 10, 10), 1.0) is MarkShape.RING

    def test_a_backwards_bbox_does_not_raise(self):
        assert choose_shape((200, 200, 100, 100), 1.0) in (
            MarkShape.RING, MarkShape.FRAME
        )


# --- the callout's tail ----------------------------------------------------
# ``_tail_reaches`` is a staticmethod over plain geometry, so it tests without
# a display even though it lives on the widget. It guards a real failure mode:
# near a screen edge the callout is clamped sideways, and a tail pinned to its
# own corner points at empty screen -- worse than no tail, because it points
# somewhere wrong. False here means "draw a leader line instead".

from PySide6.QtCore import QPointF, QRectF

from app.overlay import Overlay

reaches = Overlay._tail_reaches


class TestTailReaches:
    card = QRectF(100, 200, 240, 44)

    def test_a_mark_under_the_middle_of_the_card_reaches(self):
        assert reaches(self.card, QPointF(220, 180)) is True

    def test_a_mark_well_off_to_the_left_does_not_reach(self):
        assert reaches(self.card, QPointF(40, 180)) is False

    def test_a_mark_well_off_to_the_right_does_not_reach(self):
        assert reaches(self.card, QPointF(500, 180)) is False

    def test_a_mark_just_inside_the_flat_edge_reaches(self):
        # The tail may not grow out of a rounded corner, so the usable span is
        # the card minus its radius and the tail's own half-width at each end.
        assert reaches(self.card, QPointF(125, 180)) is True

    def test_a_very_narrow_card_has_no_flat_edge_to_hang_a_tail_from(self):
        sliver = QRectF(100, 200, 12, 44)
        assert reaches(sliver, QPointF(106, 180)) is False

    def test_a_card_pushed_far_from_the_mark_cannot_bridge_the_gap(self):
        # The mark runs off the bottom of the screen, so the card was clamped
        # back inside it and now sits a long way above where it should. A 7 px
        # tail does not reach across that.
        assert reaches(self.card, QPointF(220, 60)) is False

    def test_the_usual_gap_is_fine(self):
        # Normal placement: the card sits LABEL_GAP below the mark.
        assert reaches(self.card, QPointF(220, 200 - 16), below=True) is True

    def test_the_gap_is_measured_from_the_edge_that_faces_the_mark(self):
        # Flipped above the mark, the card's BOTTOM faces it; measuring from
        # the top would call this a huge gap and suppress a good tail.
        below_card = QRectF(100, 100, 240, 44)
        assert reaches(below_card, QPointF(220, 160), below=False) is True
        assert reaches(below_card, QPointF(220, 160), below=True) is False


# --- the step counter getting out of the way -------------------------------
# The counter sits bottom-centre, which is exactly where the callout for a
# target low on the screen lands. ``_lift_clear`` is the pure geometry that
# moves it; it is a staticmethod, so no display is needed.

lift = Overlay._lift_clear


class TestLiftClear:
    counter = QRectF(600, 760, 160, 34)

    def test_nothing_in_the_way_leaves_it_alone(self):
        assert lift(self.counter, []) == self.counter

    def test_something_far_away_leaves_it_alone(self):
        assert lift(self.counter, [QRectF(0, 0, 100, 100)]) == self.counter

    def test_it_climbs_above_an_overlapping_callout(self):
        callout = QRectF(560, 740, 300, 44)
        moved = lift(self.counter, [callout])
        assert moved.bottom() <= callout.top()
        assert moved.left() == self.counter.left()      # straight up, never sideways
        assert moved.width() == self.counter.width()

    def test_it_climbs_past_a_stack_of_things(self):
        boxes = [QRectF(560, 740, 300, 44), QRectF(560, 680, 300, 44)]
        moved = lift(self.counter, boxes)
        assert all(not b.intersects(moved) for b in boxes)

    def test_a_full_screen_leaves_it_where_it_started(self):
        # Nothing to climb to: better half-covered than off the top edge.
        wall = QRectF(0, 0, 1920, 1080)
        assert lift(self.counter, [wall]) == self.counter


# --- Tiny Me standing beside the callout ------------------------------------
# She overlaps the card's left edge, so she costs the card room on two sides:
# ``_mascot_room`` is asked for that budget *before* the card is placed, and
# ``_mascot_box`` says where she ends up once it has been. Both are pure
# geometry, so no display is needed. The rule they enforce together is that she
# is dropped, never sliced -- half a mascot is worse than none.

from app import pixel  # noqa: E402
from app.overlay import (  # noqa: E402
    MASCOT,
    MASCOT_MIN_BOX,
    MASCOT_OVERHANG,
    MASCOT_SCALE,
    SCREEN_MARGIN,
)

room = Overlay._mascot_room
mascot_box = Overlay._mascot_box


class TestMascotRoom:
    def test_the_two_halves_add_up_to_her_whole_width(self):
        inside, outside = room(44.0)
        cols, rows = pixel.size(MASCOT)
        assert inside + outside == pytest.approx(44.0 * MASCOT_SCALE * cols / rows)

    def test_the_overhang_is_the_part_outside_the_card(self):
        inside, outside = room(44.0)
        assert outside / (inside + outside) == pytest.approx(MASCOT_OVERHANG)

    def test_a_taller_card_wants_a_bigger_mascot(self):
        assert sum(room(60.0)) > sum(room(44.0))

    def test_an_unknown_mascot_costs_nothing(self, monkeypatch):
        # If the sprite were ever renamed out from under the overlay, the
        # callout must lay out exactly as it did before she existed.
        monkeypatch.setattr("app.overlay.MASCOT", "penguin")
        assert Overlay._mascot_room(44.0) == (0.0, 0.0)


class TestMascotBox:
    # A roomy card in the middle of a 1920x1080 screen: she fits easily.
    card = QRectF(700, 500, 320, 44)

    def test_she_stands_on_the_cards_baseline(self):
        box = mascot_box(self.card)
        assert box.bottom() == pytest.approx(self.card.bottom())

    def test_she_is_taller_than_the_card_so_she_leans_over_it(self):
        assert mascot_box(self.card).height() > self.card.height()

    def test_she_overlaps_the_cards_left_edge(self):
        box = mascot_box(self.card)
        assert box.left() < self.card.left() < box.right()

    def test_her_width_matches_the_room_that_was_budgeted_for_her(self):
        # If these drifted apart the words would be inset by the wrong amount
        # and sit either under her or in a gap beside her.
        box = mascot_box(self.card)
        inside, outside = room(self.card.height())
        assert box.width() <= inside + outside + 1.0

    def test_her_pixels_are_square(self):
        box = mascot_box(self.card)
        cols, rows = pixel.size(MASCOT)
        assert box.width() / cols == pytest.approx(box.height() / rows)

    def test_a_narrow_card_drops_her(self):
        narrow = QRectF(700, 500, MASCOT_MIN_BOX - 1, 44)
        assert mascot_box(narrow).isEmpty()

    def test_a_card_against_the_left_edge_drops_her(self):
        # There is nowhere for her to stand, and standing her on top of the
        # words instead would be worse than not drawing her.
        pinned = QRectF(SCREEN_MARGIN, 500, 320, 44)
        assert mascot_box(pinned).isEmpty()

    def test_a_card_near_the_top_of_the_screen_drops_her(self):
        # She is taller than the card, so a card this high would slice her
        # head off at the top of the screen.
        high = QRectF(700, SCREEN_MARGIN + 2, 320, 44)
        assert mascot_box(high).isEmpty()

    def test_she_never_runs_off_the_top_of_the_screen(self):
        for top in range(SCREEN_MARGIN, 400, 7):
            box = mascot_box(QRectF(700, top, 320, 44))
            assert box.isEmpty() or box.top() >= SCREEN_MARGIN

    def test_an_unknown_mascot_is_simply_not_drawn(self, monkeypatch):
        monkeypatch.setattr("app.overlay.MASCOT", "penguin")
        assert Overlay._mascot_box(self.card).isEmpty()
