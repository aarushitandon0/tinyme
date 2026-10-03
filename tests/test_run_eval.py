"""Tests for the eval scoring (P10).

The eval is how every number in the post gets justified, so the scoring is worth
as much care as the app. What is tested here is specifically the part that could
be wrong *quietly*: a label file that disagrees with its element list, an off-by-
a-monitor-origin in the coordinate check, or a "correct" rule that happens to let
a ``cannot_see_it`` answer pass on a row where the target is plainly visible.

The model is never called. ``brain.plan_step`` takes a client, so a stub returning
a fixed JSON string is enough to drive the whole pipeline path.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from app.brain import CheckType, StepPlan, SuccessCheck
from app.elements import Element
from eval import run_eval


# --- fixtures --------------------------------------------------------------


def element(id: int, text: str, source: str = "ocr", role: str = "text",
            region: str = "center",
            bbox: tuple[float, float, float, float] = (10, 20, 110, 50)) -> Element:
    return Element(id=id, text=text, source=source, role=role, region=region, bbox_px=bbox)


FROZEN = {
    "format_version": 1,
    "shot": "explorer_01.png",
    "app": "explorer",
    "monitor": {"left": 0, "top": 0, "width": 1920, "height": 1080},
    "elements": [
        {"id": 1, "text": "Downloads", "source": "uia", "role": "ListItem",
         "region": "left-middle", "bbox_px": [100, 200, 300, 230]},
        {"id": 2, "text": "Downloads", "source": "ocr", "role": "text",
         "region": "top-left", "bbox_px": [400, 10, 500, 30]},
        {"id": 3, "text": "Documents", "source": "uia", "role": "ListItem",
         "region": "left-middle", "bbox_px": [100, 240, 300, 270]},
    ],
}


def write_dataset(tmp_path: Path, rows: list[dict], frozen: dict | None = None,
                  name: str = "explorer_01.json") -> Path:
    """Write a labels file plus the element file it points at, and repoint run_eval."""
    elements_dir = tmp_path / "elements"
    screenshots_dir = tmp_path / "screenshots"
    elements_dir.mkdir(exist_ok=True)
    screenshots_dir.mkdir(exist_ok=True)
    (elements_dir / name).write_text(json.dumps(frozen or FROZEN), encoding="utf-8")

    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return labels


@pytest.fixture(autouse=True)
def redirect_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "ELEMENTS_DIR", tmp_path / "elements")
    monkeypatch.setattr(run_eval, "SCREENSHOT_DIR", tmp_path / "screenshots")


def base_row(**overrides) -> dict:
    row = {
        "id": "explorer_01_a",
        "screenshot": "explorer_01.png",
        "elements": "explorer_01.json",
        "app": "explorer",
        "goal": "find the file I downloaded",
        "history": ["Open File Explorer"],
        "correct_ids": [1],
    }
    row.update(overrides)
    return row


# --- loading and validating the dataset ------------------------------------


def test_load_rows_reads_elements_and_history(tmp_path):
    labels = write_dataset(tmp_path, [base_row()])
    rows = run_eval.load_rows(labels)

    assert len(rows) == 1
    row = rows[0]
    assert row.id == "explorer_01_a"
    assert row.app == "explorer"
    assert row.history == ("Open File Explorer",)
    assert row.correct_ids == frozenset({1})
    assert [e.text for e in row.elements] == ["Downloads", "Downloads", "Documents"]
    assert row.by_id(3).text == "Documents"


def test_load_rows_rejects_correct_id_not_in_the_element_list(tmp_path):
    """A typo'd id would otherwise mark every system wrong and look like a model
    failure in the table."""
    labels = write_dataset(tmp_path, [base_row(correct_ids=[99])])
    with pytest.raises(SystemExit, match="99"):
        run_eval.load_rows(labels)


def test_load_rows_rejects_a_row_nothing_could_satisfy(tmp_path):
    labels = write_dataset(tmp_path, [base_row(correct_ids=[])])
    with pytest.raises(SystemExit, match="nothing could ever be right"):
        run_eval.load_rows(labels)


def test_load_rows_rejects_cannot_see_with_correct_ids(tmp_path):
    labels = write_dataset(tmp_path, [base_row(expect_cannot_see=True)])
    with pytest.raises(SystemExit, match="pick one"):
        run_eval.load_rows(labels)


def test_load_rows_accepts_a_cannot_see_row(tmp_path):
    labels = write_dataset(tmp_path, [base_row(correct_ids=[], expect_cannot_see=True)])
    rows = run_eval.load_rows(labels)
    assert rows[0].expect_cannot_see is True
    assert rows[0].correct_ids == frozenset()


def test_load_rows_reports_the_line_number_of_bad_json(tmp_path):
    labels = write_dataset(tmp_path, [base_row()])
    labels.write_text(json.dumps(base_row()) + "\n{not json}\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=":2:"):
        run_eval.load_rows(labels)


def test_load_rows_skips_blank_and_commented_lines(tmp_path):
    labels = write_dataset(tmp_path, [base_row()])
    labels.write_text(
        "// a note about this app\n\n" + json.dumps(base_row()) + "\n\n",
        encoding="utf-8",
    )
    assert len(run_eval.load_rows(labels)) == 1


# --- the coordinate check --------------------------------------------------


def test_point_in_boxes_includes_the_edges():
    boxes = [(10.0, 20.0, 110.0, 50.0)]
    assert run_eval.point_in_boxes(10, 20, boxes)
    assert run_eval.point_in_boxes(110, 50, boxes)
    assert run_eval.point_in_boxes(60, 35, boxes)
    assert not run_eval.point_in_boxes(9, 35, boxes)
    assert not run_eval.point_in_boxes(60, 51, boxes)


def test_point_in_boxes_is_false_with_no_boxes():
    assert not run_eval.point_in_boxes(5, 5, [])


def test_correct_boxes_are_shifted_out_of_screen_space(tmp_path):
    """A bbox is in screen coordinates; the model is given an image that starts at
    the monitor's origin. On a primary monitor that is not at (0, 0) -- which
    happens whenever a second display sits to the left -- forgetting the shift
    scores every coordinate answer against boxes that are offset by the whole
    monitor origin."""
    frozen = dict(FROZEN, monitor={"left": -1920, "top": -200,
                                   "width": 1920, "height": 1080})
    labels = write_dataset(tmp_path, [base_row(correct_ids=[1])], frozen=frozen)
    row = run_eval.load_rows(labels)[0]

    assert row.monitor_origin == (-1920, -200)
    assert row.correct_boxes_in_image() == [(2020.0, 400.0, 2220.0, 430.0)]


def test_correct_boxes_only_covers_labelled_ids(tmp_path):
    labels = write_dataset(tmp_path, [base_row(correct_ids=[1, 3])])
    row = run_eval.load_rows(labels)[0]
    assert len(row.correct_boxes_in_image()) == 2


# --- the pipeline system ---------------------------------------------------


class StubClient:
    """Returns canned replies, counting calls. Mirrors ollama.Client.chat enough
    for brain.plan_step."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        content = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

        class Message:
            def __init__(self, text: str) -> None:
                self.content = text

        class Response:
            def __init__(self, text: str) -> None:
                self.message = Message(text)
                self.prompt_eval_count = 900
                self.eval_count = 40

        return Response(content)


def plan_json(target_id: int, cannot_see: bool = False, goal_reached: bool = False,
              instruction: str = "Click Downloads in the left panel") -> str:
    return StepPlan(
        instruction=instruction,
        target_id=target_id,
        success_check=SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS, value="Downloads"),
        goal_reached=goal_reached,
        cannot_see_it=cannot_see,
    ).model_dump_json()


def one_row(tmp_path, **overrides) -> run_eval.Row:
    labels = write_dataset(tmp_path, [base_row(**overrides)])
    return run_eval.load_rows(labels)[0]


def test_pipeline_scores_a_correct_pick(tmp_path):
    row = one_row(tmp_path)
    answer = run_eval.run_pipeline(row, "gemma4:e2b", StubClient(plan_json(1)))

    assert answer.correct is True
    assert answer.valid is True
    assert answer.retried is False
    assert answer.reason == ""
    assert answer.prompt_tokens == 900
    assert "Downloads" in answer.chose


def test_pipeline_accepts_any_of_several_correct_ids(tmp_path):
    row = one_row(tmp_path, correct_ids=[1, 2])
    assert run_eval.run_pipeline(row, "m", StubClient(plan_json(2))).correct is True


def test_pipeline_scores_a_wrong_pick(tmp_path):
    row = one_row(tmp_path)
    answer = run_eval.run_pipeline(row, "m", StubClient(plan_json(3)))
    assert answer.correct is False
    assert answer.reason


def test_cannot_see_it_is_wrong_when_the_target_is_on_screen(tmp_path):
    """The failure this guards against: counting "I cannot see it" as right
    because the id check is skipped for that flag, which would reward a model
    that gave up on every row."""
    row = one_row(tmp_path)
    answer = run_eval.run_pipeline(row, "m", StubClient(plan_json(0, cannot_see=True)))
    assert answer.correct is False
    assert answer.reason == "said it could not see the target, which is on this screen"


def test_cannot_see_it_is_right_on_a_cannot_see_row(tmp_path):
    row = one_row(tmp_path, correct_ids=[], expect_cannot_see=True)
    answer = run_eval.run_pipeline(row, "m", StubClient(plan_json(0, cannot_see=True)))
    assert answer.correct is True


def test_picking_something_on_a_cannot_see_row_is_wrong(tmp_path):
    row = one_row(tmp_path, correct_ids=[], expect_cannot_see=True)
    answer = run_eval.run_pipeline(row, "m", StubClient(plan_json(1)))
    assert answer.correct is False
    assert "not on this screen" in answer.reason


def test_invalid_json_then_a_good_retry_counts_as_valid_and_retried(tmp_path):
    row = one_row(tmp_path)
    client = StubClient("not json at all", plan_json(1))
    answer = run_eval.run_pipeline(row, "m", client)

    assert len(client.calls) == 2
    assert answer.retried is True
    assert answer.valid is True
    assert answer.correct is True


def test_two_invalid_replies_count_as_invalid_json(tmp_path):
    row = one_row(tmp_path)
    answer = run_eval.run_pipeline(row, "m", StubClient("nope", "still nope"))
    assert answer.valid is False
    assert answer.correct is False


def test_the_model_never_sees_a_coordinate(tmp_path):
    """CLAUDE.md rule 1, checked at the eval boundary too: the frozen element
    files contain bboxes, so this is the one place a coordinate could leak back
    into a prompt."""
    row = one_row(tmp_path)
    client = StubClient(plan_json(1))
    run_eval.run_pipeline(row, "m", client)

    prompt = "\n".join(message["content"] for message in client.calls[0]["messages"])
    for element_json in FROZEN["elements"]:
        for value in element_json["bbox_px"]:
            assert str(int(value)) not in prompt


# --- the failure reasons ---------------------------------------------------


def test_reason_names_the_right_words_in_the_wrong_place(tmp_path):
    """Both ids say "Downloads"; the labelled one is in the left panel and the
    other is the window's own title."""
    row = one_row(tmp_path, correct_ids=[1])
    plan = StepPlan.model_validate_json(plan_json(2))
    reason = run_eval.pipeline_reason(row, plan, row.by_id(2))
    assert "wrong place" in reason
    assert "top-left" in reason


def test_reason_names_the_label_instead_of_the_control(tmp_path):
    frozen = {
        **FROZEN,
        "elements": [
            {"id": 1, "text": "Printers", "source": "uia", "role": "Button",
             "region": "center", "bbox_px": [10, 10, 50, 30]},
            {"id": 2, "text": "Add device", "source": "ocr", "role": "text",
             "region": "center", "bbox_px": [60, 10, 100, 30]},
        ],
    }
    labels = write_dataset(tmp_path, [base_row(correct_ids=[1])], frozen=frozen)
    row = run_eval.load_rows(labels)[0]
    plan = StepPlan.model_validate_json(plan_json(2))
    assert "label instead of the control" in run_eval.pipeline_reason(row, plan, row.by_id(2))


def test_reason_for_goal_reached_too_early(tmp_path):
    row = one_row(tmp_path)
    plan = StepPlan.model_validate_json(plan_json(0, goal_reached=True))
    assert "goal reached" in run_eval.pipeline_reason(row, plan, None)


def test_describe_handles_nothing():
    assert run_eval.describe(None) == "nothing"
    assert "#4" in run_eval.describe(element(4, "Downloads"))


# --- system names ----------------------------------------------------------


def test_resolve_systems_expands_all():
    resolved = run_eval.resolve_systems("all", external=False)
    assert [name for name, _, _ in resolved] == list(run_eval.DEFAULT_SYSTEMS)
    assert ("pipeline_e2b", "pipeline", "gemma4:e2b") in resolved


def test_resolve_systems_keeps_a_full_model_tag():
    resolved = run_eval.resolve_systems("pipeline_qwen3:4b", external=False)
    assert resolved == [("pipeline_qwen3:4b", "pipeline", "qwen3:4b")]


def test_resolve_systems_rejects_an_unknown_kind():
    with pytest.raises(SystemExit, match="unknown system"):
        run_eval.resolve_systems("magic_e2b", external=False)


def test_external_needs_its_environment(monkeypatch):
    monkeypatch.setattr(run_eval, "EXTERNAL_BASE_URL", "")
    monkeypatch.setattr(run_eval, "EXTERNAL_MODEL", "")
    monkeypatch.setattr(run_eval, "EXTERNAL_KEY", "")
    with pytest.raises(SystemExit, match="TINYME_EXTERNAL_BASE_URL"):
        run_eval.resolve_systems("pipeline_e2b", external=True)


# --- the report ------------------------------------------------------------


def test_rate_puts_the_count_before_the_percentage():
    assert run_eval._rate(31, 40) == "31/40 (78%)"
    assert run_eval._rate(0, 0) == "0/0 (-)"


def result_with(name: str, outcomes: list[tuple[str, bool]]) -> run_eval.SystemResult:
    result = run_eval.SystemResult(name=name, model="gemma4:e2b")
    for index, (app, correct) in enumerate(outcomes):
        result.answers.append(run_eval.Answer(
            row_id=f"{app}_{index:02d}", app=app, correct=correct, valid=True,
            elapsed_ms=1000.0 * (index + 1),
            reason="" if correct else "picked something else",
            chose="#1 \"Downloads\"",
        ))
    return result


def test_system_result_counts_and_percentiles():
    result = result_with("pipeline_e2b", [("explorer", True), ("explorer", False),
                                          ("notepad", True), ("notepad", True)])
    assert (result.correct, result.attempted) == (3, 4)
    assert result.per_app() == {"explorer": (1, 2), "notepad": (2, 2)}
    assert result.median_ms == 2500.0
    assert result.latency(0.9) == 4000.0
    assert len(result.failures()) == 1


def test_write_results_is_generated_and_shows_counts(tmp_path):
    labels = write_dataset(tmp_path, [base_row(), base_row(id="explorer_01_b",
                                                           correct_ids=[3])])
    rows = run_eval.load_rows(labels)
    result = result_with("pipeline_e2b", [("explorer", True), ("explorer", False)])
    result.answers[1].row_id = rows[1].id

    out = tmp_path / "results.md"
    run_eval.write_results([result], rows, {"cpu": "test cpu"}, out)
    text = out.read_text(encoding="utf-8")

    assert "Do not edit by hand" in text
    assert "1/2 (50%)" in text
    assert "test cpu" in text
    # The failure section must name the row, what it chose and what was correct.
    assert rows[1].id in text
    assert "picked something else" in text
    # And it must be honest about what the table cannot show.
    assert "Latency is the model call only" in text


def test_write_results_says_so_when_there_are_no_failures(tmp_path):
    labels = write_dataset(tmp_path, [base_row()])
    rows = run_eval.load_rows(labels)
    result = result_with("pipeline_e4b", [("explorer", True)])
    result.answers[0].row_id = rows[0].id

    out = tmp_path / "results.md"
    run_eval.write_results([result], rows, {}, out)
    assert "No failures." in out.read_text(encoding="utf-8")


# --- the tinker systems (P13) ----------------------------------------------
#
# Tinker is not installed on the machine that runs these tests -- it needs
# PyTorch, which Application Control blocks here, so the real rows are produced
# from WSL. What is stubbed is therefore the whole SDK surface ``run_tinker``
# touches, and what is tested is the shape of that contract: in particular that
# ``SamplingClient.sample`` returns a *future*, not a response. Getting that
# wrong fails instantly on every row, which looks exactly like a model that
# cannot answer.


class StubFuture:
    """What ``tinker.SamplingClient.sample`` actually returns."""

    def __init__(self, value: object) -> None:
        self._value = value

    def result(self):
        return self._value


class StubSamplingClient:
    def __init__(self, reply: str, raises: Exception | None = None) -> None:
        self.reply = reply
        self.raises = raises
        self.calls: list[dict] = []

    def sample(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises

        class Sequence:
            tokens = [1, 2, 3]

        class Response:
            sequences = [Sequence()]

        return StubFuture(Response())


class StubRenderer:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def build_generation_prompt(self, messages):
        self.messages = messages
        return messages

    def parse_response(self, tokens):
        return {"role": "assistant", "content": self.reply}, None


@pytest.fixture
def fake_tinker(monkeypatch):
    """Put a stub ``tinker`` module in place and hand back a sampler factory."""
    module = types.ModuleType("tinker")
    module.SamplingParams = lambda **kwargs: kwargs
    monkeypatch.setitem(sys.modules, "tinker", module)

    def install(reply: str = "", raises: Exception | None = None) -> StubSamplingClient:
        client = StubSamplingClient(reply, raises)
        monkeypatch.setattr(run_eval, "tinker_sampler",
                            lambda checkpoint: (client, StubRenderer(reply)))
        return client

    return install


def test_tinker_waits_for_the_sampling_future(tmp_path, fake_tinker):
    row = one_row(tmp_path)
    fake_tinker(plan_json(1))

    answer = run_eval.run_tinker(row, "")

    assert answer.valid is True
    assert answer.correct is True
    assert "Downloads" in answer.chose


def test_tinker_reason_names_why_the_call_failed(tmp_path, fake_tinker):
    row = one_row(tmp_path)
    fake_tinker(raises=RuntimeError("sampler is busy"))

    answer = run_eval.run_tinker(row, "")

    assert answer.valid is False
    assert answer.chose == "call failed"
    assert "sampler is busy" in answer.reason


# --- the path printed at the end -------------------------------------------


def test_display_path_is_relative_for_a_path_inside_the_repo():
    assert run_eval.display_path(Path("eval/results_tinker.md")) == str(
        Path("eval/results_tinker.md"))


def test_display_path_falls_back_to_the_full_path_outside_the_repo(tmp_path):
    outside = tmp_path / "results.md"
    assert run_eval.display_path(outside) == str(outside)
