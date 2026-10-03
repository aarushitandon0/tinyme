"""Tests for app.brain: prompt shape, validation, and the retry path.

Ollama is the only thing mocked, and only at the ``client.chat`` boundary: the
prompt builder, the pydantic schema, the validation rules and the retry logic
all run for real. A fake that returned a ready-made StepPlan would test nothing.
"""
import json
from dataclasses import dataclass

import pytest

from app import config
from app.brain import (
    MAX_TEACH_NOTES,
    PlanInvalid,
    build_messages,
    build_system_prompt,
    fallback_plan,
    load_teach_notes,
    matching_notes,
    notes_for_goal,
    plan_step,
    validate_plan,
)
from app.elements import Element, number_elements

ELEMENTS = number_elements([
    Element(id=0, text="File Explorer", source="uia", role="Button",
            region="bottom-left", bbox_px=(40, 1040, 80, 1070)),
    Element(id=0, text="Downloads", source="uia", role="TreeItem",
            region="left-middle", bbox_px=(60, 500, 180, 524)),
    Element(id=0, text="bill.pdf", source="ocr", role="text",
            region="center", bbox_px=(700, 400, 820, 424)),
])
VALID_IDS = {element.id for element in ELEMENTS}


def plan_json(**overrides) -> str:
    """A valid model reply, overridable field by field."""
    body = {
        "instruction": "Click Downloads in the left panel",
        "target_id": 2,
        "success_check": {"type": "window_title_contains", "value": "Downloads"},
        "goal_reached": False,
        "cannot_see_it": False,
        "hint_if_missing": "",
    }
    body.update(overrides)
    return json.dumps(body)


@dataclass
class FakeResponse:
    """Mirrors the ollama 0.6.3 ChatResponse fields brain.py reads."""

    content: str
    prompt_eval_count: int = 812
    eval_count: int = 47

    @property
    def message(self):
        return type("Message", (), {"content": self.content})()


class FakeClient:
    """Returns the queued replies in order and records every call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


class ExplodingClient:
    """Stands in for an Ollama server that is not running."""

    def __init__(self, exc):
        self.exc = exc
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        raise self.exc


# --- the prompt ------------------------------------------------------------


class TestSystemPrompt:
    def test_names_the_configured_language(self):
        assert "Marathi" in build_system_prompt(language="Marathi")

    def test_forbids_asking_for_passwords(self):
        assert "password" in build_system_prompt(language="English").lower()

    def test_tells_the_model_to_use_only_listed_ids(self):
        prompt = build_system_prompt(language="English")
        assert "target_id" in prompt
        assert "cannot_see_it" in prompt


class TestBuildMessages:
    def test_carries_goal_history_and_elements(self):
        messages = build_messages(
            goal="find my bill",
            elements=ELEMENTS,
            history=["Click the folder icon"],
        )
        user = messages[-1]["content"]
        assert "find my bill" in user
        assert "Click the folder icon" in user
        assert '2 | "Downloads" | uia:TreeItem | left-middle' in user

    def test_no_coordinates_reach_the_prompt(self):
        """CLAUDE.md rule 1: bboxes stay in Python."""
        messages = build_messages(goal="find my bill", elements=ELEMENTS)
        blob = " ".join(message["content"] for message in messages)
        for forbidden in ("1040", "1070", "524", "820"):
            assert forbidden not in blob

    def test_teach_notes_are_included_when_given(self):
        messages = build_messages(
            goal="print this",
            elements=ELEMENTS,
            teach_notes=["Her printer is called HP DeskJet 2300"],
        )
        assert "HP DeskJet 2300" in messages[-1]["content"]

    def test_history_is_omitted_when_empty(self):
        user = build_messages(goal="x", elements=ELEMENTS)[-1]["content"]
        assert "already done" not in user


class TestTeachNotes:
    """Her setup notes: loading them, and choosing which ones are relevant (P8).

    The point of filtering is not prompt size -- the whole file would fit. It is
    that an irrelevant note is a thing the model can anchor on: tell it about
    her printer while she is trying to zoom a document and it may well suggest
    printing.
    """

    NOTES = [
        "Her printer is called Office Printer and lives in the back room",
        "She uses Microsoft Edge, not Chrome",
        "Photos from her phone end up in Pictures, in a folder named Camera Roll",
        "Scanned documents go in Documents, in a folder named Scans",
    ]

    def test_loads_one_fact_per_line(self, tmp_path):
        path = tmp_path / "her_setup.txt"
        path.write_text(
            "# a comment\n\nHer printer is Office Printer\n   \nShe uses Edge\n",
            encoding="utf-8",
        )
        assert load_teach_notes(path) == ["Her printer is Office Printer",
                                         "She uses Edge"]

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """The normal case on a fresh checkout: nobody has taught it anything."""
        assert load_teach_notes(tmp_path / "nothing.txt") == []

    def test_picks_the_note_about_what_she_asked(self):
        chosen = matching_notes("where do my phone photos go", self.NOTES)
        assert chosen
        assert "Camera Roll" in chosen[0]

    def test_drops_notes_about_other_things(self):
        chosen = matching_notes("where do my phone photos go", self.NOTES)
        assert all("printer" not in note.casefold() for note in chosen)

    def test_an_unrelated_goal_takes_nothing(self):
        assert matching_notes("make this text bigger", self.NOTES) == []

    def test_her_word_need_not_be_the_notes_word(self):
        """"print this page" is about the printer note. Word-level fuzziness."""
        chosen = matching_notes("print this page", self.NOTES)
        assert chosen and "Office Printer" in chosen[0]

    def test_a_note_about_a_moved_folder_reaches_the_download_goal(self):
        """Why this feature exists: ``~/Downloads`` is not where hers is."""
        notes = ["Her Downloads folder has been moved to the D drive"]
        assert matching_notes("i cant find the file i downloaded", notes) == notes

    def test_the_best_match_comes_first(self):
        chosen = matching_notes("open the scans folder", self.NOTES)
        assert "Scans" in chosen[0]

    def test_never_more_than_three(self):
        many = [f"She keeps her photos in folder number {n}" for n in range(10)]
        assert len(matching_notes("where are my photos", many)) == MAX_TEACH_NOTES

    def test_an_empty_goal_takes_nothing(self):
        assert matching_notes("", self.NOTES) == []

    def test_notes_for_goal_reads_and_filters(self, tmp_path):
        path = tmp_path / "her_setup.txt"
        path.write_text("\n".join(self.NOTES), encoding="utf-8")
        chosen = notes_for_goal("where do my phone photos go", path)
        assert chosen and "Camera Roll" in chosen[0]

    def test_the_shipped_example_file_is_loadable_and_dummy(self):
        """CLAUDE.md: the file in the repo carries dummy content only.

        A real printer name or folder path landing in git is a privacy failure,
        and the example file is exactly where one would land by accident.
        """
        from pathlib import Path

        shipped = Path(__file__).resolve().parents[1] / "notes" / "her_setup.txt"
        lines = load_teach_notes(shipped)
        assert lines, "the example file should have some dummy lines in it"
        blob = shipped.read_text(encoding="utf-8")
        assert "DUMMY" in blob.upper()


# --- validation ------------------------------------------------------------


class TestValidatePlan:
    def test_accepts_an_id_from_the_list(self):
        plan = validate_plan(plan_json(target_id=2), VALID_IDS)
        assert plan.target_id == 2
        assert plan.success_check.value == "Downloads"

    def test_rejects_an_id_not_in_the_list(self):
        with pytest.raises(PlanInvalid) as caught:
            validate_plan(plan_json(target_id=99), VALID_IDS)
        assert "99" in str(caught.value)

    def test_allows_a_missing_id_when_the_goal_is_reached(self):
        plan = validate_plan(plan_json(target_id=0, goal_reached=True), VALID_IDS)
        assert plan.goal_reached is True

    def test_allows_a_missing_id_when_it_cannot_see_it(self):
        plan = validate_plan(
            plan_json(target_id=0, cannot_see_it=True,
                      hint_if_missing="Scroll down a little"),
            VALID_IDS,
        )
        assert plan.hint_if_missing == "Scroll down a little"

    def test_rejects_an_instruction_over_90_characters(self):
        with pytest.raises(PlanInvalid):
            validate_plan(plan_json(instruction="x" * 91), VALID_IDS)

    def test_rejects_an_unknown_success_check_type(self):
        with pytest.raises(PlanInvalid):
            validate_plan(
                plan_json(success_check={"type": "vibes", "value": ""}), VALID_IDS
            )

    def test_rejects_text_that_is_not_json(self):
        with pytest.raises(PlanInvalid):
            validate_plan("Sure! Here is the step you asked for.", VALID_IDS)

    def test_rejects_an_empty_reply(self):
        with pytest.raises(PlanInvalid):
            validate_plan("", VALID_IDS)


# --- the call, with retry and fallback ------------------------------------


class TestPlanStep:
    def test_valid_first_time_makes_one_call(self):
        client = FakeClient(FakeResponse(plan_json(target_id=2)))
        result = plan_step("find my bill", ELEMENTS, client=client)

        assert result.plan.target_id == 2
        assert result.retried is False
        assert result.fell_back is False
        assert len(client.calls) == 1

    def test_reports_timing_and_token_counts(self):
        client = FakeClient(
            FakeResponse(plan_json(), prompt_eval_count=812, eval_count=47)
        )
        result = plan_step("find my bill", ELEMENTS, client=client)

        assert result.prompt_tokens == 812
        assert result.output_tokens == 47
        assert result.elapsed_ms >= 0

    def test_sends_the_schema_at_temperature_zero(self):
        """MASTERSPEC 5.3: structured output, temperature 0, thinking off."""
        client = FakeClient(FakeResponse(plan_json()))
        plan_step("find my bill", ELEMENTS, client=client)

        call = client.calls[0]
        assert call["format"]["properties"]["target_id"]
        assert call["options"]["temperature"] == 0
        assert call["keep_alive"] == config.KEEP_ALIVE
        assert call["think"] is False

    def test_retries_once_with_the_validation_error_appended(self):
        client = FakeClient(
            FakeResponse(plan_json(target_id=99)),
            FakeResponse(plan_json(target_id=3)),
        )
        result = plan_step("find my bill", ELEMENTS, client=client)

        assert result.plan.target_id == 3
        assert result.retried is True
        assert result.fell_back is False
        assert len(client.calls) == 2

        retry_messages = client.calls[1]["messages"]
        assert len(retry_messages) > len(client.calls[0]["messages"])
        assert "99" in retry_messages[-1]["content"]

    def test_falls_back_to_cannot_see_it_after_two_bad_replies(self):
        client = FakeClient(
            FakeResponse("not json at all"),
            FakeResponse(plan_json(target_id=99)),
        )
        result = plan_step("find my bill", ELEMENTS, client=client)

        assert result.plan.cannot_see_it is True
        assert result.plan.hint_if_missing
        assert result.retried is True
        assert result.fell_back is True
        assert len(client.calls) == 2

    def test_falls_back_when_ollama_is_unreachable(self):
        client = ExplodingClient(ConnectionError("Ollama server not running"))
        result = plan_step("find my bill", ELEMENTS, client=client)

        assert result.plan.cannot_see_it is True
        assert result.fell_back is True

    def test_does_not_retry_a_transport_failure(self):
        """A dead server will not answer differently one second later."""
        client = ExplodingClient(ConnectionError("down"))
        plan_step("find my bill", ELEMENTS, client=client)
        assert len(client.calls) == 1

    def test_an_empty_element_list_never_calls_the_model(self):
        client = FakeClient(FakeResponse(plan_json()))
        result = plan_step("find my bill", [], client=client)

        assert result.plan.cannot_see_it is True
        assert client.calls == []


class TestThinkParameter:
    """Some Ollama/model builds reject ``think``; bench_gemma.py hit this."""

    class PickyClient:
        """Rejects ``think``, like a server build that does not support it."""

        def __init__(self, content):
            self.content = content
            self.calls: list[dict] = []

        def chat(self, **kwargs):
            self.calls.append(kwargs)
            if "think" in kwargs:
                raise TypeError("chat() got an unexpected keyword argument 'think'")
            return FakeResponse(self.content)

    def test_retries_without_think_when_the_server_rejects_it(self):
        client = self.PickyClient(plan_json(target_id=2))
        result = plan_step("find my bill", ELEMENTS, client=client)

        assert result.plan.target_id == 2
        assert result.fell_back is False
        assert "think" not in client.calls[-1]

    def test_a_think_rejection_does_not_count_as_a_validation_retry(self):
        client = self.PickyClient(plan_json(target_id=2))
        result = plan_step("find my bill", ELEMENTS, client=client)
        assert result.retried is False


class TestFallbackPlan:
    def test_is_a_valid_step_plan_that_draws_no_circle(self):
        plan = fallback_plan("Scroll down a little")
        assert plan.cannot_see_it is True
        assert plan.target_id == 0
        assert plan.hint_if_missing == "Scroll down a little"

    def test_truncates_a_long_hint_to_the_schema_limit(self):
        plan = fallback_plan("y" * 200)
        assert len(plan.hint_if_missing) <= 90
