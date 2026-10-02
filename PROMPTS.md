# Tiny Me — Claude Code Prompts

Paste these one at a time into Claude Code from the repo root. Each one assumes `CLAUDE.md` and `MASTERSPEC.md` are present. Commit after each prompt passes its acceptance check (Entire records the session on commit).

Tip: for P2–P9, ask Claude to show its plan first and approve it before it edits.

---

## P0 — Orientation (first message of every new session)
```
Read CLAUDE.md, MASTERSPEC.md and PROJECTPLAN.md. In under 15 lines tell me:
what is built so far (look at the repo), what the next unchecked item in
PROJECTPLAN.md is, and any conflict you see between the spec and the code.
Don't edit anything yet.
```

## P1 — Gemma speed benchmark
```
Create scripts/bench_gemma.py. It should:
- build a fake element list of 60 lines in the exact model-facing format from
  MASTERSPEC §5.2, plus a goal and 2 history steps
- call Ollama with structured output (format = the pydantic JSON schema from
  §5.3), temperature 0, keep_alive so the model stays loaded
- run 1 warm-up + 10 timed calls for each model passed via --models
  (default gemma4:e2b,gemma4:e4b)
- print median and p90 seconds, prompt_eval_count, eval_count, and how many
  outputs were valid against the schema
- append results with machine info (CPU, RAM, GPU if any) to notes/bench.md
Check the installed ollama client version first and use its real API. If the
model emits thinking text, find the supported way to turn it off on this version.
```
Acceptance: `notes/bench.md` has real numbers from her laptop or the closest machine.

## P2 — Scaffold, capture, OCR, elements
```
Scaffold the repo layout from MASTERSPEC §11 with pyproject.toml (Python 3.11,
deps from CLAUDE.md, dev extras: pytest). Then implement:
- app/capture.py: primary-monitor mss grab → numpy RGB array + monitor geometry
  in physical pixels. Memory only.
- app/ocr.py: RapidOCR → list[Element] (check which RapidOCR package/API is
  installed first). Filter score < 0.5 and height < 8 px.
- app/elements.py: Element dataclass (id, text, source, role, region, bbox_px),
  9-region classifier, numbering, model-facing text rendering, cap at 80
  elements prioritising taskbar + foreground window, and to_logical(bbox, dpr).
- tests for region classification, numbering, cap, to_logical.
Add scripts/dump_elements.py that captures once and prints the numbered list.
```
Acceptance: `python scripts\dump_elements.py` prints sensible numbered text; tests pass.

## P3 — Overlay + DPI calibration
```
Implement app/overlay.py with PySide6: a full-screen frameless, translucent,
always-on-top, Tool, WindowTransparentForInput overlay that draws a circle
(bbox + 20 px padding, thick bright stroke) and a short instruction label near
it, kept on-screen. Exclude it from capture with
SetWindowDisplayAffinity(hwnd, 0x11); verify by capturing with mss while it is
shown and OCR-ing the result — if the instruction text is found, fall back to
hide → capture → show and log which path is active.
Add scripts/calibrate.py: capture, OCR, circle the 3 largest words for 5 s.
Use elements.to_logical with the screen's devicePixelRatio. Tell me exactly
how to test at 100% and 125% Windows scaling.
```
Acceptance: circles land on the words at both scalings; the overlay never shows up in OCR.

## P4 — Brain + first end-to-end step
```
Implement app/brain.py:
- pydantic StepPlan exactly as MASTERSPEC §5.3 (success_check.type enum,
  instruction max 90 chars)
- system prompt: she is a non-technical user; plain short instructions; pick
  ONLY an id from the list; if the right element isn't listed set
  cannot_see_it and give hint_if_missing; never ask for passwords; write
  instruction in config.LANGUAGE
- user message: goal, history (instructions already completed), element list,
  teach-note lines if any
- validation: target_id must exist unless goal_reached/cannot_see_it; one
  retry with the validation error appended; then fall back to cannot_see_it
- returns StepPlan + timing + token counts
Then app/main.py minimal: Ctrl+Alt+H opens a small prompt window (text box +
"Show me how"), runs capture → elements → brain in a QThread worker, and the
overlay circles the chosen element. pynput hotkey must signal into Qt, never
touch widgets directly.
Tests: brain validation and retry with Ollama mocked.
```
Acceptance: "open my downloads" → Gemma circles the right element once.

## P5 — UI Automation elements
```
Implement app/uia.py using the uiautomation package: collect named, visible,
enabled controls from the taskbar (Shell_TrayWnd) and the foreground window
only, depth-limited, with a hard time budget of 300 ms (return what you have
when it expires). Map to Element(source="uia", role=ControlTypeName).
In elements.py merge OCR + UIA: if IoU > 0.5 and fuzzy text ratio ≥ 85 keep
one (prefer UIA for role, OCR for text if UIA name empty).
Log uia_ms and ocr_ms. Add a test for merge/dedupe with synthetic boxes.
Fallback: if UIA throws or times out, continue with OCR only.
```
Acceptance: `dump_elements.py` shows "File Explorer" as a taskbar element.

## P6 — Watcher + task loop
```
Implement app/watcher.py and the multi-step task loop per MASTERSPEC §5.4–5.5:
- checks: window_class_is, window_title_contains, text_appears,
  text_disappears (rapidfuzz ≥ 85), region_changed (mean abs diff in padded
  bbox above a threshold you calibrate and put in config)
- cheap checks first; OCR only for text checks
- ~1 Hz polling via QTimer, never blocking the UI thread
- "I did it" button forces success
- 20 s without success → show hint/rephrase once
- unexpected change (foreground window changed to something unrelated, or
  target region changed but check failed) → re-plan from a fresh capture;
  record a recovery event
- loop ends on goal_reached, pause hotkey, or 12 steps max
Add a step history the brain receives. Tests: each check type with fixture
images and mocked window info.
```
Acceptance: Scene A completes 3 steps; a deliberate wrong click recovers.

## P7 — Guard + permissions + pause
```
Implement app/guard.py:
- is_sensitive(elements, window_title) with the keyword list from MASTERSPEC §6,
  case-insensitive, fuzzy, returning the reason
- can_do_it(task_type) using the permission table in §4; unknown → False
- called before every brain call and every automated action; when sensitive:
  no model call, overlay shows "This part is yours. I'll wait." and the loop
  waits until the screen is no longer sensitive
Add Ctrl+Alt+P pause/stop everything and a small always-visible
"Tiny Me is looking" indicator whenever capture is active.
Tests: positives, negatives (e.g. "Bank holiday list.docx" in Explorer should
NOT trigger by itself — decide and document the rule), permissions table.
Then run the tinyme-guardrails skill.
```
Acceptance: a page with a password field pauses before any model call; tests green.

## P8 — Find it for me + teach notes
```
Add "Find it for me" to the prompt window. In app/actions.py:
- resolve Downloads via the Windows Known Folder API (FOLDERID_Downloads),
  not Path.home()/"Downloads"
- match by fuzzy name from her goal, else newest file
- open with subprocess: explorer /select,"<path>" (quote correctly)
- guard: read-only, never delete/move
Teach notes: notes/her_setup.txt (plain lines). brain.py adds the top 3 lines
that fuzzy-match the goal. Ship an example file with dummy content only.
```
Acceptance: newest download highlighted in Explorer in < 3 s.

## P9 — Mock booking site + handover
```
Build demo_site/ (static HTML/CSS/JS, served by python -m http.server on
localhost): a 3-page ticket flow — search (from, to, date) → results → login
(username + password) → OTP → payment (card fields). Clearly labelled
"Demo site" in the footer.
In app/actions.py add a Playwright headed Chromium flow for "Do it for me":
open the site, fill from/to/date parsed from her goal, pick the first train,
and before EVERY action run a DOM guard: visible input[type=password],
autocomplete cc-* or one-time-code, or labels matching OTP/CVV/card/UPI PIN
→ stop, get that element's screen rect, circle it via the overlay with
"You type your own password, I'll wait.", and wait until the page moves on.
Never call fill/type/click on those elements. Tests for the DOM guard using
the demo pages.
```
Acceptance: fills route/date, stops at login with the circle, resumes after she logs in, stops again at OTP and payment.

## P10 — Eval harness
```
Use the tinyme-eval skill. Build:
- eval/capture_tool.py: hotkey-driven tool to save a screenshot + its frozen
  element list JSON (developer's laptop only)
- eval/labels.jsonl format from the skill
- eval/run_eval.py with systems: pipeline_e2b, pipeline_e4b,
  vision_coords_e4b (Gemma 4 gets the image, returns x,y; correct if inside a
  correct bbox), and an optional external baseline behind a flag
- metrics: correct-element rate, JSON validity, latency split
- write eval/results.md (markdown table + 3 example failures with reasons)
Run it and show me the table.
```

## P11 — Fix what she got stuck on
```
Here are my notes from the user session (from notes/user_test.md): <paste
summary, no personal data>. For each stuck point propose the smallest fix,
implement the top fixes that fit in 2 hours, and add the rest to README
"Limitations" honestly. Re-run pytest and the eval afterwards and tell me if
any metric moved.
```

## P12 — Sentry agent tracing (dev only)
```
Implement app/telemetry.py per MASTERSPEC §8 using sentry-sdk manual agent
instrumentation. Before writing code, check the installed sentry-sdk version
and the current docs for manual AI agent spans in Python, and tell me the
exact API you'll use.
Rules: init only if TINYME_TELEMETRY=1 and SENTRY_DSN set; send_default_pii
False; traces_sample_rate 1.0; every span sets gen_ai.operation.name; chat
span sets gen_ai.request.model, gen_ai.provider.name="ollama", input/output
token counts from Ollama; attributes are primitives only; NEVER attach goal,
instruction, OCR text, titles, filenames or images. When disabled, every
helper is a no-op context manager. Add a test that asserts no string
attribute exceeds 40 chars and none matches the current goal text.
Then run the tinyme-guardrails skill.
```

## P13 — Tinker fine-tune (stretch)
```
Use the tinker-finetune skill. First confirm from the current Tinker docs which
small instruction model is available (expected Qwen/Qwen3-4B-Instruct-2507),
current pricing, and whether LoRA weights can be downloaded. Then build
finetune/build_dataset.py from eval/labels.jsonl with the app-held-out split,
train, and evaluate tuned vs untuned vs Gemma on the held-out app. Add the
rows to eval/results.md. Stop and ask me before any run estimated above the
credits I have.
```

## P14 — README + post
```
Write README.md: what it is, who it's for, setup, privacy design (MASTERSPEC §6),
limitations, how to run eval, "Post-deadline commits" section (empty), license
(MIT). Then use the dev-post-writer skill to draft the DEV post into
docs/post_draft.md using only numbers and quotes that exist in the repo.
List any placeholders I must fill by hand.
```
