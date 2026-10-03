# Tiny Me — Master Spec

DEV Hacktoberfest 2026 Weekend Challenge: **Build for a Friend**
Deadline: **Monday 5 Oct 2026, 12:29 PM IST** (06:59 UTC). Repo must be created inside the window (opened Fri 2 Oct, 7:30 AM IST).
Required tags: `#devchallenge #weekendchallenge #hf26challenge`. One post per team. Open to 18+.

---

## 0. One-paragraph pitch

Tiny Me is a small Windows app for one real person (my mom). She presses one hotkey, types what she wants ("I can't find the file I downloaded"), and Tiny Me either **shows her how** (draws a circle on the exact thing to click, waits until she clicks it, then the next circle) or **does it for her** while she watches. For anything involving passwords, OTPs, payments, deleting or sending, it stops and says *"this part is yours."* Everything runs on her laptop: local OCR, local Gemma through Ollama, no screen data uploaded. It works with Wi-Fi off.

---

## 1. Prize categories (final)

| Category | Prize | Status | What we actually do |
|---|---|---|---|
| **Gemma** | $200 | Core | Gemma 4 (via Ollama, local) picks the numbered element, writes the instruction, and chooses the success check. Also Gemma 4's vision mode is one of the baselines in the eval. |
| **Entire** | $100 | Nearly free | `entire enable --agent claude-code` in the repo from the first hour. Sessions are captured as checkpoints on the `entire/checkpoints/v1` branch. Link the sessions in the post and use them to explain *why* key code exists. |
| **Sentry Agent Tracing** | $100 | 1 to 2 hours | Manual `gen_ai.*` spans around each guide step: latency per stage, tokens, failures, recoveries. **Dev/eval mode only** (see §8). |
| **Tinker** | $200 | Stretch, Sunday | LoRA fine-tune **`Qwen/Qwen3.5-4B`** (Tinker does **not** list Gemma) to pick the correct box. Compare against untuned Qwen and Gemma 4 on a held-out app. See `finetune/DECISION.md` for where this actually stands. |

**Not entering:** ElevenLabs (voice was cut; optional 15-minute demo narration only if everything else is done), GitHub Copilot, Render, DigitalOcean, TabPFN, Arduino, Backboard, Mastra, MongoDB, SerpApi, Temporal, Tiger Data. They would be bolted on, and cloud hosting contradicts the "nothing leaves her laptop" story.

> Check the FAQ item "Can one submission win more than once?" before writing the post, and frame categories accordingly.

### Verified facts this plan relies on (re-check Friday night)
- **Gemma 4** (released 2 Apr 2026) is on Ollama as `gemma4` with edge sizes `e2b` and `e4b` (default) plus larger sizes. Edge sizes accept images. Confirm exact tags on ollama.com/library/gemma4 and update Ollama to the latest version (early releases had tool-calling bugs).
- **Tinker model lineup** (tinker-docs.thinkingmachines.ai/tinker/models/) — **re-checked 4 Oct 2026, and it had moved.** Qwen3.x, Nemotron, GLM-5.3, Kimi-K2.6, gpt-oss, DeepSeek-V3.1, Inkling. **Still no Gemma.** The id this spec first named, `Qwen/Qwen3-4B-Instruct-2507`, **no longer exists**; the smallest instruction-tuned model is now **`Qwen/Qwen3.5-4B`** (dense, 64K context) at **$0.737 / M training tokens**. LoRA weights **can** be exported (`build_lora_adapter`, `download`, `publish_to_hf_hub`). LoRA only, no full fine-tune.
- **Entire CLI** hooks into git and supported agents (including Claude Code), stores transcripts on a separate `entire/checkpoints/v1` branch, and pushes them with your code.
- **Sentry** Python agent tracing: spans with `op="gen_ai.invoke_agent"` (container), `gen_ai.chat` (model call), `gen_ai.execute_tool`; `gen_ai.operation.name` is required; span attributes must be primitive types.

---

## 2. Scope decisions (locked)

- **Windows 10/11 only.** Mac is out of scope (permissions setup alone would eat Saturday). Say so in the README.
- **Primary monitor only.**
- **Text prompt, not voice.**
- **Two buttons, no automatic router:** "Show me how" (default) and "Do it for me". Code enforces what "Do it for me" may do.
- **The model never outputs coordinates.** It picks an element ID from a numbered list we built.

---

## 3. The three demo scenes

### A. "I can't find the doc I downloaded" (guide mode)
1. Circle the **File Explorer** taskbar icon → "Open this." Success: a window of class `CabinetWClass` is in the foreground.
   - Note: the Explorer window **title is the folder name** ("Home", "Downloads"), never "File Explorer". Use the window class, not the title.
   - The taskbar icon has **no text**, so OCR cannot see it. It comes from Windows UI Automation (see §5.2). Fallback if UIA fails: no circle, instruction "Press the Windows key and E together."
2. Circle **Downloads** in the left panel → "Click Downloads." Success: foreground Explorer title contains "Downloads".
3. Circle the file (name match, else newest) → "That's your file." Goal check: filename visible in OCR.

Plus **"Find it for me"**: resolve the real Downloads folder (Known Folder API, not `~/Downloads`, since it can be relocated), find the match or newest file, run `explorer /select,"<path>"` so Explorer opens with the file highlighted.

### B. "I want to book a ticket" (do it, then hand over)
Playwright, **headed** browser, she watches. It fills route and date, then stops at login: circle on the password box, "You type your own password, I'll wait." Same at OTP and payment.
- **Demo target: a small local mock booking site we build** (`demo_site/`, served on localhost). Real Indian ticketing sites forbid automation in their terms and use CAPTCHAs. State this openly in the post; the handover logic is identical on a real site.
- In browser mode the guard is **deterministic from the DOM**: any visible `input[type=password]`, `autocomplete` of `cc-*` / `one-time-code`, or OTP/CVV labels → stop and hand over.

### C. "Make this text bigger" (guide mode, any app)
No app-specific code. Reads the current screen, guides through menus (View → Zoom, or Ctrl + plus as a hint). Shows that it generalizes.

---

## 4. Do-it permissions (enforced in `guard.py`, not by the model)

| Task type | "Do it for me" allowed? |
|---|---|
| Finding or opening files and folders | Yes |
| Navigating to a website and filling non-sensitive fields | Yes, visibly, headed browser |
| Login, password, OTP, CVV, payment, deleting, sending messages/emails | **No.** Hand over with a circle |
| Anything unrecognized | Falls back to guide mode |

---

## 5. How it works

### 5.1 Pipeline per step
1. **Capture** (`capture.py`): `mss` screenshot of the primary monitor, only after the hotkey or inside an active task. In memory only.
2. **Read** (`ocr.py` + `uia.py`): OCR text boxes + UI Automation elements, merged into one numbered list.
3. **Guard** (`guard.py`): if the screen is sensitive, stop before the model is called.
4. **Decide** (`brain.py`): Gemma gets goal, steps so far, and the element list; returns schema-validated JSON.
5. **Show** (`overlay.py`): click-through overlay draws the circle and the instruction.
6. **Wait** (`watcher.py`): deterministic success check, ~1 Hz.
7. Repeat until `goal_reached`.

### 5.2 Element list (the model's whole world)
Each element: `id`, `text`, `source` (`ocr` | `uia`), `role` (UIA control type or `text`), `region` (one of 9: top-left … bottom-right), `bbox` (physical pixels, **never sent to the model**).
- **OCR:** RapidOCR (ONNX runtime, CPU). Drop boxes with score < 0.5 or height < 8 px. Cap at ~80 elements, prioritizing the foreground window and taskbar.
- **UIA:** the `uiautomation` package. Walk only the taskbar (`Shell_TrayWnd`) and the foreground window, depth-limited, with a time budget (< 300 ms). Keep named, on-screen, enabled controls. This gives names to icon-only buttons.
- **Dedupe:** if a UIA element and an OCR box overlap (IoU > 0.5) with similar text, keep one.

Model sees lines like:
`17 | "Downloads" | uia:ListItem | left-middle`

### 5.3 Model output schema (Ollama structured output via `format=<JSON schema>`, temperature 0)
```json
{
  "instruction": "Click Downloads in the left panel",
  "target_id": 17,
  "success_check": {"type": "window_title_contains", "value": "Downloads"},
  "goal_reached": false,
  "cannot_see_it": false,
  "hint_if_missing": ""
}
```
`success_check.type` ∈ `window_class_is`, `window_title_contains`, `text_appears`, `text_disappears`, `region_changed`.

Validation in code (Pydantic), after the model returns:
- `target_id` must exist in this step's list unless `goal_reached` or `cannot_see_it` is true. Invalid → one retry with the error message appended, then fall back to `cannot_see_it`.
- `instruction` ≤ 90 characters, plain words.
- If `cannot_see_it`: no circle; show `hint_if_missing` ("Scroll down a little" / "Open the menu at the top").
- If Gemma 4 emits thinking text, disable it (Ollama `think=False`, verify on installed version).

### 5.4 Success checks (deterministic, never model-judged)
| Type | Implementation |
|---|---|
| `window_class_is` | `win32gui.GetClassName(GetForegroundWindow())` |
| `window_title_contains` | `win32gui.GetWindowText(GetForegroundWindow())`, case-insensitive |
| `text_appears` / `text_disappears` | fresh OCR, fuzzy match (rapidfuzz ratio ≥ 85) |
| `region_changed` | mean abs pixel diff in the circled bbox (+20 px pad) above threshold |

Cheap checks first (window class/title, region diff); OCR only when the check needs it. This keeps a CPU laptop responsive.

### 5.5 Wait and recover
- Poll ~1 Hz.
- **"I did it"** button always visible: forces success and moves on.
- **20 s with no change:** show the hint / rephrase once.
- **Unexpected change** (foreground window changed to something unrelated, or the target vanished without the check passing): **re-plan from the new screen**, never repeat the old step blindly. Count it as a recovery event (metric).

### 5.6 Coordinates, DPI, and the overlay (the bugs that will bite)
- `mss` returns **physical pixels**. Qt 6 is DPI-aware and positions widgets in **logical pixels**. Convert: `logical = physical / screen.devicePixelRatio()`. Write a calibration test on day 1: OCR a known word, circle it, eyeball it at 100% and 125%/150% scaling.
- Overlay flags: `FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowTransparentForInput`, plus `WA_TranslucentBackground`. Her clicks must pass through.
- **The overlay must not appear in our own screenshots**, or OCR will read our instruction text. Use `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE=0x11)` (Windows 10 2004+); verify it actually hides from `mss`. Fallback: hide overlay → capture → show.
- The prompt box and buttons are a **separate, normal (clickable) window**.

### 5.7 Threads
- Qt main thread owns all widgets.
- `pynput` `GlobalHotKeys` runs in its own thread → emit a Qt signal; never touch widgets from it.
- Capture/OCR/model run in a `QThread` worker; results come back via signals.
- Hotkeys: `Ctrl+Alt+H` open Tiny Me, `Ctrl+Alt+P` pause/stop everything.

---

## 6. Safety and privacy (README + post)

- Captures **only** after the hotkey or during an active task. Never continuous.
- Screenshots live in memory and are dropped after each step. Nothing saved unless she presses "Save this".
- **Sensitive-screen pause** before any model call if OCR text or window title matches: password, passcode, OTP, one-time, CVV, card number, expiry, UPI PIN, net banking, bank, sign in (when combined with a password field). Keyword list lives in `guard.py`, case-insensitive, fuzzy.
- Never types into login or payment fields. Never clicks for her on those screens.
- Visible "Tiny Me is looking" indicator whenever capture is active. `Ctrl+Alt+P` stops everything.
- Works with Wi-Fi off (demo this).
- **Sentry is off on her laptop.** It is enabled only by `TINYME_TELEMETRY=1` on my dev machine and in eval runs, with `send_default_pii=False`, and spans carry **only numbers and enums**: stage latencies, token counts, element count, check type, outcome. Never screenshots, OCR text, goals, or instructions.
- **Entire transcripts are pushed with the repo.** Never paste her screenshots, files, or personal details into Claude Code sessions. Eval screenshots come from **my** laptop with dummy files.
- I explain to her exactly what it can see, and she can say no.

---

## 7. Feature tiers

**Tier 1 (must work by Saturday night)**
Hotkey, prompt box, capture, OCR + UIA element list, Gemma picks an element, circle overlay with correct DPI, deterministic success checks, "I did it", 20 s hint, re-plan on unexpected change, sensitive-screen pause, pause hotkey. Scene A guide mode end to end.

**Tier 2 (Sunday)**
- "Find it for me" file search + `explorer /select`.
- Booking handover on the local mock site (Scene B) with DOM guard.
- Teach notes: `notes/her_setup.txt` (printer name, her browser, where she keeps photos). Brain prompt includes matching lines.
- Instructions in her preferred language (Gemma writes `instruction` in Hindi/Marathi if set in config). Optional but strong for the theme.
- Eval harness + results table.
- Sentry spans (dev mode).

**Tier 3 (only if the rest is solid)**
- Tinker fine-tune of `Qwen/Qwen3.5-4B` for element picking.
- Saved cheat-sheet cards.

**Cut line:** if Saturday ends without Scene A working multi-step, drop everything except Tier 1 + eval.

---

## 8. Sentry Agent Tracing design

One trace per task:
```
invoke_agent Tiny Me            (op gen_ai.invoke_agent, gen_ai.agent.name="Tiny Me")
 ├─ execute_tool capture        (op gen_ai.execute_tool)   ms
 ├─ execute_tool read_screen    (op gen_ai.execute_tool)   ms, element_count, uia_ms, ocr_ms
 ├─ execute_tool guard          (op gen_ai.execute_tool)   outcome=clear|paused
 ├─ chat gemma4:e2b             (op gen_ai.chat)           gen_ai.request.model, gen_ai.usage.input_tokens/output_tokens, ms, valid_json, retried
 └─ execute_tool wait_check     (op gen_ai.execute_tool)   check_type, outcome=passed|timeout|manual|replanned, wait_ms
```
Token counts come from Ollama's response (`prompt_eval_count`, `eval_count`). Set `gen_ai.provider.name` to `ollama` and `gen_ai.operation.name` on every span. Post screenshots of the AI Agents dashboard + one trace where a recovery happened. Write what the traces revealed (e.g. "OCR is 60% of step time, not the model").

---

## 9. Tinker plan (Tier 3)

> **Status, 4 Oct 2026:** built and tested, not run. `finetune/DECISION.md` is the
> record. Two blockers: only File Explorer is labelled, so there is no held-out
> *app* to test on, and 20 labelled rows give a 6-example test split where one
> example is 17 points. Also: the labels carry `target_id` only, so a model
> trained on this is a **picker**, not a drop-in brain — its `success_check` is
> templated and would regress MASTERSPEC 5.4's cheap checks. Cost is not the
> blocker (~$0.12 for 3 epochs).

- **Task:** given goal + history + element list → correct `target_id` (or `cannot_see_it`).
- **Data:** ~40 labeled screenshots × 2–4 goals each, frozen element lists stored as JSON (no images needed for training). Augment by shuffling element IDs and paraphrasing goals. **Split by app** (hold out one whole app) so test is honest.
- **Model:** `Qwen/Qwen3.5-4B`, LoRA, supervised fine-tuning via the Tinker cookbook. Pricing checked 4 Oct 2026: $0.737 / M training tokens, so a 3-epoch run on this dataset is cents, not dollars. Re-check before quoting.
- **Compare:** untuned vs tuned Qwen vs Gemma 4 e2b/e4b, on the held-out app: correct-element rate, JSON validity, latency. The rows are `tinker_base` and `tinker_tuned` in `eval/run_eval.py`, generated, never typed. Tinker latencies are a network round trip and are not comparable to the local rows.
- **Running locally:** if Tinker lets you download the LoRA weights, merge → GGUF → `ollama create tinyme-picker`. If that path doesn't work in time, report accuracy from Tinker's sampler and say local deployment is future work. Do not claim local speed you didn't measure.
- **Story:** "Open weights let me swap the brain. Gemma stays the default; the tuned picker is a drop-in." That is the open-innovation argument in one line.

---

## 10. Evaluation

- 8–10 everyday tasks across 4–5 apps (Explorer, Edge/Chrome, Notepad, Settings, Word or WhatsApp Desktop), ~40 labeled screenshots, from my laptop.
- Label file `eval/labels.jsonl`: `{screenshot, goal, history, correct_ids[], app}`. Multiple correct IDs allowed (e.g. a menu text and its icon).
- **Systems compared:**
  1. Gemma 4 with the screenshot, asked for pixel coordinates (correct if point falls in a correct bbox).
  2. Our pipeline with Gemma 4 e2b and e4b (numbered elements).
  3. Tuned Qwen (if Tier 3 happened).
  4. Optional: one closed multimodal model with the screenshot, my screenshots only.
- **Metrics:** correct-element rate, JSON validity, seconds per step (split: capture / read / model / total), task completion rate (live), recovery rate after a deliberate wrong click.
- **User test:** she does 3 tasks with Tiny Me and 3 the usual way (calling me). Time each, note where she hesitated, quote her exactly, including complaints.
- **Report failures honestly.** Icon-only buttons without UIA names, non-English UI, slow first model load.

---

## 11. Repo layout
```
tiny-me/
  README.md            privacy design, setup, limitations, post-deadline commits note
  CLAUDE.md
  pyproject.toml
  .claude/skills/      project skills
  app/
    main.py            QApplication, hotkeys, prompt window, task loop wiring
    config.py          model name, hotkeys, language, telemetry flag
    capture.py         mss capture, primary monitor, DPI info
    ocr.py             RapidOCR → elements
    uia.py             UI Automation → elements
    elements.py        Element dataclass, merge/dedupe, numbering, model-facing text
    brain.py           Ollama call, schema, validation, retry
    overlay.py         click-through circle + instruction, capture exclusion
    watcher.py         success checks, timeout hint, re-plan detection
    guard.py           sensitive-screen rules, do-it permissions
    actions.py         find file, explorer /select, Playwright booking flow
    telemetry.py       Sentry init + span helpers (no-op when disabled)
  demo_site/           local mock booking site (static HTML)
  notes/               teach notes
  eval/                screenshots/, labels.jsonl, run_eval.py, results.md
  finetune/            build_dataset.py, tinker_train.py, export notes
  tests/               pytest: guard, elements, brain validation, watcher checks
```

---

## 12. Demo video (60–90 s)
1. 3 s: text from mom ("beta, where did the file go?").
2. Wi-Fi off, shown on screen.
3. Scene A: circle, wait, circle, done. Include one wrong click and the recovery.
4. Scene B: it fills the form, stops at password: "this part is yours."
5. Results table on screen for 5 s.

## 13. Post
Structure: the 9 p.m. phone call → demo → how it works (one diagram) → why open matters here (offline, her screen never leaves her laptop, zero cost, swap/fine-tune the brain) → numbers including failures → handing it to her and what she said → privacy design → how I built it (Entire sessions, Sentry traces).
Titles: "My mom kept calling me at 9 p.m., so I built a tiny me that lives on her laptop" / "An AI that does the clicking, and stops at the payment page".

## 14. Risks
| Risk | Mitigation |
|---|---|
| Gemma slow on her CPU laptop | Test her machine Friday night. e2b, short prompts, ≤ 80 elements, one model call per step, keep model loaded (`keep_alive`). Report real numbers. |
| OCR misses icon-only buttons | UIA elements; keyboard-shortcut hint fallback; state it. |
| DPI mismatch → circle in the wrong place | Calibration test on day 1 at two scaling levels. |
| Overlay text read by OCR | Capture exclusion, verified. |
| Ticket sites forbid automation | Local mock site, said openly. |
| Tinker can't train Gemma | Fine-tune `Qwen/Qwen3.5-4B`; framed as model swap. |
| Telemetry contradicts privacy story | Off by default, dev-only, numbers only. |
| Rules | Repo created inside window; note any post-deadline commits in README; tags; 18+. |
