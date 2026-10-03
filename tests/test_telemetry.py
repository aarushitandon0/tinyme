"""Tests for app.telemetry: the module that could break the privacy promise.

CLAUDE.md rule 7 is a claim made in the write-up -- "spans carry numbers and
enums only" -- so it is tested the way the guard is tested: by running the real
pipeline with telemetry really on, capturing what Sentry would have sent, and
searching that payload for the words that must not be in it.

Two kinds of mistake matter here, and as with the guard they are not symmetric:

* A **leak** puts her goal, an instruction, an OCR line, a window title or a
  filename on the wire. That is the one failure this whole design exists to
  prevent, so the tests below assert against the *whole* captured payload, not
  just the attributes we meant to set.
* A **dropped attribute** loses a number from the dashboard. Annoying, and it
  logs a warning, and it is the side :func:`app.telemetry.clean` leans to.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from app import config, telemetry
from app.brain import BrainResult, CheckType, StepPlan, SuccessCheck
from app.elements import Element, Monitor
from app.guard import Verdict
from app.main import plan_next_step
from app.telemetry import MAX_ATTR_CHARS, Outcome, Tool, clean, vocabulary
from app.watcher import Outcome as PollOutcome
from app.watcher import WindowInfo

#: Strings that exist in the pipeline below and must never reach Sentry. Every
#: one is something the model, OCR or Windows told us about her screen.
GOAL = "find the electricity bill I downloaded yesterday"
INSTRUCTION = "Click Downloads in the left panel"
OCR_TEXT = "Electricity-bill-September.pdf"
WINDOW_TITLE = "Downloads - File Explorer"
TEACH_NOTE = "Her printer is the HP in the study"

FORBIDDEN = (GOAL, INSTRUCTION, OCR_TEXT, WINDOW_TITLE, TEACH_NOTE)

MON = Monitor(left=0, top=0, width=1920, height=1080)


# --- a fake Sentry ---------------------------------------------------------


class Captured:
    """Everything the SDK tried to send, as parsed JSON."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    @property
    def text(self) -> str:
        """The whole payload as one string, for the leak assertions."""
        return json.dumps(self.payloads, default=str)

    def attributes(self) -> dict[str, object]:
        """Every span attribute Sentry was given, flattened.

        Sentry encodes attributes differently in the two envelope shapes it
        uses (a transaction event and a span item), so this walks the payload
        for anything that looks like an attribute map rather than assuming one.
        """
        found: dict[str, object] = {}

        def walk(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in ("data", "attributes") and isinstance(value, dict):
                        for name, raw in value.items():
                            found[name] = (raw.get("value")
                                           if isinstance(raw, dict) and "value" in raw
                                           else raw)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.payloads)
        return found

    def ours(self) -> dict[str, object]:
        """Only the attributes this app set, i.e. the ones under our contract."""
        return {key: value for key, value in self.attributes().items()
                if key in telemetry.ATTR_KEYS}


@pytest.fixture
def sentry(monkeypatch):
    """Telemetry really on, pointed at a transport that keeps the payload.

    The fixture resets ``telemetry._started`` and detaches the client
    afterwards, because both are process-global and would otherwise leak into
    the rest of the suite.
    """
    import sentry_sdk

    captured = Captured()

    class Keep(sentry_sdk.Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                payload = item.payload.json
                if isinstance(payload, dict):
                    captured.payloads.append(payload)

        def capture_event(self, event):  # pragma: no cover - 2.x uses envelopes
            captured.payloads.append(event)

    real_init = sentry_sdk.init

    def init_with_transport(**kwargs):
        kwargs["transport"] = Keep()
        return real_init(**kwargs)

    monkeypatch.setattr(sentry_sdk, "init", init_with_transport)
    monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
    monkeypatch.setenv("SENTRY_DSN", "https://key@o0.ingest.sentry.io/1")
    monkeypatch.setattr(telemetry, "_started", False)

    assert telemetry.init() is True
    try:
        yield captured
    finally:
        telemetry._started = False
        sentry_sdk.get_global_scope().set_client(None)


# --- the pipeline, with real screen text in it -----------------------------


def screen_elements() -> list[Element]:
    """A plausible Explorer screen, including a filename of hers."""
    return [
        Element(id=0, text="Downloads", source="uia", role="ListItem",
                region="left-middle", bbox_px=(60, 120, 180, 144)),
        Element(id=0, text=OCR_TEXT, source="ocr", role="text",
                region="center", bbox_px=(400, 300, 900, 324)),
    ]


def a_plan() -> StepPlan:
    return StepPlan(
        instruction=INSTRUCTION,
        target_id=1,
        success_check=SuccessCheck(type=CheckType.WINDOW_TITLE_CONTAINS,
                                   value="Downloads"),
    )


def run_a_step(trace, *, sensitive: bool = False, **result_kwargs):
    """One real ``plan_next_step`` with every stage faked but the tracing."""
    from app.capture import Capture

    def grab():
        return Capture(image=np.zeros((20, 20, 3), dtype=np.uint8),
                       monitor=MON, elapsed_ms=42.0)

    def read(image, monitor, span=None):
        if span is not None:
            span.set_many({"tinyme.ocr_ms": 310.0, "tinyme.uia_ms": 90.0})
        return screen_elements(), 400.0

    def planner(goal, elements, history=(), teach_notes=()):
        defaults = dict(elapsed_ms=2500.0, prompt_tokens=812, output_tokens=44)
        defaults.update(result_kwargs)
        return BrainResult(plan=a_plan(), **defaults)

    return plan_next_step(
        GOAL,
        history=(INSTRUCTION,),
        teach_notes=(TEACH_NOTE,),
        grab=grab,
        read=read,
        planner=planner,
        window_reader=lambda: WindowInfo(class_name="CabinetWClass",
                                         title=WINDOW_TITLE, hwnd=7),
        sensitive=lambda elements, title: Verdict(
            sensitive=sensitive, reason="password" if sensitive else ""),
        trace=trace,
    )


# --- the promise -----------------------------------------------------------


class TestNothingOfHersLeaves:
    """The assertions the write-up's privacy paragraph stands on."""

    def test_a_whole_step_sends_no_screen_text(self, sentry):
        trace = telemetry.start_task()
        run_a_step(trace)
        trace.record_wait(CheckType.WINDOW_TITLE_CONTAINS, PollOutcome.PASSED,
                          wait_s=6.0, step_index=1)
        trace.finish(outcome="goal_reached", **{"tinyme.steps": 3,
                                                "tinyme.recoveries": 1,
                                                "tinyme.pauses": 0})
        telemetry.flush()

        assert sentry.payloads, "nothing was captured, so nothing was proved"
        for secret in FORBIDDEN:
            assert secret not in sentry.text

    def test_no_attribute_of_ours_is_longer_than_a_word(self, sentry):
        """MASTERSPEC 8: attributes are primitives, and ours are short.

        Scoped to our own keys on purpose. Sentry sets strings of its own --
        the hostname, the SDK version -- which are not ours to cap and carry
        nothing of hers.
        """
        trace = telemetry.start_task()
        run_a_step(trace)
        trace.finish(outcome="goal_reached")
        telemetry.flush()

        ours = sentry.ours()
        assert ours, "no attributes of ours were sent"
        for key, value in ours.items():
            if isinstance(value, str):
                assert len(value) <= MAX_ATTR_CHARS, f"{key} is a sentence, not an enum"

    def test_the_goal_is_not_hiding_in_a_span_name(self, sentry):
        trace = telemetry.start_task()
        run_a_step(trace)
        trace.finish(outcome="goal_reached")
        telemetry.flush()

        names = [payload.get("transaction") or payload.get("name")
                 for payload in sentry.payloads]
        for name in names:
            if isinstance(name, str):
                for secret in FORBIDDEN:
                    assert secret not in name

    def test_the_guard_sends_the_outcome_and_not_the_reason(self, sentry):
        """A paused screen is the most sensitive moment there is."""
        trace = telemetry.start_task()
        outcome = run_a_step(trace, sensitive=True)
        trace.finish(outcome="stopped")
        telemetry.flush()

        assert outcome.paused_reason == "password"
        assert sentry.ours()["tinyme.outcome"] == Outcome.PAUSED.value
        # The reason is a keyword list built from her screen, so even though
        # "password" is short and harmless-looking, it must not be the thing we
        # send: the next keyword added to guard.py might not be.
        assert "paused_reason" not in sentry.text
        # CLAUDE.md rule 3, visible in the trace: a held-back screen produces
        # no chat span at all, because no model call happened.
        assert "gen_ai.request.model" not in sentry.ours()
        assert "chat " not in sentry.text


class TestTheNumbersActuallyArrive:
    """A privacy-safe span that carries nothing is no use either."""

    def test_every_stage_of_a_step_is_reported(self, sentry):
        trace = telemetry.start_task()
        run_a_step(trace)
        trace.record_wait(CheckType.WINDOW_TITLE_CONTAINS, PollOutcome.PASSED,
                          wait_s=6.0, step_index=2)
        trace.finish(outcome="goal_reached", **{"tinyme.steps": 3,
                                                "tinyme.recoveries": 1,
                                                "tinyme.pauses": 0})
        telemetry.flush()

        ours = sentry.ours()
        assert ours["tinyme.capture_ms"] == 42.0
        assert ours["tinyme.read_ms"] == 400.0
        assert ours["tinyme.ocr_ms"] == 310.0
        assert ours["tinyme.uia_ms"] == 90.0
        assert ours["tinyme.model_ms"] == 2500.0
        assert ours["tinyme.element_count"] == 2
        assert ours["tinyme.wait_ms"] == 6000.0
        assert ours["tinyme.steps"] == 3
        assert ours["tinyme.recoveries"] == 1

    def test_the_chat_span_names_the_model_and_the_tokens(self, sentry):
        trace = telemetry.start_task()
        run_a_step(trace)
        trace.finish(outcome="goal_reached")
        telemetry.flush()

        ours = sentry.ours()
        assert ours["gen_ai.request.model"] == config.MODEL
        assert ours["gen_ai.provider.name"] == telemetry.PROVIDER
        assert ours["gen_ai.usage.input_tokens"] == 812
        assert ours["gen_ai.usage.output_tokens"] == 44
        assert ours["tinyme.valid_json"] is True
        assert ours["tinyme.retried"] is False

    def test_a_fallback_is_reported_as_invalid_json(self, sentry):
        trace = telemetry.start_task()
        run_a_step(trace, fell_back=True, retried=True)
        trace.finish(outcome="max_steps")
        telemetry.flush()

        ours = sentry.ours()
        assert ours["tinyme.valid_json"] is False
        assert ours["tinyme.fell_back"] is True
        assert ours["tinyme.retried"] is True

    def test_every_span_carries_the_operation_name(self, sentry):
        """Sentry requires it on every gen_ai span (MASTERSPEC 1, verified facts)."""
        trace = telemetry.start_task()
        run_a_step(trace)
        trace.record_wait(CheckType.REGION_CHANGED, PollOutcome.REPLAN, 3.0)
        trace.finish(outcome="goal_reached")
        telemetry.flush()

        operations = set()

        def walk(node):
            if isinstance(node, dict):
                attrs = node.get("data") or node.get("attributes")
                if isinstance(attrs, dict) and telemetry.OPERATION_NAME in attrs:
                    raw = attrs[telemetry.OPERATION_NAME]
                    operations.add(raw.get("value") if isinstance(raw, dict) else raw)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(sentry.payloads)
        assert operations == {"invoke_agent", "execute_tool", "chat"}


# --- the filter, on its own ------------------------------------------------


class TestClean:
    """:func:`app.telemetry.clean` is the only way a value reaches a span."""

    def test_an_undeclared_key_is_dropped(self):
        assert clean("tinyme.goal", "find my bill") is None
        assert clean("gen_ai.request.messages", "anything") is None

    def test_a_sentence_under_a_numeric_key_is_dropped(self):
        assert clean("tinyme.element_count", "eighty") is None

    def test_a_word_outside_the_vocabulary_is_dropped(self):
        assert clean("tinyme.outcome", "Downloads - File Explorer") is None
        assert clean("tinyme.check_type", "looked right to me") is None

    def test_enums_arrive_as_their_value(self):
        assert clean("tinyme.outcome", PollOutcome.PASSED) == "passed"
        assert clean("tinyme.check_type", CheckType.WINDOW_CLASS_IS) == "window_class_is"
        assert clean("gen_ai.tool.name", Tool.READ_SCREEN) == "read_screen"

    def test_bools_stay_bools(self):
        assert clean("tinyme.retried", True) is True
        assert clean("tinyme.valid_json", False) is False

    def test_milliseconds_are_rounded_not_dropped(self):
        assert clean("tinyme.model_ms", 2513.874) == 2513.9

    def test_a_model_tag_is_allowed_but_a_goal_is_not(self):
        assert clean("gen_ai.request.model", "gemma4:e2b") == "gemma4:e2b"
        assert clean("gen_ai.request.model", "tinyme-picker") == "tinyme-picker"
        # Whitespace is what rules out everything she typed or we read.
        assert clean("gen_ai.request.model", "find my bill") is None
        assert clean("gen_ai.request.model", "x" * 60) is None

    def test_a_long_vocabulary_word_would_still_be_capped(self):
        assert all(len(word) <= MAX_ATTR_CHARS for word in vocabulary())

    def test_the_vocabulary_covers_the_enums_that_feed_it(self):
        """Guards against a new check type or outcome becoming unsendable."""
        words = vocabulary()
        for member in CheckType:
            assert member.value in words
        for member in PollOutcome:
            assert member.value in words
        for member in Tool:
            assert member.value in words

    def test_the_task_outcomes_match_what_main_actually_reports(self):
        """``TaskLoop._finish`` is the only writer of ``finished_because``."""
        import inspect

        from app.main import TaskLoop

        source = inspect.getsource(TaskLoop)
        for outcome in telemetry.TASK_OUTCOMES:
            assert f'"{outcome}"' in source


class TestOffByDefault:
    """On her laptop none of this does anything (CLAUDE.md rule 7)."""

    def test_disabled_without_the_flag(self, monkeypatch):
        monkeypatch.setattr(config, "TELEMETRY_ENABLED", False)
        monkeypatch.setenv("SENTRY_DSN", "https://key@o0.ingest.sentry.io/1")
        assert telemetry.is_enabled() is False

    def test_disabled_without_a_dsn(self, monkeypatch):
        monkeypatch.setattr(config, "TELEMETRY_ENABLED", True)
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        assert telemetry.is_enabled() is False

    def test_init_is_a_no_op_and_start_task_gives_a_dead_trace(self, monkeypatch):
        monkeypatch.setattr(config, "TELEMETRY_ENABLED", False)
        monkeypatch.setattr(telemetry, "_started", False)
        assert telemetry.init() is False
        trace = telemetry.start_task()
        assert trace.live is False

    def test_a_dead_trace_still_runs_the_pipeline(self, monkeypatch):
        """The spans are structure, not a dependency."""
        monkeypatch.setattr(telemetry, "_started", False)
        outcome = run_a_step(telemetry.NO_TRACE)
        assert outcome.target is not None
        assert outcome.plan.instruction == INSTRUCTION

    def test_the_env_file_never_overrides_a_real_variable(self, tmp_path, monkeypatch):
        """``TINYME_TELEMETRY=1 python -m app.main`` must beat a stale ``.env``."""
        env = tmp_path / ".env"
        env.write_text("SENTRY_DSN=https://from-file@o0.ingest.sentry.io/1\n"
                       "# a comment\n"
                       "TINYME_TELEMETRY=0\n", encoding="utf-8")
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        monkeypatch.setenv("TINYME_TELEMETRY", "1")

        config._load_env_file(env)

        assert os.environ["SENTRY_DSN"].endswith("ingest.sentry.io/1")
        assert os.environ["TINYME_TELEMETRY"] == "1"

    def test_a_missing_env_file_is_the_normal_case(self, tmp_path):
        config._load_env_file(tmp_path / "nothing-here")

    def test_a_dead_span_swallows_everything(self):
        with telemetry.NO_TRACE.tool(Tool.CAPTURE) as span:
            assert span.live is False
            span.set("tinyme.capture_ms", 1.0)
            span.set("tinyme.goal", "not even this")
        telemetry.NO_TRACE.record_wait(CheckType.TEXT_APPEARS, Outcome.TIMEOUT, 9.0)
        telemetry.NO_TRACE.finish(outcome="stopped")
