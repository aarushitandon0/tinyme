"""Turn ``eval/labels.jsonl`` into a Tinker SFT dataset (P13, MASTERSPEC 9).

    python finetune\\build_dataset.py
    python finetune\\build_dataset.py --holdout-app explorer --copies 4

The task being taught is the one thing Gemma has to get right: *given her goal,
what she has already done, and the numbered list of what is on screen, which
number should she click?* No images are involved -- the element lists are
frozen in ``eval/elements/*.json``, which is also why this dataset can be built
and inspected on a laptop with no GPU and no network.

Three decisions worth knowing before you read the code, all of them about
honesty rather than about machine learning:

**The prompt is not a copy of the app's prompt; it is the app's prompt.**
Every example is built by calling :func:`app.brain.build_messages`, the same
function ``brain.plan_step`` calls at runtime. If the prompt changes, the
dataset changes with it. A fine-tune against a paraphrase of the real prompt
would look fine in the eval and be worse in her hands.

**Only ``target_id`` is a real label.** The labels file records which elements
are correct and nothing else, so ``instruction`` and ``success_check`` in the
target JSON are *templated* -- see :func:`target_plan`. A model trained on this
learns to pick the right element and to emit the right shape; it does not learn
to write instructions or to choose checks, and it would regress both if dropped
into ``brain.py`` as-is. ``data_card.md`` says so, and so should the post.

**The split is by app, and with one app there is no split.** MASTERSPEC 9 holds
a whole app out so the test says something about a screen the model has never
seen. With a single labelled app that is impossible, and this script refuses
rather than quietly holding out three screenshots of the same File Explorer and
calling it generalisation. ``--holdout-screenshot`` forces that weaker split and
stamps the data card accordingly.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import brain as brain_mod  # noqa: E402
from app.brain import CheckType, StepPlan, SuccessCheck  # noqa: E402
from app.elements import Element  # noqa: E402
from eval.run_eval import Row, load_rows  # noqa: E402

log = logging.getLogger("build_dataset")

OUT_DIR = Path(__file__).resolve().parent
TRAIN_PATH = OUT_DIR / "train.jsonl"
TEST_PATH = OUT_DIR / "test.jsonl"
DATA_CARD_PATH = OUT_DIR / "data_card.md"

#: How many augmented copies of each labelled row to write, including the
#: unaugmented one. Four is a guess tempered by the size of the source: with
#: ~20 rows, more copies buy more passes over the same twenty screens, not more
#: information.
DEFAULT_COPIES = 4

#: Fraction of the non-target elements an augmented copy may drop. Enough to
#: change the list and the numbering; not enough to turn an eighty-element
#: screen into a trivial one.
DROP_FRACTION = 0.15

#: Meaning-preserving wrappers for her goal. Deliberately dull: a paraphrase
#: that changes what she asked for is a mislabelled example, and these are the
#: ways she actually opens a sentence.
GOAL_WRAPPERS = (
    "{goal}",
    "{goal}?",
    "please, {goal}",
    "can you help me, {goal}",
    "beta, {goal}",
)

#: The seed is fixed and recorded in the data card: a dataset you cannot
#: rebuild is a number you cannot defend.
DEFAULT_SEED = 20261004


@dataclass(frozen=True)
class Example:
    """One training example: the app's prompt, and the JSON we want back."""

    #: Which labelled row this came from, so train/test leakage is checkable.
    row_id: str
    app: str
    messages: list[dict[str, str]]
    completion: str
    #: 0 for the unaugmented copy, 1+ for augmented ones.
    copy: int = 0

    def as_conversation(self) -> dict[str, object]:
        """Tinker's chat format: a list of messages, assistant message last.

        ``tinker_cookbook.supervised.data.conversation_to_datum`` takes exactly
        this and masks the loss onto the final assistant message, which is what
        makes this completion-only training.
        """
        return {
            "row_id": self.row_id,
            "app": self.app,
            "copy": self.copy,
            "messages": [
                *self.messages,
                {"role": "assistant", "content": self.completion},
            ],
        }


# --- the target ------------------------------------------------------------


def _instruction_for(element: Element | None) -> str:
    """A plain templated instruction, capped at the schema's 90 characters.

    Templated, not labelled: see the module docstring. The verb changes with
    what the element is only so that the model does not learn that every answer
    starts with the same word.
    """
    if element is None:
        return "I cannot see that on this screen."
    text = (element.text or "").strip() or "that"
    verb = "Open" if element.region.startswith("bottom") else "Click"
    return f"{verb} {text}"[:90]


def target_plan(row: Row, target_id: int | None) -> StepPlan:
    """The StepPlan we want the model to produce for this row.

    ``target_id`` is the label. ``instruction`` and ``success_check`` are
    templates and are not evidence of anything -- ``run_eval`` scores element
    choice and JSON validity, and those are the only two claims a model trained
    on this dataset can support.

    ``region_changed`` is the templated check because it is the one check that
    is *valid* for any element without knowing anything else about the screen
    (MASTERSPEC 5.4). It is also the main reason this dataset cannot be used to
    train the shipped brain wholesale: it would teach the model to choose that
    check always, and the cheap window checks are what keep her laptop
    responsive.
    """
    if target_id is None:
        hint = "I cannot see it. Try scrolling, or open the menu at the top."
        return brain_mod.fallback_plan(hint)

    element = row.by_id(target_id)
    return StepPlan(
        instruction=_instruction_for(element),
        target_id=target_id,
        success_check=SuccessCheck(type=CheckType.REGION_CHANGED, value=""),
        goal_reached=False,
        cannot_see_it=False,
        hint_if_missing="",
    )


def chosen_target(row: Row) -> int | None:
    """Which of the correct ids to train on: the lowest, or None.

    Several ids can be right (a list row and its nav entry are the same click
    to her). Training on all of them would teach the model that this screen has
    several answers; training on a random one would make the dataset depend on
    the seed in a way the data card could not explain. The lowest id is
    arbitrary but stable, and the eval still counts any correct id as correct,
    so nothing is scored on this choice.
    """
    if row.expect_cannot_see or not row.correct_ids:
        return None
    return min(row.correct_ids)


# --- augmentation ----------------------------------------------------------


def renumber(elements: Sequence[Element]) -> list[Element]:
    """Renumber a list 1..n in its current order, as ``elements`` does at runtime."""
    return [
        Element(id=index, text=element.text, source=element.source,
                role=element.role, region=element.region, bbox_px=element.bbox_px)
        for index, element in enumerate(elements, start=1)
    ]


def augment(row: Row, target_id: int | None, rng: random.Random
            ) -> tuple[list[Element], int | None, str]:
    """One shuffled, thinned, rewrapped copy of a row.

    The three augmentations do different jobs:

    * **Shuffle and renumber.** The one augmentation that matters. Without it a
      model can learn "the answer is usually number 33" from twenty rows, which
      is exactly the shortcut that would score well on a held-out screenshot of
      the same app and fail on hers.
    * **Drop non-target elements.** Varies the list length, so the model does
      not tie its answer to a screen having eighty lines.
    * **Wrap the goal.** The way she actually types, with the meaning untouched.

    Returns the new element list, the target's new id, and the new goal.
    """
    kept = [element for element in row.elements
            if element.id == target_id or rng.random() > DROP_FRACTION]
    # Keep at least the target and a plausible screen's worth of distractors,
    # so a thinned copy is still the same kind of problem.
    if len(kept) < max(4, len(row.elements) // 4):
        kept = list(row.elements)

    rng.shuffle(kept)
    old_ids = [element.id for element in kept]
    renumbered = renumber(kept)
    new_target = (renumbered[old_ids.index(target_id)].id
                  if target_id is not None and target_id in old_ids else None)
    if target_id is not None and new_target is None:
        # The target survived the drop by construction, so this cannot happen;
        # if it ever does, fail loudly rather than write a mislabelled example.
        raise AssertionError(f"{row.id}: lost the target element during augmentation")

    goal = rng.choice(GOAL_WRAPPERS).format(goal=row.goal)
    return renumbered, new_target, goal


# --- building --------------------------------------------------------------


def examples_for(row: Row, copies: int, rng: random.Random) -> list[Example]:
    """The unaugmented example for a row, plus ``copies - 1`` augmented ones."""
    out: list[Example] = []
    target_id = chosen_target(row)

    plan = target_plan(row, target_id)
    out.append(Example(
        row_id=row.id,
        app=row.app,
        messages=brain_mod.build_messages(row.goal, row.elements,
                                          history=row.history),
        completion=plan.model_dump_json(),
        copy=0,
    ))

    for copy in range(1, max(1, copies)):
        elements, new_target, goal = augment(row, target_id, rng)
        shifted = Row(
            id=row.id, app=row.app, goal=goal, screenshot=row.screenshot,
            elements=elements,
            correct_ids=frozenset({new_target} if new_target is not None else set()),
            history=row.history, expect_cannot_see=row.expect_cannot_see,
            monitor_origin=row.monitor_origin,
        )
        out.append(Example(
            row_id=row.id,
            app=row.app,
            messages=brain_mod.build_messages(goal, elements, history=row.history),
            completion=target_plan(shifted, new_target).model_dump_json(),
            copy=copy,
        ))
    return out


def split_rows(rows: Sequence[Row], holdout_app: str | None,
               holdout_screenshot: bool) -> tuple[list[Row], list[Row], str]:
    """Hold out one whole app, or refuse (MASTERSPEC 9).

    Returns (train, test, how) where ``how`` is the one line the data card and
    the results table need in order not to overstate the test.
    """
    apps = sorted({row.app for row in rows})

    if len(apps) >= 2:
        app = holdout_app or apps[-1]
        if app not in apps:
            raise SystemExit(f"--holdout-app {app!r} is not in the labels ({', '.join(apps)})")
        train = [row for row in rows if row.app != app]
        test = [row for row in rows if row.app == app]
        return train, test, f"held out the whole {app} app ({len(test)} rows)"

    if not holdout_screenshot:
        raise SystemExit(
            f"Only one app is labelled ({apps[0] if apps else 'none'}), so there is no\n"
            "app to hold out and no honest test of generalisation to a new app\n"
            "(MASTERSPEC 9).\n\n"
            "Either label a second app -- `python eval\\collect.py --group all` on a\n"
            "quiet machine, then label the screens in eval/labels.jsonl -- or pass\n"
            "--holdout-screenshot to split by screenshot instead. That weaker split\n"
            "is stamped into data_card.md and must be described that way anywhere the\n"
            "numbers are quoted."
        )

    screens = sorted({row.screenshot.name for row in rows})
    if len(screens) < 2:
        raise SystemExit("one screenshot cannot be split into train and test")
    held = screens[-1]
    train = [row for row in rows if row.screenshot.name != held]
    test = [row for row in rows if row.screenshot.name == held]
    return train, test, (f"WEAK SPLIT: held out one screenshot ({held}, {len(test)} rows) "
                         f"of the same {apps[0]} app -- not a held-out app")


def write_jsonl(examples: Iterable[Example], path: Path) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for example in examples:
            handle.write(json.dumps(example.as_conversation(), ensure_ascii=False) + "\n")
            count += 1
    return count


def check_no_leak(train: Sequence[Example], test: Sequence[Example]) -> None:
    """No augmented copy of a test row may appear in train (tinker skill).

    Checked on ``row_id`` rather than on the text, because augmentation changes
    the text and the point is that the *screen* must not be shared.
    """
    shared = {example.row_id for example in train} & {example.row_id for example in test}
    if shared:
        raise AssertionError(f"train and test share {len(shared)} rows: {sorted(shared)[:5]}")


def tokens_estimate(examples: Sequence[Example]) -> int:
    """A rough token count for the cost gate in ``tinker_train.py``.

    Characters over four, the usual English approximation. It is an estimate and
    the training script says so when it prints the cost: the real tokenizer
    lives on Tinker's side and is not worth a dependency here to be 10% closer.
    """
    characters = sum(
        len(message["content"]) for example in examples
        for message in example.as_conversation()["messages"]  # type: ignore[index]
    )
    return characters // 4


def write_data_card(train: Sequence[Example], test: Sequence[Example],
                    rows: Sequence[Row], how: str, copies: int, seed: int,
                    path: Path = DATA_CARD_PATH) -> Path:
    """Generate ``data_card.md``. Never edit the output by hand."""
    train_apps = Counter(example.app for example in train)
    test_apps = Counter(example.app for example in test)
    weak = how.startswith("WEAK SPLIT")

    lines = [
        "# Tiny Me fine-tune data card",
        "",
        "Generated by `finetune/build_dataset.py`. Do not edit by hand; rerun the",
        "script. Every number here is a count of what is in `train.jsonl` and",
        "`test.jsonl`.",
        "",
        f"- Source: `eval/labels.jsonl` ({len(rows)} labelled rows)",
        f"- Augmented copies per row: {copies} (including the unaugmented one)",
        f"- Random seed: {seed}",
        f"- Split: {how}",
        f"- Train examples: {len(train)} from {len(train_apps)} app(s)",
        f"- Test examples: {len(test)} from {len(test_apps)} app(s)",
        f"- Estimated training tokens per epoch: ~{tokens_estimate(train):,}",
        "",
        "## Rows per app",
        "",
        "| app | train | test |",
        "|---|---|---|",
    ]
    for app in sorted(set(train_apps) | set(test_apps)):
        lines.append(f"| {app} | {train_apps.get(app, 0)} | {test_apps.get(app, 0)} |")

    lines += [
        "",
        "## Augmentation",
        "",
        "Per augmented copy, in this order:",
        "",
        "1. **Shuffle the element list and renumber it 1..n.** The one that",
        "   matters: it stops the model learning that the answer is usually a",
        "   particular number, which is the shortcut that would score well here",
        "   and fail on her laptop.",
        f"2. **Drop ~{int(DROP_FRACTION * 100)}% of the non-target elements**, so the",
        "   answer is not tied to a screen of a particular length. The target is",
        "   never dropped.",
        "3. **Wrap her goal** in one of a few openings she actually uses. No",
        "   paraphrase that could change what she asked for.",
        "",
        "## What this dataset does and does not label",
        "",
        "`eval/labels.jsonl` records **which elements are correct** and nothing",
        "else. So in the target JSON:",
        "",
        "- `target_id` is a **real label**.",
        "- `cannot_see_it` is a **real label** (the `expect_cannot_see` rows).",
        "- `instruction` is **templated** (\"Click <text>\").",
        "- `success_check` is **templated** and always `region_changed`.",
        "",
        "Consequences, which must be carried into any write-up:",
        "",
        "- A model trained on this may be judged on **correct-element rate and",
        "  JSON validity only**. It has not been taught to word an instruction or",
        "  to choose a success check.",
        "- Dropping it into `app/brain.py` wholesale would regress both: it would",
        "  learn to answer `region_changed` every time, and the cheap window",
        "  checks are what keep a CPU laptop responsive (MASTERSPEC 5.4).",
        "- Fixing this means adding `instruction` and `success_check` to the",
        "  labels, not changing the templates.",
        "",
    ]

    if weak:
        lines += [
            "## Honesty warning",
            "",
            "**This is not the split MASTERSPEC 9 specifies.** Only one app is",
            "labelled, so train and test are different screenshots of the *same*",
            "app. A number from this split says the model can handle another",
            "screen of File Explorer; it says nothing about an app it has never",
            "seen, which is the claim the spec wanted to test. Anywhere these",
            "numbers appear, that sentence appears with them.",
            "",
        ]

    lines += [
        "## Sample size",
        "",
        f"{len(rows)} labelled rows is small. Augmentation multiplies passes over",
        "the same screens; it does not add screens. A difference of a few points",
        "between tuned and untuned on a test set this size is noise, and should be",
        "reported as such rather than as an improvement.",
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return path


def build(copies: int = DEFAULT_COPIES, seed: int = DEFAULT_SEED,
          holdout_app: str | None = None, holdout_screenshot: bool = False,
          out_dir: Path = OUT_DIR) -> dict[str, object]:
    """Build both splits and the data card. Returns the counts, for the tests."""
    rows = load_rows()
    if not rows:
        raise SystemExit("no labelled rows in eval/labels.jsonl")

    train_rows, test_rows, how = split_rows(rows, holdout_app, holdout_screenshot)
    rng = random.Random(seed)
    train = [example for row in train_rows for example in examples_for(row, copies, rng)]
    # The test split is never augmented: it is what the model is measured on,
    # and a shuffled copy of a test row is not another test.
    test = [example for row in test_rows for example in examples_for(row, 1, rng)]
    check_no_leak(train, test)

    train_path = out_dir / TRAIN_PATH.name
    test_path = out_dir / TEST_PATH.name
    write_jsonl(train, train_path)
    write_jsonl(test, test_path)
    card = write_data_card(train, test, rows, how, copies, seed,
                           path=out_dir / DATA_CARD_PATH.name)

    log.info("wrote %d train and %d test examples (%s)", len(train), len(test), how)
    return {
        "train": len(train),
        "test": len(test),
        "how": how,
        "tokens_per_epoch": tokens_estimate(train),
        "train_path": train_path,
        "test_path": test_path,
        "data_card": card,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copies", type=int, default=DEFAULT_COPIES,
                        help=f"augmented copies per train row (default {DEFAULT_COPIES})")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--holdout-app", default=None,
                        help="which app to hold out (default: the last alphabetically)")
    parser.add_argument("--holdout-screenshot", action="store_true",
                        help="with only one app labelled, split by screenshot instead "
                             "and stamp data_card.md as a weak split")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    counts = build(copies=args.copies, seed=args.seed,
                   holdout_app=args.holdout_app,
                   holdout_screenshot=args.holdout_screenshot)
    print(f"train: {counts['train']} examples -> {counts['train_path']}")
    print(f"test:  {counts['test']} examples -> {counts['test_path']}")
    print(f"split: {counts['how']}")
    print(f"~{counts['tokens_per_epoch']:,} training tokens per epoch")
    print(f"data card: {counts['data_card']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
