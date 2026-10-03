"""Tests for finetune/build_dataset.py (P13).

A fine-tune dataset fails quietly. A mislabelled example does not raise, it
just produces a model that is slightly wrong in a way no test catches, and then
a number in the write-up. So the tests here are about the three ways this
script could lie:

* **A target that moved.** Augmentation renumbers the element list, so the
  label has to move with it. If it does not, the model is trained to pick the
  wrong line and the eval still looks plausible.
* **A leak.** An augmented copy of a test screen appearing in train turns the
  held-out score into a memorisation score.
* **A split that is weaker than it is described as.** With one labelled app
  there is no held-out app, and the script has to refuse rather than produce a
  number that reads as generalisation.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.brain import CheckType, StepPlan
from app.elements import Element
from eval.run_eval import Row
from finetune import build_dataset as bd


def element(id: int, text: str, region: str = "center") -> Element:
    return Element(id=id, text=text, source="ocr", role="text", region=region,
                   bbox_px=(10.0 * id, 20.0, 10.0 * id + 50, 44.0))


def row(app: str = "explorer", screenshot: str = "explorer_01.png",
        correct: set[int] | None = None, expect_cannot_see: bool = False,
        n: int = 12) -> Row:
    return Row(
        id=f"{app}_{screenshot}",
        app=app,
        goal="I cannot find the electricity bill",
        screenshot=Path("eval/screenshots") / screenshot,
        elements=[element(index, f"thing {index}") for index in range(1, n + 1)],
        correct_ids=frozenset(correct or {7}),
        history=("Open File Explorer",),
        expect_cannot_see=expect_cannot_see,
    )


class TestTheLabelFollowsTheRenumbering:
    """The one bug that would poison the dataset in silence."""

    def test_the_target_id_points_at_the_same_element_after_augmentation(self):
        import random

        source = row()
        target = bd.chosen_target(source)
        wanted = source.by_id(target)
        assert wanted is not None

        rng = random.Random(1)
        for _ in range(40):
            elements, new_target, _ = bd.augment(source, target, rng)
            moved = next(e for e in elements if e.id == new_target)
            assert moved.text == wanted.text
            assert moved.bbox_px == wanted.bbox_px

    def test_every_augmented_example_labels_an_id_that_is_in_its_own_list(self):
        import random

        rng = random.Random(2)
        for example in bd.examples_for(row(), copies=6, rng=rng):
            plan = StepPlan.model_validate_json(example.completion)
            listed = example.messages[1]["content"]
            assert f"\n{plan.target_id} | " in listed, (
                f"copy {example.copy} labels id {plan.target_id}, "
                "which is not a line in its own element list"
            )

    def test_the_target_is_never_dropped(self):
        import random

        source = row(n=80)
        rng = random.Random(3)
        for _ in range(60):
            elements, new_target, _ = bd.augment(source, 7, rng)
            assert new_target is not None
            assert any(e.id == new_target for e in elements)

    def test_shuffling_actually_moves_the_answer_around(self):
        """Otherwise the model can learn the id instead of the screen."""
        import random

        rng = random.Random(4)
        ids = {bd.augment(row(n=40), 7, rng)[1] for _ in range(30)}
        assert len(ids) > 5, "the target id barely moved; augmentation is not working"


class TestTheTarget:
    def test_a_cannot_see_row_is_labelled_cannot_see_it(self):
        import random

        source = row(correct=set(), expect_cannot_see=True)
        assert bd.chosen_target(source) is None
        example = bd.examples_for(source, copies=1, rng=random.Random(5))[0]
        plan = StepPlan.model_validate_json(example.completion)
        assert plan.cannot_see_it is True
        assert plan.hint_if_missing

    def test_the_lowest_correct_id_is_the_stable_choice(self):
        assert bd.chosen_target(row(correct={19, 4, 31})) == 4

    def test_the_instruction_fits_the_schema_even_for_a_long_label(self):
        long = Element(id=1, text="x" * 200, source="ocr", role="text",
                       region="center", bbox_px=(0.0, 0.0, 10.0, 10.0))
        assert len(bd._instruction_for(long)) <= 90

    def test_the_templated_check_is_the_one_that_is_always_valid(self):
        plan = bd.target_plan(row(), 7)
        assert plan.success_check.type is CheckType.REGION_CHANGED

    def test_the_completion_is_a_valid_step_plan(self):
        import random

        for example in bd.examples_for(row(), copies=3, rng=random.Random(6)):
            StepPlan.model_validate_json(example.completion)


class TestThePromptIsTheAppsPrompt:
    """A fine-tune against a paraphrase of the real prompt is worth nothing."""

    def test_the_messages_come_from_brain_build_messages(self):
        import random

        from app import brain as brain_mod

        source = row()
        example = bd.examples_for(source, copies=1, rng=random.Random(7))[0]
        expected = brain_mod.build_messages(source.goal, source.elements,
                                           history=source.history)
        assert example.messages == expected

    def test_no_bbox_or_pixel_value_reaches_the_prompt(self):
        """CLAUDE.md rule 1, which a dataset could break as easily as the app."""
        import random

        for example in bd.examples_for(row(), copies=4, rng=random.Random(8)):
            for message in example.messages:
                assert "bbox" not in message["content"]
                # The fixture's boxes are at x = 10, 20, 30 ... so a leaked box
                # would show up as these coordinate pairs.
                assert "(10.0," not in message["content"]
                assert "20.0, 44.0" not in message["content"]


class TestTheSplit:
    def test_one_app_is_refused(self):
        rows = [row(screenshot="explorer_01.png"), row(screenshot="explorer_02.png")]
        with pytest.raises(SystemExit) as exc:
            bd.split_rows(rows, holdout_app=None, holdout_screenshot=False)
        assert "no honest test" in str(exc.value)

    def test_two_apps_hold_one_out_whole(self):
        rows = [row(app="explorer"), row(app="settings")]
        train, test, how = bd.split_rows(rows, None, False)
        assert [r.app for r in train] == ["explorer"]
        assert [r.app for r in test] == ["settings"]
        assert "held out the whole settings app" in how

    def test_the_held_out_app_can_be_chosen(self):
        rows = [row(app="explorer"), row(app="settings")]
        train, test, _ = bd.split_rows(rows, "explorer", False)
        assert [r.app for r in test] == ["explorer"]

    def test_an_unknown_holdout_app_is_refused(self):
        with pytest.raises(SystemExit):
            bd.split_rows([row(app="explorer"), row(app="settings")], "notepad", False)

    def test_the_weak_split_says_so_in_its_own_description(self):
        rows = [row(screenshot="explorer_01.png"), row(screenshot="explorer_02.png")]
        _, test, how = bd.split_rows(rows, None, holdout_screenshot=True)
        assert how.startswith("WEAK SPLIT")
        assert len(test) == 1

    def test_a_single_screenshot_cannot_be_split_at_all(self):
        with pytest.raises(SystemExit):
            bd.split_rows([row()], None, holdout_screenshot=True)


class TestNoLeak:
    def test_a_shared_row_is_caught(self):
        import random

        rng = random.Random(9)
        shared = row()
        train = bd.examples_for(shared, 3, rng)
        test = bd.examples_for(shared, 1, rng)
        with pytest.raises(AssertionError):
            bd.check_no_leak(train, test)

    def test_the_real_build_has_no_leak_and_an_unaugmented_test_split(self, tmp_path):
        counts = bd.build(copies=3, holdout_screenshot=True, out_dir=tmp_path)
        test = [json.loads(line) for line
                in (tmp_path / "test.jsonl").read_text(encoding="utf-8").splitlines()]
        train = [json.loads(line) for line
                 in (tmp_path / "train.jsonl").read_text(encoding="utf-8").splitlines()]

        assert {entry["copy"] for entry in test} == {0}, "the test split was augmented"
        assert not ({entry["row_id"] for entry in train}
                    & {entry["row_id"] for entry in test})
        assert counts["train"] > counts["test"]


class TestTheDataCard:
    def test_it_records_the_weak_split_as_a_warning(self, tmp_path):
        bd.build(copies=2, holdout_screenshot=True, out_dir=tmp_path)
        card = (tmp_path / "data_card.md").read_text(encoding="utf-8")
        assert "Honesty warning" in card
        assert "not the split MASTERSPEC 9 specifies" in card

    def test_it_says_which_fields_are_real_labels(self, tmp_path):
        bd.build(copies=2, holdout_screenshot=True, out_dir=tmp_path)
        card = (tmp_path / "data_card.md").read_text(encoding="utf-8")
        assert "`target_id` is a **real label**" in card
        assert "`success_check` is **templated**" in card

    def test_it_records_the_seed_so_the_dataset_can_be_rebuilt(self, tmp_path):
        bd.build(copies=2, seed=1234, holdout_screenshot=True, out_dir=tmp_path)
        card = (tmp_path / "data_card.md").read_text(encoding="utf-8")
        assert "Random seed: 1234" in card
