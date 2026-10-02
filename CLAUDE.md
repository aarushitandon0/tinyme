# CLAUDE.md — Tiny Me

Read `MASTERSPEC.md` before any non-trivial change. `PROJECTPLAN.md` says what is in scope right now. If a request conflicts with the spec, say so before coding.

## What this is
A Windows desktop helper for one non-technical person. Hotkey → she types a goal → it circles what to click, waits for her, and continues; or does safe steps for her. Stops and hands over at passwords, OTP, payments, delete, send. Runs fully local: RapidOCR + Windows UI Automation + Gemma 4 via Ollama.

Hackathon deadline: Mon 5 Oct 2026, 12:29 PM IST. Prefer the simplest thing that works and can be measured.

## Stack
- Python 3.11, Windows 10/11, primary monitor only
- PySide6 (Qt 6) for overlay and prompt window
- mss (capture), rapidocr + onnxruntime (OCR), uiautomation (UIA), pywin32 (window class/title)
- ollama Python client, pydantic v2 for the output schema
- pynput (global hotkeys), rapidfuzz (fuzzy text), numpy, Pillow
- playwright (headed Chromium) for the booking flow on `demo_site/`
- sentry-sdk (dev/eval only), pytest

Check installed package versions before using an API (`pip show <pkg>`); RapidOCR's API differs between `rapidocr_onnxruntime` and the newer `rapidocr` package. Pin versions in `pyproject.toml` once something works.

## Commands
```
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
ollama pull gemma4:e2b
python -m app.main                 # run the app
pytest -q                          # unit tests
python eval\run_eval.py --systems all
python scripts\bench_gemma.py
```

## Architecture rules (do not break)
1. **The model never outputs coordinates.** It returns `target_id` from the numbered element list. Bboxes stay in Python and are never put in the prompt.
2. **Success is checked deterministically** in `watcher.py` (window class/title, OCR text, region diff). The model proposes which check; code runs it.
3. **`guard.py` runs before every model call and before every automated action.** If it says sensitive, nothing is sent to the model and nothing is clicked or typed.
4. **"Do it for me" permissions live in code** (`guard.py`), not in the prompt. Unknown task type → guide mode.
5. **Never type into or click password, OTP, CVV, card, UPI PIN, or payment fields.** In Playwright, detect via DOM (`input[type=password]`, `autocomplete` `cc-*` / `one-time-code`, labels) and hand over.
6. **Screenshots stay in memory.** No writing images to disk except in `eval/` tooling run on the developer's own machine.
7. **Telemetry is off unless `TINYME_TELEMETRY=1`.** Spans carry numbers and enums only: latencies, token counts, element counts, check types, outcomes. Never OCR text, goals, instructions, file names, window titles, or images. `send_default_pii=False`.
8. **Qt widgets only on the main thread.** pynput callbacks and worker threads communicate through Qt signals.
9. **Coordinates:** mss gives physical pixels; Qt uses logical pixels. Convert with `screen.devicePixelRatio()` in exactly one function (`elements.to_logical`). Don't scatter conversions.
10. **Overlay is click-through and excluded from capture** (`SetWindowDisplayAffinity(hwnd, 0x11)`, fallback hide→capture→show). The prompt window is a separate, normal window.
11. File Explorer windows are detected by class `CabinetWClass`, not by title.

## Code style
- Small modules matching the repo layout in the spec. Type hints everywhere. Dataclasses or pydantic models for data passed between modules.
- Every external call (Ollama, OCR, UIA, Playwright) wrapped with a timeout and a clear fallback.
- Log with `logging`, level INFO by default, and never log screen text at INFO (DEBUG only, dev machine).
- No new dependencies without saying why. No cloud services in the runtime path.

## Testing
- `tests/` covers: guard keyword and DOM rules, element merge/dedupe and numbering, schema validation + retry path in brain (Ollama mocked), each watcher check type with fixture images, physical→logical conversion.
- Before saying a task is done: run `pytest -q`, then describe how you verified behaviour on screen (or say clearly that it needs a manual check by the developer).

## Honesty rules (this project is judged on its write-up)
- Never invent benchmark numbers, timings, or user quotes. Numbers come from `eval/results.md`, `notes/bench.md`, Sentry screenshots; quotes from `notes/user_test.md`.
- If something only works on the mock site or only at 100% scaling, say so in README "Limitations".

## Privacy of the repo itself
- Agent sessions are captured by Entire and pushed with the repo. Do not ask for, read, or paste the real user's screenshots, files, or personal details. Eval data comes from the developer's laptop with dummy files.
- `.env` (Sentry DSN etc.) is gitignored. Never print secrets.

## Project skills
- `tinyme-guardrails`: run before committing changes to guard, actions, capture, overlay, telemetry.
- `tinyme-eval`: labeling format, running the eval, writing `eval/results.md`.
- `tinker-finetune`: Tinker dataset + training + comparison.
- `dev-post-writer`: drafting the DEV submission post.
