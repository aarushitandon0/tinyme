"""Sentry agent tracing, for my laptop only (MASTERSPEC 8, CLAUDE.md rule 7).

This module exists to answer one question with numbers instead of a guess:
**where does her wait actually go?** One trace per task, with a child span per
stage, so the AI Agents dashboard shows whether a step is slow because of the
model, OCR, or UI Automation.

It is also the one file in this repo that could break the privacy promise, so
it is written to make that hard rather than to make it unlikely:

* **Off unless asked.** ``init`` does nothing unless ``TINYME_TELEMETRY=1``
  *and* ``SENTRY_DSN`` is set. On her laptop neither is, so every helper here
  is a no-op context manager and ``sentry_sdk`` is never even imported.
* **Only declared keys, only declared values.** :func:`clean` drops anything
  that is not a number, a bool, or a string from a closed vocabulary
  (:func:`vocabulary`) under a key from :data:`ATTR_KEYS`. There is no code
  path that puts an arbitrary string on a span, so a goal, an instruction, a
  window title, a filename or an OCR line cannot reach Sentry by being passed
  to the wrong argument -- it would be dropped and logged.
* **No breadcrumbs, no error events, no locals.** Spans are the only thing
  that leaves. Breadcrumbs would carry our own INFO log lines, and an
  exception event would carry local variables -- and ``goal`` is a local
  variable in half the call stack. Both are switched off in :func:`init`.

The spans are created by explicit parenting (``parent.start_child(...)``)
rather than by Sentry's ambient scope, because the stages run on a QThread
worker while the task's root span was started on the main thread
(CLAUDE.md rule 8). Explicit parents are the only ones that survive that hop.

Nothing here may raise. A telemetry bug must not end her task, so every public
entry point swallows its own failures and logs them.
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from enum import Enum
from typing import Any, Iterator

from app import config

log = logging.getLogger(__name__)

#: MASTERSPEC 8: the agent span's name, which is how the trace is grouped in
#: the AI Agents dashboard.
AGENT_NAME = "Tiny Me"

#: Gemma runs through Ollama, so this is the provider Sentry should attribute
#: the call to -- not "google".
PROVIDER = "ollama"

#: Sentry's own environment tag. This build is never the one she runs.
ENVIRONMENT = "dev"


class Tool(str, Enum):
    """The stages that get an ``execute_tool`` span (MASTERSPEC 8).

    A closed set on purpose: the span name is sent to Sentry, so it has to come
    from a vocabulary rather than from a caller's f-string.
    """

    CAPTURE = "capture"
    READ_SCREEN = "read_screen"
    GUARD = "guard"
    WAIT_CHECK = "wait_check"
    FIND_FILE = "find_file"
    BOOK = "book"


class Outcome(str, Enum):
    """Span outcomes that are not already an enum somewhere else.

    ``watcher.Outcome`` covers what a poll concluded and ``TaskLoop``'s
    ``finished_because`` covers how a task ended; these are the few that have
    no home of their own.
    """

    CLEAR = "clear"
    PAUSED = "paused"
    TIMEOUT = "timeout"
    FAILED = "failed"
    OK = "ok"
    HANDED_OVER = "handed_over"
    NOT_FOUND = "not_found"


#: How ``TaskLoop._finish`` describes the end of a task. Copied as strings
#: because they are not an enum in ``main.py``; ``tests/test_telemetry.py``
#: asserts this stays in step with the real call sites.
TASK_OUTCOMES = frozenset({"goal_reached", "max_steps", "stopped"})


# --- the attribute contract ------------------------------------------------
# Sentry's gen_ai.* conventions (MASTERSPEC 8) plus our own numbers under
# tinyme.*. Every key a span may carry is listed here; anything else is a bug
# and is dropped rather than sent.

OPERATION_NAME = "gen_ai.operation.name"
AGENT_NAME_KEY = "gen_ai.agent.name"
PROVIDER_KEY = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
TOOL_NAME = "gen_ai.tool.name"

#: Keys whose value is a model tag, checked against :data:`MODEL_TAG` rather
#: than the vocabulary (the tag is configuration, not an enum).
MODEL_KEYS = frozenset({REQUEST_MODEL})

#: Keys whose value must be a number or a bool.
NUMERIC_KEYS = frozenset({
    INPUT_TOKENS,
    OUTPUT_TOKENS,
    "tinyme.capture_ms",
    "tinyme.read_ms",
    "tinyme.ocr_ms",
    "tinyme.uia_ms",
    "tinyme.model_ms",
    "tinyme.wait_ms",
    "tinyme.element_count",
    "tinyme.step_index",
    "tinyme.steps",
    "tinyme.recoveries",
    "tinyme.pauses",
    "tinyme.handovers",
    "tinyme.filled_fields",
    "tinyme.retried",
    "tinyme.valid_json",
    "tinyme.fell_back",
})

#: Keys whose value must be a word from :func:`vocabulary`.
ENUM_KEYS = frozenset({
    OPERATION_NAME,
    AGENT_NAME_KEY,
    PROVIDER_KEY,
    TOOL_NAME,
    "tinyme.check_type",
    "tinyme.outcome",
})

ATTR_KEYS = NUMERIC_KEYS | ENUM_KEYS | MODEL_KEYS

#: Longest string any attribute may be. Nothing in the vocabulary comes close;
#: the cap is a second line of defence, so that a future addition to the
#: vocabulary cannot quietly become a sentence.
MAX_ATTR_CHARS = 40

#: An Ollama tag, e.g. ``gemma4:e2b`` or ``tinyme-picker``. No whitespace, so
#: no goal, instruction, title or OCR line can pass as one.
MODEL_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,39}$")

_vocabulary: frozenset[str] | None = None


def vocabulary() -> frozenset[str]:
    """Every string an attribute is allowed to be.

    Built from the enums that already exist elsewhere -- ``brain.CheckType``
    (MASTERSPEC 5.4) and ``watcher.Outcome`` -- rather than from a copy kept
    here, so a new check type cannot be silently unsendable. Imported inside
    the function because ``watcher`` imports ``brain`` and both are imported by
    ``main`` alongside this module; a module-level import would make the order
    matter.
    """
    global _vocabulary
    if _vocabulary is None:
        from app.brain import CheckType
        from app.watcher import Outcome as PollOutcome

        _vocabulary = frozenset(
            {AGENT_NAME, PROVIDER}
            | {member.value for member in Tool}
            | {member.value for member in Outcome}
            | {member.value for member in CheckType}
            | {member.value for member in PollOutcome}
            | {"invoke_agent", "execute_tool", "chat"}
            | TASK_OUTCOMES
        )
    return _vocabulary


def _as_word(value: Any) -> str | None:
    """The enum's value if *value* is an enum, else the string itself."""
    if isinstance(value, Enum):
        value = value.value
    return value if isinstance(value, str) else None


def clean(key: str, value: Any) -> Any | None:
    """The value that may go on a span under *key*, or None to drop it.

    This is the whole of CLAUDE.md rule 7 in one function, and the only reason
    the guardrails review of this file is short: there is no other way for a
    value to reach a span.
    """
    if key not in ATTR_KEYS:
        return None

    if key in NUMERIC_KEYS:
        # bool before int: bool is an int subclass, and ``retried`` wants to
        # arrive as true/false rather than as 1/0.
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            return round(float(value), 1)
        return None

    word = _as_word(value)
    if word is None or len(word) > MAX_ATTR_CHARS:
        return None
    if key in MODEL_KEYS:
        return word if MODEL_TAG.match(word) else None
    return word if word in vocabulary() else None


# --- init ------------------------------------------------------------------

_started = False


def is_enabled() -> bool:
    """True only on a machine that asked for telemetry and has somewhere to send it."""
    return bool(config.TELEMETRY_ENABLED and os.environ.get("SENTRY_DSN"))


def _drop_event(event: Any, hint: Any) -> None:
    """Send no error events at all; spans are the entire payload.

    An exception event carries local variables and log breadcrumbs, and
    ``goal`` is a local variable in most of the call stack. Rather than trying
    to scrub that, this build never sends one. Stack traces stay in the console
    on my own machine, where they are more use anyway.
    """
    return None


def init() -> bool:
    """Start Sentry if this machine asked for it. Idempotent; never raises.

    Returns True when tracing is live, which ``main`` logs so that a run with
    telemetry silently off is obvious in the log rather than at the end of an
    eval.
    """
    global _started
    if _started:
        return True
    if not is_enabled():
        log.debug("telemetry off (TINYME_TELEMETRY/SENTRY_DSN not set)")
        return False

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=os.environ["SENTRY_DSN"],
            environment=ENVIRONMENT,
            traces_sample_rate=1.0,
            # MASTERSPEC 6 / CLAUDE.md rule 7, in four lines:
            send_default_pii=False,
            max_breadcrumbs=0,          # our own INFO lines would ride along
            attach_stacktrace=False,
            include_local_variables=False,
            before_send=_drop_event,    # spans only
        )
    except Exception:
        log.exception("could not start telemetry; carrying on without it")
        return False

    _started = True
    log.info("telemetry on: agent spans only, numbers and enums only")
    return True


def flush(timeout_s: float = 2.0) -> None:
    """Push anything buffered. For eval runs, which exit immediately after."""
    if not _started:
        return
    try:
        import sentry_sdk

        sentry_sdk.flush(timeout=timeout_s)
    except Exception:
        log.debug("telemetry flush failed", exc_info=True)


# --- spans -----------------------------------------------------------------


class SpanHandle:
    """One span, with an attribute setter that cannot be misused.

    Wrapping Sentry's span rather than handing it out keeps ``set_data`` -- which
    accepts any object -- out of reach of the rest of the app. Everything goes
    through :func:`clean`.
    """

    __slots__ = ("_span",)

    def __init__(self, span: Any = None) -> None:
        self._span = span

    @property
    def live(self) -> bool:
        return self._span is not None

    def set(self, key: str, value: Any) -> None:
        if self._span is None or value is None:
            return
        cleaned = clean(key, value)
        if cleaned is None:
            log.warning("telemetry dropped attribute %r (not a declared number or enum)", key)
            return
        try:
            self._span.set_data(key, cleaned)
        except Exception:
            log.debug("could not set span attribute %r", key, exc_info=True)

    def set_many(self, attrs: dict[str, Any]) -> None:
        for key, value in attrs.items():
            self.set(key, value)


#: Yielded when tracing is off, so callers never branch on it.
NULL_SPAN = SpanHandle(None)


class TaskTrace:
    """One trace per task (MASTERSPEC 8): an ``invoke_agent`` span and children.

    Created on the main thread when she starts a task, finished when the task
    ends. In between, the stage spans are opened from the worker thread, which
    is why children are started from this object's span rather than from
    Sentry's current scope.

    A disabled trace (``TaskTrace()``, or :data:`NO_TRACE`) has the same methods
    and does nothing, so no call site has an ``if telemetry.is_enabled()`` in it.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Any = None) -> None:
        self._root = root

    @property
    def live(self) -> bool:
        return self._root is not None

    def _child(self, op: str, name: str, **kwargs: Any) -> Any:
        if self._root is None:
            return None
        try:
            return self._root.start_child(op=op, name=name, **kwargs)
        except Exception:
            log.debug("could not start a %s span", op, exc_info=True)
            return None

    @contextmanager
    def tool(self, tool: Tool, **attrs: Any) -> Iterator[SpanHandle]:
        """An ``execute_tool`` span around one stage of the pipeline.

        Attributes may be passed up front or set on the handle once the stage
        has produced its numbers; both go through :func:`clean`.
        """
        span = self._child("gen_ai.execute_tool", f"execute_tool {tool.value}")
        handle = SpanHandle(span)
        handle.set_many({
            OPERATION_NAME: "execute_tool",
            TOOL_NAME: tool,
            **attrs,
        })
        try:
            yield handle
        finally:
            _finish(span)

    @contextmanager
    def chat(self, model: str, **attrs: Any) -> Iterator[SpanHandle]:
        """A ``gen_ai.chat`` span around the one model call of a step.

        The model tag is the only free-form string that reaches Sentry, and it
        has to match :data:`MODEL_TAG` to get there.
        """
        span = self._child("gen_ai.chat", f"chat {model}")
        handle = SpanHandle(span)
        handle.set_many({
            OPERATION_NAME: "chat",
            PROVIDER_KEY: PROVIDER,
            REQUEST_MODEL: model,
            **attrs,
        })
        try:
            yield handle
        finally:
            _finish(span)

    def record_wait(self, check_type: Any, outcome: Any, wait_s: float,
                    step_index: int | None = None) -> None:
        """A ``wait_check`` span for the time she spent on one step.

        Recorded after the fact rather than held open: the wait is many polls
        across many timer ticks, and holding a span open across them would mean
        storing it on the step and hoping every exit path closes it. The start
        is backdated so the trace waterfall still shows the real shape of the
        step -- which is the whole point, since this span is usually the widest
        one in the trace.
        """
        if self._root is None:
            return
        wait_s = max(0.0, float(wait_s))
        span = self._child(
            "gen_ai.execute_tool",
            f"execute_tool {Tool.WAIT_CHECK.value}",
            start_timestamp=_ago(wait_s),
        )
        if span is None:
            return
        SpanHandle(span).set_many({
            OPERATION_NAME: "execute_tool",
            TOOL_NAME: Tool.WAIT_CHECK,
            "tinyme.check_type": check_type,
            "tinyme.outcome": outcome,
            "tinyme.wait_ms": wait_s * 1000.0,
            "tinyme.step_index": step_index,
        })
        _finish(span)

    def finish(self, outcome: Any = None, **attrs: Any) -> None:
        """Close the trace. Safe to call twice; the second call does nothing."""
        root = self._root
        if root is None:
            return
        self._root = None
        SpanHandle(root).set_many({"tinyme.outcome": outcome, **attrs})
        _finish(root)


#: The trace a disabled build uses. Shared, immutable in practice: every method
#: on it returns immediately.
NO_TRACE = TaskTrace()


def _ago(seconds: float):
    """A timezone-aware wall-clock timestamp *seconds* in the past."""
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _finish(span: Any) -> None:
    if span is None:
        return
    try:
        span.finish()
    except Exception:
        log.debug("could not finish a span", exc_info=True)


def start_task() -> TaskTrace:
    """Begin one task's trace, or return :data:`NO_TRACE` when tracing is off.

    The root is a Sentry transaction rather than a plain span: it is the thing
    the AI Agents dashboard groups by, and it is what carries the trace id that
    the worker thread's children inherit.
    """
    if not _started:
        return NO_TRACE
    try:
        import sentry_sdk

        root = sentry_sdk.start_transaction(
            op="gen_ai.invoke_agent",
            name=f"invoke_agent {AGENT_NAME}",
        )
    except Exception:
        log.debug("could not start a task trace", exc_info=True)
        return NO_TRACE

    trace = TaskTrace(root)
    SpanHandle(root).set_many({
        OPERATION_NAME: "invoke_agent",
        AGENT_NAME_KEY: AGENT_NAME,
    })
    return trace


@contextmanager
def task() -> Iterator[TaskTrace]:
    """A whole task's trace as a context manager, for the eval runner.

    ``main.py`` cannot use this -- a task there spans many timer ticks and ends
    in one of several callbacks -- so it holds a :class:`TaskTrace` and calls
    ``finish`` itself.
    """
    trace = start_task()
    try:
        yield trace
    finally:
        trace.finish()
