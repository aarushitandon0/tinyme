"""Tests for the capture-exclusion probe matcher.

Regression cover for a real bug: the first version of this check used the probe
text "TINYME OVERLAY CHECK" and ``fuzz.partial_ratio(...) >= 85``, which matched
ordinary screen text. On a live screen it reported that the overlay had leaked
into the screenshot when it had not, and that false positive forces the slow
hide->capture->show path for the whole session.

Only the pure matcher is tested here; constructing the widget needs a display.
"""
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
