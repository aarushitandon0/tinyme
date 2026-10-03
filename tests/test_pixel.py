"""Tests for the pixel-art sprites and their integer scaling.

Two things are worth holding still here.

The *grids* must stay rectangular and stay inside the palette. Pixel art is
edited by typing characters into a block of strings, and the easy mistakes are
a row one character short and a typo'd colour key -- neither of which raises,
both of which quietly corrupt the drawing.

The *scaling* must stay integer. A sprite drawn at 3.7 device pixels per art
pixel has rows of uneven thickness and stops reading as pixel art, which is the
whole reason this module exists separately from :mod:`app.icons`.

No display is needed: ``size`` and ``fit`` are pure geometry.
"""
import pytest
from PySide6.QtCore import QRectF

from app import pixel


class TestGrids:
    @pytest.mark.parametrize("name", sorted(pixel.SPRITES))
    def test_every_row_is_the_same_width(self, name):
        rows = pixel.SPRITES[name]
        assert len({len(row) for row in rows}) == 1, f"{name} has a ragged row"

    @pytest.mark.parametrize("name", sorted(pixel.SPRITES))
    def test_every_character_is_in_the_palette(self, name):
        used = set("".join(pixel.SPRITES[name]))
        unknown = used - set(pixel.PALETTE) - {"."}
        assert not unknown, f"{name} uses undefined colours: {sorted(unknown)}"

    @pytest.mark.parametrize("name", sorted(pixel.SPRITES))
    def test_the_sprite_is_not_blank(self, name):
        assert set("".join(pixel.SPRITES[name])) != {"."}

    def test_the_two_characters_the_ui_asks_for_are_present(self):
        # app.overlay and app.ui name these directly.
        assert {"girl", "cat"} <= set(pixel.SPRITES)

    def test_size_reports_columns_then_rows(self):
        rows = pixel.SPRITES["girl"]
        assert pixel.size("girl") == (len(rows[0]), len(rows))

    def test_an_unknown_sprite_has_no_size_rather_than_raising(self):
        assert pixel.size("penguin") == (0, 0)


class TestFit:
    def test_the_scale_is_a_whole_number_of_pixels_per_art_pixel(self):
        cols, rows = pixel.size("girl")
        # 400/16 = 25 exactly down one axis, 331/18 = 18.4 down the other, so
        # the short axis wins and floors to 18.
        box = pixel.fit("girl", QRectF(0, 0, 400, 331))
        assert box.width() / cols == pytest.approx(18.0)
        assert box.height() / rows == pytest.approx(18.0)

    def test_the_drawn_box_never_exceeds_the_rect_it_was_offered(self):
        offered = QRectF(10, 10, 103, 97)
        box = pixel.fit("cat", offered)
        assert box.width() <= offered.width()
        assert box.height() <= offered.height()

    def test_it_is_centred_in_the_rect_it_was_offered(self):
        offered = QRectF(10, 10, 103, 97)
        box = pixel.fit("cat", offered)
        assert box.center().x() == pytest.approx(offered.center().x())
        assert box.center().y() == pytest.approx(offered.center().y())

    def test_aspect_ratio_is_kept(self):
        cols, rows = pixel.size("girl")
        box = pixel.fit("girl", QRectF(0, 0, 500, 120))
        assert box.width() / box.height() == pytest.approx(cols / rows)

    def test_a_rect_too_small_for_one_pixel_each_still_draws_something(self):
        # Floor would give 0 and paint nothing; the scale is clamped to 1 so a
        # tiny sprite is tiny rather than absent.
        box = pixel.fit("girl", QRectF(0, 0, 4, 4))
        assert box.width() > 0 and box.height() > 0

    def test_an_unknown_sprite_fits_to_nothing_rather_than_raising(self):
        assert pixel.fit("penguin", QRectF(0, 0, 100, 100)).isEmpty()

    def test_a_degenerate_rect_fits_to_nothing(self):
        assert pixel.fit("girl", QRectF(0, 0, 0, 50)).isEmpty()
