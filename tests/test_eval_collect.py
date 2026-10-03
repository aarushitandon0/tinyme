"""Tests for the eval collection tooling (P10).

These cover the parts that decide what ends up in the repo. ``eval/elements/*.json``
is committed and carries every word the screen was showing, so a shot that should
have been discarded is a text leak, not just bad data. The window-focus and
Windows-launching parts are not tested here -- they are driven by the screen and
are verified by running the collector.
"""

from __future__ import annotations

import json

import pytest

from app.elements import Element
from eval import capture_tool, collect


# --- the deny-list ---------------------------------------------------------


def elements_with(*texts: str) -> list[dict]:
    return [{"text": text} for text in texts]


def test_contamination_finds_a_fragment_case_insensitively():
    found = collect.contamination(
        elements_with("Downloads", "B.Tech_Computer Engineering.docx"),
        ["b.tech"],
    )
    assert found == ["b.tech"]


def test_contamination_is_empty_for_a_clean_capture():
    assert collect.contamination(elements_with("Downloads", "recipe notes"),
                                 ["b.tech", "researchpaper"]) == []


def test_contamination_matches_across_elements_but_not_across_the_join():
    """A fragment must be inside one element's text, not formed by two elements
    being next to each other -- otherwise the separator characters would make
    neighbouring words look like a match."""
    assert collect.contamination(elements_with("resear", "chpaper"), ["researchpaper"]) == []


def test_contamination_finds_every_fragment_present():
    found = collect.contamination(
        elements_with("B.Tech thing", "Network PiyaliDen"),
        ["b.tech", "network piyaliden", "not here"],
    )
    assert found == ["b.tech", "network piyaliden"]


def test_no_deny_list_file_means_no_fragments(tmp_path):
    assert collect.forbidden_fragments(tmp_path / "nope.txt") == []


def test_deny_list_skips_comments_and_blanks_and_lowercases(tmp_path):
    path = tmp_path / "forbidden.txt"
    path.write_text("# a comment\n\n  B.Tech  \nResearchPaper\n", encoding="utf-8")
    assert collect.forbidden_fragments(path) == ["b.tech", "researchpaper"]


# --- filenames -------------------------------------------------------------


def test_next_index_starts_at_one(tmp_path):
    assert capture_tool.next_index("explorer", tmp_path) == 1


def test_next_index_skips_what_is_already_there(tmp_path):
    for name in ("explorer_01.png", "explorer_02.png"):
        (tmp_path / name).touch()
    assert capture_tool.next_index("explorer", tmp_path) == 3


def test_next_index_fills_a_gap_left_by_a_discarded_shot(tmp_path):
    (tmp_path / "explorer_01.png").touch()
    (tmp_path / "explorer_03.png").touch()
    assert capture_tool.next_index("explorer", tmp_path) == 2


def test_next_index_ignores_other_apps(tmp_path):
    (tmp_path / "notepad_01.png").touch()
    assert capture_tool.next_index("explorer", tmp_path) == 1


# --- the frozen element format --------------------------------------------


def test_element_to_json_keeps_the_bbox_and_rounds_it():
    element = Element(id=7, text="Downloads", source="uia", role="ListItem",
                      region="left-middle", bbox_px=(100.04, 200.06, 300.0, 230.0))
    assert capture_tool.element_to_json(element) == {
        "id": 7,
        "text": "Downloads",
        "source": "uia",
        "role": "ListItem",
        "region": "left-middle",
        "bbox_px": [100.0, 200.1, 300.0, 230.0],
    }


def test_element_json_round_trips_through_the_eval_loader():
    """The two halves of the dataset must agree: whatever capture_tool writes,
    run_eval has to be able to rebuild into the same Element."""
    from eval.run_eval import _element_from_json

    original = Element(id=3, text='He said "hi" | there', source="ocr", role="text",
                       region="center", bbox_px=(1.0, 2.0, 3.0, 4.0))
    rebuilt = _element_from_json(json.loads(json.dumps(
        capture_tool.element_to_json(original))))
    assert rebuilt == original


# --- the screen list -------------------------------------------------------


def test_every_planned_screen_has_a_slug_that_can_be_a_filename():
    for screen in collect.screens():
        assert capture_tool.APP_SLUG.match(screen.app), screen.app


def test_planned_screens_cover_several_apps():
    """A system that only works in one app should be visible as such in the
    per-app table, which needs more than one app in the data."""
    apps = {screen.app for screen in collect.screens()}
    assert len(apps) >= 3


def test_explorer_screens_expect_a_folder_name_not_the_words_file_explorer():
    """CLAUDE.md rule 11: an Explorer window's title is the folder name. A screen
    waiting for the title "File Explorer" would wait forever."""
    for screen in collect.screens():
        if screen.window_class == "CabinetWClass":
            assert screen.expect_title
            assert screen.expect_title.casefold() != "file explorer"


@pytest.mark.parametrize("app", ["explorer", "settings"])
def test_screens_can_be_filtered_to_one_app(app):
    assert [screen for screen in collect.screens() if screen.app == app]
