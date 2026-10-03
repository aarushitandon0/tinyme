# Sentry agent tracing — how to turn it on and what to capture

This is the operator's page for MASTERSPEC §8. The design and the privacy
argument live in `app/telemetry.py`'s docstring; this is the half-hour of
clicking that turns it into two screenshots for the post.

**It is off on her laptop and there is no way to turn it on by accident.** It
needs both `TINYME_TELEMETRY=1` and a `SENTRY_DSN`, and her laptop has neither.

## Turn it on (dev machine only)

Put the DSN in `.env` in the repo root — gitignored, never committed:

```
SENTRY_DSN=https://<key>@<org>.ingest.sentry.io/<project>
TINYME_TELEMETRY=1
```

`app/config.py` reads that file at import; a real environment variable wins
over it, so `TINYME_TELEMETRY=0 python -m app.main` still gives you a quiet run.

Then:

```
python -m app.main
```

The log line to look for, before the first hotkey press:

```
INFO app.telemetry: telemetry on: agent spans only, numbers and enums only
```

If it says nothing, telemetry is off: the flag or the DSN is missing. A run
with telemetry silently off is the one way to waste an afternoon here, which is
why that line is at INFO.

## What one task looks like in Sentry

One trace per task, as MASTERSPEC §8 specifies:

```
invoke_agent Tiny Me          gen_ai.agent.name, tinyme.steps/recoveries/pauses, tinyme.outcome
├─ execute_tool capture       tinyme.capture_ms
├─ execute_tool read_screen   tinyme.read_ms, tinyme.ocr_ms, tinyme.uia_ms, tinyme.element_count
├─ execute_tool guard         tinyme.outcome = clear | paused
├─ chat <model>               gen_ai.request.model, gen_ai.provider.name, input/output tokens,
│                             tinyme.model_ms, tinyme.valid_json, tinyme.retried
└─ execute_tool wait_check    tinyme.check_type, tinyme.outcome, tinyme.wait_ms, tinyme.step_index
```

A multi-step task repeats the five children once per step. The `wait_check`
span is backdated to when the step was shown, so the waterfall shows the real
shape of the step rather than five spans bunched at the start.

Two shapes worth knowing by sight:

- **A paused step has no `chat` span at all.** That is CLAUDE.md rule 3 visible
  in the trace: the guard held the screen back and nothing was sent to the
  model. `tests/test_telemetry.py` asserts it.
- **A recovery is two `wait_check` spans at the same `step_index`**, the first
  with `tinyme.outcome = replanned`.

## What is deliberately *not* there

- No goal, no instruction, no OCR text, no window title, no filename, no
  screenshot. `app/telemetry.clean` drops anything that is not a declared
  number or a word from a closed vocabulary, so these cannot arrive by being
  passed to the wrong argument.
- No breadcrumbs (they would carry our own INFO log lines), no error events, no
  local variables — `goal` is a local variable in half the call stack. Spans are
  the entire payload. Stack traces stay in the console.
- `send_default_pii=False`.

The guard span's reason is the clearest example of the rule: it sends
`paused` and not `password`, because the reason is a keyword list built from her
screen, and the next keyword added to `guard.py` might be less harmless-looking
than that one.

## Populating the dashboard for the post

PROJECTPLAN, Sunday afternoon. Needs a real DSN and ten minutes at the laptop:

1. Run ten tasks across at least three apps (Explorer, a browser, Settings).
   Scene A twice, Scene B once, Scene C once, and the rest ordinary.
2. Include **one deliberate wrong click** so there is a recovery trace to show,
   and **one password screen** so there is a trace with a `guard` span that
   paused and no `chat` span.
3. Screenshot the **AI Agents dashboard** → `docs/sentry-dashboard.png`.
4. Open the recovery trace, screenshot the waterfall →
   `docs/sentry-recovery-trace.png`.

`.gitignore` ignores `docs/*.png` but un-ignores `docs/sentry-*.png`, so use
exactly those names. Those two images show span names, latencies and token
counts and nothing of her screen, which is why they are the one exception.

Then write the paragraph the post needs: *what the traces told me*. Take the
numbers from the dashboard, not from memory — the split between `ocr_ms`,
`uia_ms` and `model_ms` is the whole reason this instrumentation exists, and
`notes/bench.md` suggests it will not be the model that dominates.

**Not done yet, and must not be claimed until it is:** no screenshots are in
`docs/` and no dashboard paragraph is written. The instrumentation is in place
and tested; the dashboard needs a DSN and a run on real hardware.
