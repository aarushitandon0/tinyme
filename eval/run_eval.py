"""Score the systems against the frozen screens and write ``eval/results.md`` (P10).

    python eval\\run_eval.py --systems all
    python eval\\run_eval.py --systems pipeline_e2b
    python eval\\run_eval.py --systems all --external     # plus a closed model

Three systems, all answering the same question -- *which thing on this screen
should she click next?* -- from the same frozen screens, so the comparison is
about the approach and not about what happened to be on screen that minute:

``pipeline_<tag>``
    What the app does. The frozen element list goes in as text, numbered, with no
    coordinates anywhere (CLAUDE.md rule 1), and Gemma returns a ``target_id``.
    This is ``brain.plan_step`` itself, not a copy of it -- including its schema,
    its one retry and its fallback -- so a bad score here is a bad score for the
    shipped code.

``vision_coords_<tag>``
    The obvious alternative: hand the same model the screenshot and ask where to
    click. Scored generously -- any pixel inside a correct element's box counts.

``external_<model>``
    Optional, behind ``--external``, for one closed multimodal model. Off by
    default: it is the only thing in this repo that sends a screen anywhere, it
    needs the developer's own key, and it only ever sees the developer's own
    dummy screens.

``tinker_base`` / ``tinker_tuned``
    The P13 comparison (MASTERSPEC 9): the same prompt, answered by a small open
    model hosted on Tinker, untuned and then LoRA-tuned by
    ``finetune/tinker_train.py``. No screenshot, no coordinates. These need
    ``pip install tinker tinker-cookbook`` and ``TINKER_API_KEY``, and their
    latencies are a network round trip rather than a local model -- the
    generated ``results.md`` says so where the numbers appear.

**Nothing here writes ``results.md`` by hand.** The file is generated, every
number in it comes from a run, and the counts are printed as ``n/N`` because
40-odd rows cannot carry a percentage on their own (tinyme-eval skill).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import brain as brain_mod  # noqa: E402
from app import config  # noqa: E402
from app.elements import Element  # noqa: E402

log = logging.getLogger("run_eval")

EVAL_DIR = REPO_ROOT / "eval"
LABELS_PATH = EVAL_DIR / "labels.jsonl"
SCREENSHOT_DIR = EVAL_DIR / "screenshots"
ELEMENTS_DIR = EVAL_DIR / "elements"
RESULTS_PATH = EVAL_DIR / "results.md"

#: Default systems for ``--systems all``. The two pipeline sizes plus the
#: coordinate baseline on the *larger* model: if asking for coordinates is going
#: to work at all, it works better with more model, and a baseline should be
#: given its best shot.
DEFAULT_SYSTEMS = ("pipeline_e2b", "pipeline_e4b", "vision_coords_e4b")

#: Schema for the coordinate baseline. ``can_see`` exists so this system can also
#: answer "it is not on this screen". Without it the ``expect_cannot_see`` rows
#: would be unanswerable by construction and the baseline would be beaten by a
#: rule of the scoring rather than by the pipeline.
COORDS_SCHEMA = {
    "type": "object",
    "properties": {
        "can_see": {"type": "boolean"},
        "x": {"type": "integer"},
        "y": {"type": "integer"},
    },
    "required": ["can_see", "x", "y"],
}

COORDS_SYSTEM_PROMPT = (
    "You help someone use a Windows laptop. You are shown a screenshot and what "
    "she wants to do. Reply with the pixel she should click next, as JSON: "
    '{"can_see": true, "x": <int>, "y": <int>}, measured from the top-left of '
    'the image. If the thing she needs is not visible on this screen, reply '
    '{"can_see": false, "x": 0, "y": 0}. Give one click, not a plan.'
)


# --- the dataset -----------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One labelled (screen, goal) pair from ``labels.jsonl``."""

    id: str
    app: str
    goal: str
    screenshot: Path
    elements: list[Element]
    #: Ids any of which counts as right. More than one is normal: a menu item and
    #: its icon are the same click to her.
    correct_ids: frozenset[int]
    history: tuple[str, ...] = ()
    #: True when the right answer is "it is not on this screen". The pipeline
    #: answers this with ``cannot_see_it``.
    expect_cannot_see: bool = False
    #: Screen-space origin of the image, so a bbox can be turned into image
    #: pixels for the coordinate baseline.
    monitor_origin: tuple[int, int] = (0, 0)

    def by_id(self, target_id: int) -> Element | None:
        for element in self.elements:
            if element.id == target_id:
                return element
        return None

    def correct_boxes_in_image(self) -> list[tuple[float, float, float, float]]:
        """The correct elements' boxes, shifted into image pixel space."""
        offset_x, offset_y = self.monitor_origin
        boxes = []
        for element in self.elements:
            if element.id in self.correct_ids:
                left, top, right, bottom = element.bbox_px
                boxes.append((left - offset_x, top - offset_y,
                              right - offset_x, bottom - offset_y))
        return boxes


def _element_from_json(raw: dict) -> Element:
    return Element(
        id=int(raw["id"]),
        text=raw["text"],
        source=raw["source"],
        role=raw["role"],
        region=raw["region"],
        bbox_px=tuple(float(value) for value in raw["bbox_px"]),  # type: ignore[arg-type]
    )


def load_rows(labels_path: Path = LABELS_PATH) -> list[Row]:
    """Read the label file and the frozen element lists it points at.

    Validation is strict and loud. A label row whose ``correct_ids`` are not in
    the element list it names scores every system as wrong, which looks exactly
    like a model failure in the table -- so it is a crash here instead.
    """
    if not labels_path.exists():
        raise SystemExit(
            f"{labels_path} does not exist. Collect screens with eval/collect.py "
            "and label them first."
        )

    rows: list[Row] = []
    frozen_cache: dict[str, dict] = {}
    for line_number, line in enumerate(labels_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{labels_path}:{line_number}: {exc}") from exc

        name = raw["elements"]
        if name not in frozen_cache:
            path = ELEMENTS_DIR / name
            if not path.exists():
                raise SystemExit(f"{labels_path}:{line_number}: no such element file {path}")
            frozen_cache[name] = json.loads(path.read_text(encoding="utf-8"))
        frozen = frozen_cache[name]

        elements = [_element_from_json(item) for item in frozen["elements"]]
        correct = frozenset(int(value) for value in raw.get("correct_ids", ()))
        expect_cannot_see = bool(raw.get("expect_cannot_see", False))

        known = {element.id for element in elements}
        unknown = correct - known
        if unknown:
            raise SystemExit(
                f"{labels_path}:{line_number}: correct_ids {sorted(unknown)} are not in {name}"
            )
        if not correct and not expect_cannot_see:
            raise SystemExit(
                f"{labels_path}:{line_number}: no correct_ids and expect_cannot_see is false, "
                "so nothing could ever be right"
            )
        if correct and expect_cannot_see:
            raise SystemExit(
                f"{labels_path}:{line_number}: expect_cannot_see is true but correct_ids "
                "are given; pick one"
            )

        monitor = frozen.get("monitor", {})
        rows.append(Row(
            id=raw["id"],
            app=raw.get("app") or frozen.get("app", "unknown"),
            goal=raw["goal"],
            screenshot=SCREENSHOT_DIR / raw["screenshot"],
            elements=elements,
            correct_ids=correct,
            history=tuple(raw.get("history", ())),
            expect_cannot_see=expect_cannot_see,
            monitor_origin=(int(monitor.get("left", 0)), int(monitor.get("top", 0))),
        ))
    return rows


# --- what one answer looks like, whatever produced it ----------------------


@dataclass
class Answer:
    """One system's attempt at one row, scored."""

    row_id: str
    app: str
    correct: bool
    #: True when the reply parsed and validated. A system that returns prose
    #: scores zero *and* should be visible as having returned prose.
    valid: bool
    elapsed_ms: float
    retried: bool = False
    #: What it chose, in words, for the failure list. Not a prompt input.
    chose: str = ""
    #: Why it was marked wrong, when a mechanical reason can be given.
    reason: str = ""
    prompt_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class SystemResult:
    name: str
    model: str
    answers: list[Answer] = field(default_factory=list)
    #: Rows that could not be attempted at all (no image for a vision system).
    skipped: int = 0

    @property
    def attempted(self) -> int:
        return len(self.answers)

    @property
    def correct(self) -> int:
        return sum(1 for answer in self.answers if answer.correct)

    @property
    def valid(self) -> int:
        return sum(1 for answer in self.answers if answer.valid)

    @property
    def retried(self) -> int:
        return sum(1 for answer in self.answers if answer.retried)

    def latency(self, percentile: float) -> float:
        values = sorted(answer.elapsed_ms for answer in self.answers)
        if not values:
            return 0.0
        if percentile <= 0:
            return values[0]
        index = min(len(values) - 1, int(round(percentile * (len(values) - 1))))
        return values[index]

    @property
    def median_ms(self) -> float:
        values = [answer.elapsed_ms for answer in self.answers]
        return statistics.median(values) if values else 0.0

    def per_app(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for answer in self.answers:
            right, total = out.get(answer.app, (0, 0))
            out[answer.app] = (right + int(answer.correct), total + 1)
        return out

    def failures(self) -> list[Answer]:
        return [answer for answer in self.answers if not answer.correct]


# --- system 1: the pipeline, exactly as the app runs it --------------------


def describe(element: Element | None) -> str:
    if element is None:
        return "nothing"
    return f'#{element.id} "{element.text}" ({element.source}:{element.role}, {element.region})'


def pipeline_reason(row: Row, plan: brain_mod.StepPlan, chosen: Element | None) -> str:
    """A mechanical one-line reason a pick was wrong. No guessing at intent.

    Every branch below is a statement about the two elements, not a theory about
    the model. The catch-all says so plainly rather than inventing a cause -- the
    tinyme-eval skill asks for a reason per failure, and "picked something else"
    is the honest one when nothing more specific is true.
    """
    expected = [row.by_id(target_id) for target_id in sorted(row.correct_ids)]
    expected = [element for element in expected if element is not None]

    if row.expect_cannot_see:
        return "the target is not on this screen, but it picked something anyway"
    if plan.cannot_see_it:
        return "said it could not see the target, which is on this screen"
    if plan.goal_reached:
        return "called the goal reached while a step was still needed"
    if chosen is None:
        return "returned no usable target"
    for element in expected:
        if element.text.casefold() == chosen.text.casefold():
            if element.region != chosen.region:
                return f"right words, wrong place: took the one in {chosen.region}"
            if element.source != chosen.source:
                return (f"picked the {chosen.source} copy of the right text, "
                        f"not the {element.source} control")
    if expected and all(element.source == "uia" for element in expected) \
            and chosen.source == "ocr":
        return "picked the text label instead of the control that can be clicked"
    if expected and chosen.region == expected[0].region:
        return "picked a different thing in the right part of the screen"
    return "picked something else"


def run_pipeline(row: Row, model: str, client: Any) -> Answer:
    # Latency comes from brain's own measurement, so the pipeline and the
    # baseline are timing the same thing: one model call, nothing around it.
    result = brain_mod.plan_step(
        row.goal,
        row.elements,
        history=row.history,
        client=client,
        model=model,
    )
    plan = result.plan
    chosen = row.by_id(plan.target_id)

    if row.expect_cannot_see:
        correct = bool(plan.cannot_see_it)
    else:
        correct = (not plan.cannot_see_it) and plan.target_id in row.correct_ids

    return Answer(
        row_id=row.id,
        app=row.app,
        correct=correct,
        # fell_back means neither the first reply nor the retry validated, so the
        # model never produced usable JSON for this row.
        valid=not result.fell_back,
        elapsed_ms=result.elapsed_ms,
        retried=result.retried,
        chose="cannot_see_it" if plan.cannot_see_it else describe(chosen),
        reason="" if correct else pipeline_reason(row, plan, chosen),
        prompt_tokens=result.prompt_tokens,
        output_tokens=result.output_tokens,
    )


# --- system 2: the same model, given the picture, asked for a pixel --------


def point_in_boxes(x: float, y: float,
                   boxes: Iterable[tuple[float, float, float, float]]) -> bool:
    return any(left <= x <= right and top <= y <= bottom
               for left, top, right, bottom in boxes)


def coords_messages(row: Row, image_bytes: bytes) -> list[dict[str, Any]]:
    user = f"She wants to: {row.goal}"
    if row.history:
        done = "\n".join(f"{i}. {step}" for i, step in enumerate(row.history, 1))
        user += f"\n\nShe has already done:\n{done}"
    return [
        {"role": "system", "content": COORDS_SYSTEM_PROMPT},
        {"role": "user", "content": user, "images": [image_bytes]},
    ]


def run_vision_coords(row: Row, model: str, client: Any) -> Answer:
    """Ask for a pixel and check whether it landed on a correct element.

    Scored in the baseline's favour at every choice: any pixel inside the box
    counts (not just the centre), several boxes can be correct, and a wrong
    answer still counts as valid JSON if it parsed. If this loses, it is not
    losing on a technicality.
    """
    image_bytes = row.screenshot.read_bytes()
    started = time.perf_counter()
    try:
        response = client.chat(
            model=model,
            messages=coords_messages(row, image_bytes),
            format=COORDS_SCHEMA,
            options={"temperature": 0},
            keep_alive=config.KEEP_ALIVE,
        )
    except Exception as exc:
        log.warning("%s: vision call failed: %s", row.id, exc)
        return Answer(row.id, row.app, correct=False, valid=False,
                      elapsed_ms=(time.perf_counter() - started) * 1000,
                      chose="call failed", reason=f"the model call failed ({type(exc).__name__})")

    elapsed_ms = (time.perf_counter() - started) * 1000
    message = getattr(response, "message", None)
    content = (getattr(message, "content", None) or "") if message else ""

    try:
        parsed = json.loads(content)
        can_see = bool(parsed["can_see"])
        x, y = float(parsed["x"]), float(parsed["y"])
    except Exception:
        return Answer(row.id, row.app, correct=False, valid=False, elapsed_ms=elapsed_ms,
                      chose=content[:60].replace("\n", " "),
                      reason="reply was not the JSON that was asked for")

    prompt_tokens = getattr(response, "prompt_eval_count", None)
    output_tokens = getattr(response, "eval_count", None)

    if row.expect_cannot_see:
        correct = not can_see
        reason = "" if correct else "pointed at a pixel although the target is not on this screen"
        chose = "can_see: false" if not can_see else f"({x:.0f}, {y:.0f})"
    elif not can_see:
        correct = False
        reason = "said it could not see the target, which is on this screen"
        chose = "can_see: false"
    else:
        boxes = row.correct_boxes_in_image()
        correct = point_in_boxes(x, y, boxes)
        chose = f"({x:.0f}, {y:.0f})"
        reason = "" if correct else "the pixel is not inside any correct element"

    return Answer(row.id, row.app, correct=correct, valid=True, elapsed_ms=elapsed_ms,
                  chose=chose, reason=reason,
                  prompt_tokens=int(prompt_tokens) if prompt_tokens else None,
                  output_tokens=int(output_tokens) if output_tokens else None)


# --- system 3: optional closed model, developer's own screens only ---------


EXTERNAL_BASE_URL = os.environ.get("TINYME_EXTERNAL_BASE_URL", "")
EXTERNAL_MODEL = os.environ.get("TINYME_EXTERNAL_MODEL", "")
EXTERNAL_KEY = os.environ.get("TINYME_EXTERNAL_KEY", "")


def run_external(row: Row) -> Answer:
    """Same question as ``vision_coords``, asked of an OpenAI-compatible endpoint.

    Deliberately plumbed with ``urllib`` and three environment variables rather
    than an SDK: no new dependency (CLAUDE.md), nothing importable from the app,
    and it cannot run unless someone sets the variables on purpose. It uploads a
    screenshot, which is why it is the only part of this repo behind a flag *and*
    behind configuration.
    """
    import base64
    import urllib.error
    import urllib.request

    payload = {
        "model": EXTERNAL_MODEL,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": COORDS_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": f"She wants to: {row.goal}"},
                {"type": "image_url", "image_url": {"url":
                    "data:image/png;base64," +
                    base64.b64encode(row.screenshot.read_bytes()).decode("ascii")}},
            ]},
        ],
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        EXTERNAL_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {EXTERNAL_KEY}"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
    except Exception as exc:
        return Answer(row.id, row.app, correct=False, valid=False,
                      elapsed_ms=(time.perf_counter() - started) * 1000,
                      chose="call failed", reason=f"the request failed ({type(exc).__name__})")
    elapsed_ms = (time.perf_counter() - started) * 1000

    try:
        parsed = json.loads(content)
        can_see = bool(parsed["can_see"])
        x, y = float(parsed["x"]), float(parsed["y"])
    except Exception:
        return Answer(row.id, row.app, correct=False, valid=False, elapsed_ms=elapsed_ms,
                      chose=str(content)[:60], reason="reply was not the JSON that was asked for")

    if row.expect_cannot_see:
        correct = not can_see
        reason = "" if correct else "pointed at a pixel although the target is not on this screen"
    elif not can_see:
        correct, reason = False, "said it could not see the target, which is on this screen"
    else:
        correct = point_in_boxes(x, y, row.correct_boxes_in_image())
        reason = "" if correct else "the pixel is not inside any correct element"
    return Answer(row.id, row.app, correct=correct, valid=True, elapsed_ms=elapsed_ms,
                  chose="can_see: false" if not can_see else f"({x:.0f}, {y:.0f})",
                  reason=reason)


# --- system 4: the Tinker-hosted picker, tuned or untuned (P13) ------------

#: The base model to sample from. MASTERSPEC 9 expected
#: ``Qwen/Qwen3-4B-Instruct-2507``; that id is gone from Tinker's lineup, and
#: the smallest instruction-tuned model there on 4 Oct 2026 is this one. See
#: ``finetune/tinker_train.py`` for the rest of the re-verified facts.
TINKER_MODEL = os.environ.get("TINYME_TINKER_MODEL", "Qwen/Qwen3.5-4B")

#: The path ``save_weights_for_sampler`` returned, printed by
#: ``finetune/tinker_train.py``. Only ``tinker_tuned`` needs it.
TINKER_CHECKPOINT = os.environ.get("TINYME_TINKER_CHECKPOINT", "")

#: Chat template family for ``tinker_cookbook.renderers.get_renderer``.
TINKER_RENDERER = os.environ.get("TINYME_TINKER_RENDERER", "qwen3_5")

_tinker_clients: dict[str, Any] = {}


def tinker_sampler(checkpoint: str) -> tuple[Any, Any]:
    """A sampling client and renderer, made once per checkpoint.

    Kept out of module import so this file still runs -- and the tests still
    pass -- on a machine with no ``tinker`` installed, which is every machine
    except the one that trains. The packages are not in ``pyproject.toml`` on
    purpose (CLAUDE.md: no new dependencies in the app's runtime path).
    """
    if checkpoint in _tinker_clients:
        return _tinker_clients[checkpoint]
    try:
        import tinker
        from tinker_cookbook.renderers import get_renderer
    except ImportError as exc:
        raise SystemExit(
            "the tinker systems need `pip install tinker tinker-cookbook` and "
            f"TINKER_API_KEY ({exc})"
        ) from exc

    service = tinker.ServiceClient()
    client = (service.create_sampling_client(model_path=checkpoint) if checkpoint
              else service.create_sampling_client(base_model=TINKER_MODEL))
    renderer = get_renderer(TINKER_RENDERER, client.get_tokenizer())
    _tinker_clients[checkpoint] = (client, renderer)
    return client, renderer


def run_tinker(row: Row, checkpoint: str) -> Answer:
    """The same prompt as the pipeline, answered by a model hosted on Tinker.

    Identical question, identical prompt (``brain.build_messages``), identical
    validation (``brain.validate_plan``) -- so a difference in the table is a
    difference between the models and not between two ways of asking. There is
    no retry here, unlike ``brain.plan_step``: the retry is a property of the
    shipped app, and giving it to the challenger would flatter it.

    **Latency is measured against Tinker's hosted sampler, over the internet.**
    It is not comparable to the pipeline rows, which are a local model on a
    laptop with no network. ``results.md`` says so next to the number; never
    quote the two side by side as if they were the same measurement.
    """
    import tinker

    client, renderer = tinker_sampler(checkpoint)
    messages = brain_mod.build_messages(row.goal, row.elements, history=row.history)
    prompt = renderer.build_generation_prompt(messages)
    params = tinker.SamplingParams(max_tokens=256, temperature=0.0)

    started = time.perf_counter()
    try:
        result = client.sample(prompt=prompt, num_samples=1, sampling_params=params)
        content, _ = renderer.parse_response(result.sequences[0].tokens)
    except Exception as exc:
        return Answer(row.id, row.app, correct=False, valid=False,
                      elapsed_ms=(time.perf_counter() - started) * 1000,
                      chose="call failed",
                      reason=f"the request failed ({type(exc).__name__})")
    elapsed_ms = (time.perf_counter() - started) * 1000

    if isinstance(content, dict):  # some renderers return a message
        content = content.get("content", "")

    valid_ids = {element.id for element in row.elements}
    try:
        plan = brain_mod.validate_plan(str(content), valid_ids)
    except brain_mod.PlanInvalid as invalid:
        return Answer(row.id, row.app, correct=False, valid=False, elapsed_ms=elapsed_ms,
                      chose=str(content)[:60], reason=f"invalid reply ({invalid})")

    chosen = row.by_id(plan.target_id)
    if row.expect_cannot_see:
        correct = bool(plan.cannot_see_it)
    else:
        correct = (not plan.cannot_see_it) and plan.target_id in row.correct_ids

    return Answer(
        row_id=row.id, app=row.app, correct=correct, valid=True,
        elapsed_ms=elapsed_ms,
        chose="cannot_see_it" if plan.cannot_see_it else describe(chosen),
        reason="" if correct else pipeline_reason(row, plan, chosen),
    )


# --- running ---------------------------------------------------------------

SYSTEM_NAME = re.compile(r"^(pipeline|vision_coords)_(.+)$")

#: The two Tinker systems. Named rather than tagged, because what distinguishes
#: them is a checkpoint path from the environment, not an Ollama tag.
TINKER_SYSTEMS = {
    "tinker_base": "",
    "tinker_tuned": "TINYME_TINKER_CHECKPOINT",
}


def resolve_systems(spec: str, external: bool) -> list[tuple[str, str, str]]:
    """Turn ``--systems`` into (name, kind, model) triples."""
    names = list(DEFAULT_SYSTEMS) if spec == "all" else [
        part.strip() for part in spec.split(",") if part.strip()
    ]
    out: list[tuple[str, str, str]] = []
    for name in names:
        if name in TINKER_SYSTEMS:
            if name == "tinker_tuned" and not TINKER_CHECKPOINT:
                raise SystemExit(
                    "tinker_tuned needs TINYME_TINKER_CHECKPOINT, the path that "
                    "finetune\\tinker_train.py printed. Without it there is nothing "
                    "tuned to score, and tinker_base is the untuned row."
                )
            out.append((name, "tinker", TINKER_MODEL))
            continue

        match = SYSTEM_NAME.match(name)
        if not match:
            raise SystemExit(
                f"unknown system {name!r}. Use pipeline_<tag>, vision_coords_<tag> "
                "(where <tag> is an Ollama tag suffix such as e2b), tinker_base or "
                "tinker_tuned."
            )
        kind, tag = match.group(1), match.group(2)
        model = tag if ":" in tag else f"gemma4:{tag}"
        out.append((name, kind, model))
    if external:
        missing = [variable for variable, value in (
            ("TINYME_EXTERNAL_BASE_URL", EXTERNAL_BASE_URL),
            ("TINYME_EXTERNAL_MODEL", EXTERNAL_MODEL),
            ("TINYME_EXTERNAL_KEY", EXTERNAL_KEY),
        ) if not value]
        if missing:
            raise SystemExit("--external needs " + ", ".join(missing))
        out.append((f"external_{EXTERNAL_MODEL}", "external", EXTERNAL_MODEL))
    return out


def run_system(name: str, kind: str, model: str, rows: Sequence[Row],
               client: Any) -> SystemResult:
    result = SystemResult(name=name, model=model)
    for index, row in enumerate(rows, start=1):
        if kind in ("vision_coords", "external") and not row.screenshot.exists():
            log.warning("%s: no screenshot at %s; skipping for %s",
                        row.id, row.screenshot, name)
            result.skipped += 1
            continue
        if kind == "pipeline":
            answer = run_pipeline(row, model, client)
        elif kind == "vision_coords":
            answer = run_vision_coords(row, model, client)
        elif kind == "tinker":
            answer = run_tinker(row, TINKER_CHECKPOINT if name == "tinker_tuned" else "")
        else:
            answer = run_external(row)
        result.answers.append(answer)
        log.info("%s %3d/%d %-18s %s %s", name, index, len(rows), row.id,
                 "ok  " if answer.correct else "WRONG", answer.chose)
    return result


# --- the report ------------------------------------------------------------


def _rate(right: int, total: int) -> str:
    """``31/40 (78%)``. The count comes first because the count is the evidence."""
    if total == 0:
        return "0/0 (-)"
    return f"{right}/{total} ({right / total * 100:.0f}%)"


def write_results(results: Sequence[SystemResult], rows: Sequence[Row],
                  machine: dict[str, str], out_path: Path = RESULTS_PATH) -> Path:
    """Generate ``results.md``. Never edit the output by hand."""
    apps = sorted({row.app for row in rows})
    lines: list[str] = []
    add = lines.append

    add("# Tiny Me — evaluation results")
    add("")
    add("**Generated by `eval/run_eval.py`. Do not edit by hand** — re-run it instead.")
    add(f"Run at {datetime.now(timezone.utc).isoformat(timespec='seconds')} (UTC).")
    add("")
    add(f"- rows: **{len(rows)}** across {len(apps)} apps ({', '.join(apps)})")
    add(f"- of those, {sum(1 for row in rows if row.expect_cannot_see)} are "
        "`expect_cannot_see` rows, where the right answer is \"it is not on this screen\"")
    add(f"- machine: {machine.get('cpu', '?')} ({machine.get('cores', '?')} cores), "
        f"{machine.get('ram', '?')} RAM, GPU: {machine.get('gpu', '?')}")
    add(f"- {machine.get('os', '?')}, Python {machine.get('python', '?')}, "
        f"Ollama server {machine.get('ollama_server', '?')}, "
        f"client {machine.get('ollama_client', '?')}")
    add("- temperature 0, identical prompts across systems, element lists frozen "
        "at collection time")
    add("")

    add("## Correct element")
    add("")
    add("| system | model | correct | valid JSON | retried | median s | p90 s |")
    add("|---|---|---|---|---|---|---|")
    for result in results:
        add(f"| `{result.name}` | {result.model} | "
            f"{_rate(result.correct, result.attempted)} | "
            f"{_rate(result.valid, result.attempted)} | "
            f"{_rate(result.retried, result.attempted)} | "
            f"{result.median_ms / 1000:.1f} | {result.latency(0.9) / 1000:.1f} |")
    add("")
    skipped = [result for result in results if result.skipped]
    if skipped:
        for result in skipped:
            add(f"> `{result.name}` skipped {result.skipped} rows with no screenshot on disk.")
        add("")

    add("## Correct element, per app")
    add("")
    add("| system | " + " | ".join(apps) + " |")
    add("|---|" + "---|" * len(apps))
    for result in results:
        per_app = result.per_app()
        cells = []
        for app in apps:
            right, total = per_app.get(app, (0, 0))
            cells.append(_rate(right, total) if total else "—")
        add(f"| `{result.name}` | " + " | ".join(cells) + " |")
    add("")

    add("## Where it went wrong")
    add("")
    add("Three failures per system, with the reason stated mechanically — what it "
        "picked against what was labelled correct. No interpretation of why the "
        "model did it.")
    add("")
    for result in results:
        failures = result.failures()
        add(f"### `{result.name}` — {len(failures)} wrong of {result.attempted}")
        add("")
        if not failures:
            add("No failures.")
            add("")
            continue
        by_row = {row.id: row for row in rows}
        for answer in failures[:3]:
            row = by_row.get(answer.row_id)
            goal = row.goal if row else "?"
            expected = ", ".join(
                describe(row.by_id(target_id)) for target_id in sorted(row.correct_ids)
            ) if row and row.correct_ids else "nothing (not on this screen)"
            add(f"- **{answer.row_id}** — goal: \"{goal}\"")
            add(f"  - chose: {answer.chose}")
            add(f"  - correct: {expected}")
            add(f"  - reason: {answer.reason}")
        add("")

    add("## What is not in these numbers")
    add("")
    add("- **Latency is the model call only.** It excludes capture and OCR, which "
        "`notes/bench.md` measures separately and which dominate a real step on "
        "this machine. A step she waits through is the sum of all three.")
    add("- **Element lists are frozen**, so a pipeline score is the picking "
        "accuracy given a good list. When OCR or UI Automation misses the target "
        "entirely on the live screen, the list never contains the right answer "
        "and this table cannot see that failure.")
    add("- **Task completion and recovery are not here.** They need the live app "
        "and a person; `eval/live_runs.md` is the hand-written log for those.")
    if any(result.name in TINKER_SYSTEMS for result in results):
        add("- **The `tinker_*` rows were timed against a hosted sampler over the "
            "internet**, and the others against a local model with the Wi-Fi off. "
            "Their correct-element rates are comparable; their latencies are not, "
            "and must never be quoted side by side as though they were. See "
            "`finetune/data_card.md` for what the tuned model was and was not "
            "taught, and `finetune/DECISION.md` for the split it was tested on.")
    add("- The coordinate baseline is scored generously: any pixel inside a "
        "correct element's box counts.")
    add("")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def print_table(results: Sequence[SystemResult]) -> None:
    print()
    print(f"{'system':<22} {'correct':>14} {'valid':>14} {'median s':>9} {'p90 s':>7}")
    for result in results:
        print(f"{result.name:<22} {_rate(result.correct, result.attempted):>14} "
              f"{_rate(result.valid, result.attempted):>14} "
              f"{result.median_ms / 1000:>9.1f} {result.latency(0.9) / 1000:>7.1f}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--systems", default="all",
                        help="comma-separated system names, or 'all' "
                             f"(default: {','.join(DEFAULT_SYSTEMS)}); also "
                             "tinker_base and tinker_tuned (P13)")
    parser.add_argument("--external", action="store_true",
                        help="also run one closed multimodal model (uploads screenshots; "
                             "needs TINYME_EXTERNAL_* in the environment)")
    parser.add_argument("--limit", type=int, default=0,
                        help="only the first N rows, for a smoke run")
    parser.add_argument("--app", default="", help="only rows for this app")
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    parser.add_argument("--no-write", action="store_true",
                        help="print the table but do not touch results.md")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    rows = load_rows()
    if args.app:
        rows = [row for row in rows if row.app == args.app]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("no rows to run")

    systems = resolve_systems(args.systems, args.external)
    log.info("%d rows, %d systems", len(rows), len(systems))

    import ollama

    from scripts.bench_gemma import machine_info

    client = ollama.Client(timeout=config.MODEL_TIMEOUT_S)
    results = [run_system(name, kind, model, rows, client) for name, kind, model in systems]

    print_table(results)
    if not args.no_write:
        path = write_results(results, rows, machine_info(), args.out)
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
