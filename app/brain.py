"""The brain: Gemma picks one element and says what to do (MASTERSPEC 5.3).

CLAUDE.md rule 1: the model returns a ``target_id`` from the numbered element
list. It never returns coordinates, and no bbox ever enters the prompt --
:func:`build_messages` goes through ``elements.render_for_model``, which is the
only function allowed to turn elements into prompt text.

CLAUDE.md rule 2: the model only *proposes* a success check. ``watcher.py``
runs it. Nothing here decides whether a step worked.

The call path mirrors ``scripts/bench_gemma.py`` exactly -- same schema, same
``temperature 0``, same ``keep_alive``, same ``think=False`` -- so the timings
in ``notes/bench.md`` describe this code rather than a near-copy of it.

Every failure here ends in a valid :class:`StepPlan`. A bad reply, a schema
violation, an invalid id, a dead Ollama server: all of them become
``cannot_see_it`` with a hint. She gets a sentence she can act on instead of a
traceback.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, Sequence

from pydantic import BaseModel, Field, ValidationError

from app import config
from app.elements import Element, render_for_model

log = logging.getLogger(__name__)


class CheckType(str, Enum):
    """The deterministic success checks from MASTERSPEC 5.4.

    The model proposes which check to run; ``watcher.py`` runs it. The model
    never judges success itself.
    """

    WINDOW_CLASS_IS = "window_class_is"
    WINDOW_TITLE_CONTAINS = "window_title_contains"
    TEXT_APPEARS = "text_appears"
    TEXT_DISAPPEARS = "text_disappears"
    REGION_CHANGED = "region_changed"


class SuccessCheck(BaseModel):
    """How to tell, in code, that the step worked."""

    type: CheckType
    value: str = Field(default="", max_length=120)


class StepPlan(BaseModel):
    """One step: what to say, what to circle, and how to know it worked.

    Mirrors the JSON in MASTERSPEC 5.3 exactly.
    """

    #: Plain words for a non-technical reader. Capped at 90 chars by MASTERSPEC.
    instruction: str = Field(max_length=90)
    #: An id from this step's numbered element list. 0 means "none", which is
    #: only valid when goal_reached or cannot_see_it is true; validate_plan
    #: enforces that the id actually exists in the list.
    target_id: int = Field(ge=0)
    success_check: SuccessCheck
    goal_reached: bool = False
    cannot_see_it: bool = False
    #: Shown instead of a circle when cannot_see_it is true.
    hint_if_missing: str = Field(default="", max_length=90)


#: Longest hint the schema accepts, so fallbacks can be truncated to fit.
MAX_HINT_CHARS = 90

#: MASTERSPEC 5.2 / Tier 2: only the closest teach notes go in, to keep the
#: prompt short on a CPU laptop.
MAX_TEACH_NOTES = 3

#: Shown when the model cannot be reached or will not produce a usable plan.
#: Plain words, no jargon, and something she can actually try.
GENERIC_HINT = "I could not work this one out. Try scrolling, or open the menu at the top."


class PlanInvalid(Exception):
    """A model reply that did not survive validation.

    The message is written to be fed back to the model verbatim on the retry,
    so it names the specific problem rather than just "invalid".
    """


class ChatClient(Protocol):
    """The slice of ``ollama.Client`` this module uses."""

    def chat(self, **kwargs: Any) -> Any: ...


# --- the prompt ------------------------------------------------------------


def build_system_prompt(language: str | None = None) -> str:
    """The standing instructions. Short on purpose: every token costs her a wait.

    MASTERSPEC 5.3 and CLAUDE.md rule 5 shape the content. Note what is *not*
    here: no permissions ("do it for me" rules live in ``guard.py``, rule 4)
    and no success-check judgement (rule 2).
    """
    language = language or config.LANGUAGE
    return (
        "You help a non-technical person use a Windows laptop. She is not "
        "technical: no jargon, no file paths, no keyboard shortcuts unless "
        "there is no other way.\n"
        "You get her goal, the steps she has already done, and a numbered list "
        "of what is on her screen right now.\n"
        "Pick exactly ONE line from that list and return its number as "
        "target_id. Never use a number that is not in the list.\n"
        "Write instruction as one short, plain sentence telling her what to do "
        f"with that one thing, in {language}, under 90 characters.\n"
        "Choose the success_check that proves the step worked.\n"
        "If the right thing is genuinely not in the list, set cannot_see_it "
        "true and put one short suggestion in hint_if_missing.\n"
        "Never ask her for a password, OTP, PIN or card number."
    )


def _history_block(history: Sequence[str]) -> str:
    return "\n".join(f"{index}. {step}" for index, step in enumerate(history, start=1))


def build_messages(
    goal: str,
    elements: Sequence[Element],
    history: Sequence[str] = (),
    teach_notes: Sequence[str] = (),
    language: str | None = None,
) -> list[dict[str, str]]:
    """Build the two-message prompt.

    ``elements`` must already be numbered (``elements.number_elements``); the
    ids in the prompt are what the model returns as ``target_id``.

    Sections that have no content are left out entirely rather than sent empty,
    so the model never sees "Steps already done:" followed by nothing and
    invents one.
    """
    parts = [f"Her goal: {goal.strip()}"]
    if history:
        parts.append(f"Steps she has already done:\n{_history_block(history)}")
    if teach_notes:
        notes = "\n".join(f"- {note}" for note in teach_notes[:MAX_TEACH_NOTES])
        parts.append(f"Things to remember about her setup:\n{notes}")
    parts.append(f"On screen now:\n{render_for_model(elements)}")
    parts.append("Give the next single step.")

    return [
        {"role": "system", "content": build_system_prompt(language)},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


# --- validation ------------------------------------------------------------


def validate_plan(content: str, valid_ids: set[int]) -> StepPlan:
    """Parse and check one model reply, or raise :class:`PlanInvalid`.

    Two layers, in this order:

    1. pydantic: shape, the 90-character instruction cap, the check-type enum.
    2. this function: the id must exist in *this step's* list, which pydantic
       cannot know. Skipped when the model is not pointing at anything --
       ``goal_reached`` or ``cannot_see_it``.
    """
    if not content or not content.strip():
        raise PlanInvalid("You returned nothing. Return the JSON object.")

    try:
        plan = StepPlan.model_validate_json(content)
    except ValidationError as exc:
        raise PlanInvalid(_readable(exc)) from exc
    except ValueError as exc:  # not JSON at all
        raise PlanInvalid(f"That was not valid JSON ({exc}). Return only the JSON object.") from exc

    if plan.goal_reached or plan.cannot_see_it:
        return plan

    if plan.target_id not in valid_ids:
        raise PlanInvalid(
            f"target_id {plan.target_id} is not in the list. "
            "Use one of the numbers shown, or set cannot_see_it true."
        )
    return plan


def _readable(exc: ValidationError) -> str:
    """Flatten a pydantic error into one line the model can act on."""
    problems = [
        f"{'.'.join(str(part) for part in error['loc']) or 'value'}: {error['msg']}"
        for error in exc.errors()[:4]
    ]
    return "; ".join(problems)


def fallback_plan(hint: str = GENERIC_HINT) -> StepPlan:
    """A valid plan that circles nothing and shows ``hint`` instead.

    MASTERSPEC 5.3: when ``cannot_see_it`` is true there is no circle, so the
    ``success_check`` here is never run -- ``watcher.py`` shows the hint and
    waits for her instead. ``region_changed`` is recorded as the least harmful
    placeholder, not as something to poll.
    """
    return StepPlan(
        instruction=hint[:MAX_HINT_CHARS],
        target_id=0,
        success_check=SuccessCheck(type=CheckType.REGION_CHANGED, value=""),
        goal_reached=False,
        cannot_see_it=True,
        hint_if_missing=hint[:MAX_HINT_CHARS],
    )


# --- the call --------------------------------------------------------------


@dataclass(frozen=True)
class BrainResult:
    """One planning attempt, with the numbers MASTERSPEC 8 wants to trace.

    Only numbers and enums, per CLAUDE.md rule 7: these fields are safe to put
    on a Sentry span. The plan itself is not.
    """

    plan: StepPlan
    elapsed_ms: float
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    #: True when the first reply failed validation and we asked again.
    retried: bool = False
    #: True when ``plan`` is a synthesised fallback rather than the model's.
    fell_back: bool = False


_client: ChatClient | None = None


def _default_client() -> ChatClient:
    """Create the Ollama client once and keep it."""
    global _client
    if _client is None:
        import ollama

        _client = ollama.Client(timeout=config.MODEL_TIMEOUT_S)
    return _client


def _content_of(response: Any) -> str:
    message = getattr(response, "message", None)
    return (getattr(message, "content", None) or "") if message else ""


def _token_counts(response: Any) -> tuple[int | None, int | None]:
    prompt = getattr(response, "prompt_eval_count", None)
    output = getattr(response, "eval_count", None)
    return (
        int(prompt) if prompt is not None else None,
        int(output) if output is not None else None,
    )


def _chat(client: ChatClient, model: str, messages: list[dict[str, str]], schema: dict) -> Any:
    """One structured, deterministic call, matching ``scripts/bench_gemma.py``.

    MASTERSPEC 5.3 says to turn Gemma's thinking text off. Some Ollama server
    and model builds reject the ``think`` parameter outright -- the benchmark
    script hit exactly that -- so a rejection is retried without it rather than
    being treated as a dead server.

    The retry is per call rather than remembered for the process: cached
    capability state made the test suite order-dependent, and the wasted call
    fails locally and immediately, before any tokens are generated.
    """
    kwargs = dict(
        model=model,
        messages=messages,
        format=schema,
        options={"temperature": 0},
        keep_alive=config.KEEP_ALIVE,
    )
    try:
        return client.chat(think=False, **kwargs)
    except Exception as exc:
        if "think" not in str(exc).lower():
            raise
        log.info("this build rejects think=False (%s); calling without it", exc)
        return client.chat(**kwargs)


def plan_step(
    goal: str,
    elements: Sequence[Element],
    history: Sequence[str] = (),
    teach_notes: Sequence[str] = (),
    client: ChatClient | None = None,
    model: str | None = None,
) -> BrainResult:
    """Ask the model for the next step. Always returns a usable plan.

    One model call, plus at most one retry when the reply fails validation --
    MASTERSPEC 5.3. A transport failure is *not* retried: a server that is not
    running will not be running a second later, and she would wait twice the
    timeout to learn the same thing.

    ``guard.py`` must have cleared the screen before this is called
    (CLAUDE.md rule 3). This function does not check that itself, because a
    guard that can be forgotten in one place is not a guard; P7 wires it into
    the single path that reaches here.
    """
    started = time.perf_counter()

    def elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000.0

    if not elements:
        # Nothing to choose from, so there is no question worth asking. Saves
        # her a multi-second wait for an answer that could only be a guess.
        log.info("no elements on screen; skipping the model call")
        return BrainResult(
            plan=fallback_plan("I cannot read anything on this screen yet."),
            elapsed_ms=elapsed_ms(),
            fell_back=True,
        )

    client = client or _default_client()
    model = model or config.MODEL
    valid_ids = {element.id for element in elements}
    messages = build_messages(goal, elements, history, teach_notes)
    schema = StepPlan.model_json_schema()

    prompt_tokens: int | None = None
    output_tokens: int | None = None
    retried = False

    for attempt in (1, 2):
        try:
            response = _chat(client, model, messages, schema)
        except Exception:
            log.exception("Ollama call failed; falling back to a hint")
            return BrainResult(
                plan=fallback_plan(),
                elapsed_ms=elapsed_ms(),
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                retried=retried,
                fell_back=True,
            )

        content = _content_of(response)
        prompt_tokens, output_tokens = _token_counts(response)

        try:
            plan = validate_plan(content, valid_ids)
        except PlanInvalid as invalid:
            log.warning("attempt %d rejected: %s", attempt, invalid)
            if attempt == 1:
                # MASTERSPEC 5.3: one retry, with the specific error appended so
                # the model can correct itself rather than guess again.
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"That was not usable: {invalid} Try again."},
                ]
                retried = True
                continue
            return BrainResult(
                plan=fallback_plan(),
                elapsed_ms=elapsed_ms(),
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                retried=True,
                fell_back=True,
            )

        return BrainResult(
            plan=plan,
            elapsed_ms=elapsed_ms(),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            retried=retried,
            fell_back=False,
        )

    raise AssertionError("unreachable: the loop returns on both attempts")
