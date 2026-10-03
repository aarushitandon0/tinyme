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
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
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

#: Where her teach notes live (MASTERSPEC 7, Tier 2): one plain line per fact
#: about her laptop that no screenshot can tell us -- her printer's name, which
#: browser she actually uses, where she keeps photos. Plain text, not JSON, so
#: she or I can edit it in Notepad without breaking anything.
TEACH_NOTES_PATH = Path("notes") / "her_setup.txt"

#: Words that appear in goals and notes alike and say nothing about the
#: subject. Short tokens are dropped by length, so this only needs the common
#: long ones.
_NOTE_STOPWORDS = frozenset("""
about again always also been come down find from have help here just know
like more much need open please should some something than that them then
there these they this those very want what when where which with would your
""".split())

#: Shortest word allowed to connect a goal to a note. Three-letter words
#: ("the", "pdf", "one") either say nothing or match everything.
MIN_NOTE_WORD = 4

#: Splits text into words for note matching.
_NOTE_WORDS = re.compile(r"[^a-z0-9]+")

#: How alike two words must be to count as the same word. This absorbs the way
#: she types -- "print this page" against a note about her printer, "scans"
#: against "scanned" -- and nothing looser: at 70 "photos" starts matching
#: "Documents" through sheer letter overlap.
NOTE_WORD_RATIO = 80.0


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


def load_teach_notes(path: Path | str | None = None) -> list[str]:
    """Read her setup notes: one fact per line, blanks and ``#`` ignored.

    A missing file is the normal case on a fresh checkout, not an error -- it
    just means nobody has told Tiny Me anything about this laptop yet.
    """
    path = Path(path) if path is not None else TEACH_NOTES_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []

    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _content_words(text: str) -> list[str]:
    """The words in a goal or a note that are about something."""
    words = _NOTE_WORDS.split((text or "").casefold())
    return [word for word in words
            if len(word) >= MIN_NOTE_WORD and word not in _NOTE_STOPWORDS]


def _same_word(a: str, b: str) -> bool:
    """Is this her word for that thing? ("print" and "printer", "scans" and "scanned")"""
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= MIN_NOTE_WORD and longer.startswith(shorter):
        return True

    from rapidfuzz import fuzz

    return fuzz.ratio(a, b) >= NOTE_WORD_RATIO


def notes_mentioning(goal_words: Sequence[str], note: str) -> int:
    """How many of her words this note talks about."""
    note_words = _content_words(note)
    return sum(1 for word in goal_words
               if any(_same_word(word, other) for other in note_words))


def matching_notes(
    goal: str,
    notes: Sequence[str],
    limit: int = MAX_TEACH_NOTES,
) -> list[str]:
    """The notes worth sending with *this* goal, best first (P8).

    Her whole notes file would fit in the prompt, so the filtering is not about
    size -- it is about relevance. "Her printer is the HP in the study" has no
    business in a step about zooming a document: it spends her wait on tokens
    that can only mislead.

    The rule is a shared subject word, not a similarity score, and that is a
    decision worth recording. A whole-string fuzzy score does not separate
    these two cases: measured against the shipped example file, a goal about
    photos scores 48 against the photos note and 44 against an unrelated one,
    so any threshold that keeps the right note keeps most of the wrong ones
    too. The notes are sentences and the goals are fragments; what they share
    when they are about the same thing is a *word*. Fuzziness lives at the word
    level instead, where "print" and "printer" are plainly the same subject and
    "photos" and "documents" are plainly not.

    Ranked by how many of her words a note mentions, then by the order she
    wrote them -- on a tie, her ordering is the only signal left.
    """
    goal_words = _content_words(goal)
    if not goal_words or not notes:
        return []

    scored = [(notes_mentioning(goal_words, note), index, note)
              for index, note in enumerate(notes)]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [note for shared, _, note in scored if shared][:limit]


def notes_for_goal(goal: str, path: Path | str | None = None) -> list[str]:
    """Load the notes file and return the lines that match this goal."""
    return matching_notes(goal, load_teach_notes(path))


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


def warm_up(client: ChatClient | None = None, model: str | None = None) -> None:
    """Make Ollama load the model now, rather than inside her first step.

    The same move ``uia.warm_up`` makes for COM, for the same reason and two
    orders of magnitude more of it. Measured on the dev laptop and recorded in
    notes/bench.md: loading ``gemma4:e2b`` costs **31.8 s**, where a warm call
    adds **0.9 s**. A real 80-element step costs ~26 s of inference on top, so
    the first call of a session came to ~58-61 s against
    ``config.MODEL_TIMEOUT_S`` of 60 -- and the read timeout that followed was
    turned into a ``cannot_see_it`` fallback by :func:`plan_step`. The visible
    symptom was that her *first* step of every session was a generic hint
    instead of a circle, and every step after it was fine.

    ``num_predict=1`` because the reply is discarded; only the load matters.
    ``keep_alive`` is what makes the load outlive this call.

    This must not raise. A laptop with Ollama switched off should still start
    the app and still guide her as far as the fallback hints allow, which is
    exactly what ``plan_step`` already degrades to.

    Expect this to take ~30 s on a cold server, so call it off the GUI thread.
    """
    client = client or _default_client()
    model = model or config.MODEL
    started = time.perf_counter()
    try:
        client.chat(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            keep_alive=config.KEEP_ALIVE,
            options={"num_predict": 1},
        )
    except Exception as exc:
        # Not .exception(): a missing Ollama is an ordinary state on her
        # laptop, not a bug worth a stack trace at startup.
        log.info("model warm-up skipped (%s); the first step will pay the load", exc)
        return
    log.info("model %s warm in %.0f ms", model, (time.perf_counter() - started) * 1000)
