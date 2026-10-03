# Tiny Me

A Windows desktop assistant that watches one screen, decides what to click next, and draws a circle around it.

Tiny Me is built for a single non-technical user. She presses a hotkey, types what she wants in plain words, and the application either **shows her how** by circling the exact control to click and waiting until she clicks it, or **does it for her** while she watches. At any screen involving a password, a one-time code, a card number or a payment, it stops and hands control back.

Everything runs on the local machine. Optical character recognition, UI inspection and the language model are all local processes. No screen content is transmitted. The application functions with networking disabled.

---

## Table of contents

1. [Design constraints](#1-design-constraints)
2. [System architecture](#2-system-architecture)
3. [The per-step pipeline](#3-the-per-step-pipeline)
4. [Module reference](#4-module-reference)
5. [The element list](#5-the-element-list-the-models-entire-world)
6. [The model contract](#6-the-model-contract)
7. [Deterministic success checking](#7-deterministic-success-checking)
8. [The guard](#8-the-guard)
9. [Coordinate systems and DPI](#9-coordinate-systems-and-dpi)
10. [Threading model](#10-threading-model)
11. [The overlay](#11-the-overlay)
12. [Direct actions](#12-direct-actions)
13. [Telemetry](#13-telemetry)
14. [Evaluation harness](#14-evaluation-harness)
15. [Fine-tuning track](#15-fine-tuning-track)
16. [Measured performance](#16-measured-performance)
17. [Installation and running](#17-installation-and-running)
18. [Testing](#18-testing)
19. [Repository layout](#19-repository-layout)
20. [Limitations](#20-limitations)

---

## 1. Design constraints

These are architectural invariants, not preferences. Code that violates one of them is a defect regardless of whether it works.

| # | Invariant | Rationale |
|---|---|---|
| 1 | The model never emits coordinates. It returns an integer `target_id` selected from a list the application constructed. Bounding boxes are never placed in the prompt. | A language model asked for pixel coordinates produces plausible numbers that are wrong. Constraining the output to a key in a lookup table converts an open-ended regression problem into a closed-set classification problem, and makes an invalid answer detectable by set membership. |
| 2 | Success is verified deterministically in code. The model proposes which check to run; Python runs it. | A model asked "did that work?" will agree with itself. The checks are window class comparison, substring matching, fuzzy text matching and pixel differencing, all of which have a defined answer. |
| 3 | `guard.py` executes before every model call and before every automated action. | Placing the guard on the single code path to the model makes the privacy property true by construction rather than by remembering to check. |
| 4 | "Do it for me" permissions are an allow-list in Python, not a prompt instruction. | A system prompt can be argued with. A `frozenset` membership test cannot. |
| 5 | Never type into or click a password, OTP, CVV, card, or payment field. | The core promise of the product. |
| 6 | Screenshots exist only in memory. | Nothing in `app/` opens a file for image writing. Only `eval/` tooling, run on a developer machine, persists images. |
| 7 | Telemetry is disabled unless `TINYME_TELEMETRY=1`. Spans carry numbers and closed-vocabulary enums only. | Observability must not contradict the privacy claim. |
| 8 | Qt widgets are touched only on the main thread. | Hotkey callbacks and worker threads communicate by Qt signals. |
| 9 | Physical-to-logical pixel conversion happens in exactly one function. | Scattered conversions are the classic source of misplaced overlays on scaled displays. |
| 10 | The overlay is click-through and excluded from capture. | Otherwise the application reads its own instruction text back through OCR on the next step. |
| 11 | File Explorer is identified by window class `CabinetWClass`, never by title. | An Explorer window's title is the folder name, for example "Downloads" or "Home". It is never the string "File Explorer". |

---

## 2. System architecture

```
                            ┌──────────────────────────────┐
                            │   pynput GlobalHotKeys       │
                            │   (dedicated OS thread)      │
                            │   Ctrl+Alt+H   Ctrl+Alt+P    │
                            └──────────────┬───────────────┘
                                           │ Qt signal (thread hop)
                                           v
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Qt main thread (GUI)                                │
│                                                                             │
│   ┌──────────────────┐   ┌───────────────────┐   ┌──────────────────────┐   │
│   │  PromptWindow    │   │   TinyMe          │   │   Overlay            │   │
│   │  (ui.py)         │<->│   (main.py)       │-->│   (overlay.py)       │   │
│   │  normal window,  │   │   orchestrator    │   │   frameless, always  │   │
│   │  clickable       │   │   owns TaskLoop   │   │   on top, click      │   │
│   │                  │   │   owns QTimer     │   │   through, excluded  │   │
│   └──────────────────┘   └─────────┬─────────┘   │   from capture       │   │
│                                    │             └──────────────────────┘   │
└────────────────────────────────────┼────────────────────────────────────────┘
                                     │ QThread dispatch (Job)
                                     v
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Worker thread (per operation)                        │
│                                                                             │
│   capture.grab_primary()                                                    │
│          │  numpy array, RGB, physical pixels, in memory                    │
│          v                                                                  │
│   ┌──────────────┐        ┌──────────────┐                                  │
│   │  ocr.py      │        │  uia.py      │                                  │
│   │  RapidOCR    │        │  UI Automa-  │                                  │
│   │  ONNX, CPU   │        │  tion, COM   │                                  │
│   │  text boxes  │        │  named ctrls │                                  │
│   └──────┬───────┘        └──────┬───────┘                                  │
│          │                       │                                          │
│          └───────────┬───────────┘                                          │
│                      v                                                      │
│            elements.merge_elements()   IoU dedupe                           │
│            elements.cap_elements()     priority trim to 80                  │
│            elements.number_elements()  assign ids 1..n                      │
│                      │                                                      │
│                      v                                                      │
│            ┌───────────────────┐                                            │
│            │   guard.py        │  sensitive?  ──yes──> stop. No model call. │
│            └─────────┬─────────┘                                            │
│                      │ no                                                   │
│                      v                                                      │
│            ┌───────────────────┐      HTTP localhost:11434                  │
│            │   brain.py        │ <──────────────────────> Ollama / Gemma 4  │
│            │   structured out  │      JSON schema enforced                  │
│            └─────────┬─────────┘                                            │
└──────────────────────┼──────────────────────────────────────────────────────┘
                       │ Qt signal back to main thread
                       v
             Overlay draws circle + instruction
                       │
                       v
             ┌───────────────────┐
             │   watcher.py      │  polled at 1 Hz by QTimer on main thread
             │   success checks  │  passed / pending / replan / manual
             └─────────┬─────────┘
                       │
         ┌─────────────┼─────────────┬──────────────────┐
         v             v             v                  v
      PASSED        PENDING       REPLAN            MANUAL
    next step    keep waiting   re-plan from      she pressed
                 hint at 20 s   the new screen    "I did it"
```

### Data flow summary

The application is a loop over a single function: given the current screen and the goal, produce one instruction and one verification predicate. The loop terminates when the model sets `goal_reached`, when the step budget `MAX_STEPS = 12` is exhausted, or when the user stops it.

---

## 3. The per-step pipeline

Each iteration performs seven stages in a fixed order. The ordering is load-bearing in two places, noted below.

| Stage | Module | Output | Typical cost |
|---|---|---|---|
| 1. Capture | `capture.py` | `Capture(image, monitor, elapsed_ms)` | 15 to 80 ms |
| 2. Read | `ocr.py`, `uia.py` | merged, unnumbered elements | OCR dominates, see section 16 |
| 3. Cap and number | `elements.py` | at most 80 elements with ids 1..n | under 1 ms |
| 4. Guard | `guard.py` | `Verdict(clear or paused)` | under 1 ms |
| 5. Decide | `brain.py` | schema-validated `StepPlan` | see section 16 |
| 6. Show | `overlay.py` | circle plus instruction on screen | one frame |
| 7. Wait | `watcher.py` | poll at 1 Hz until an outcome | until she acts |

**Ordering constraint 1.** Elements are capped *before* they are numbered. Numbering first and capping second would present the model with a list containing gaps in the id sequence, and an id the application later fails to resolve.

**Ordering constraint 2.** The guard runs *after* reading and *before* planning. This is the only code path that reaches the model, which is what makes invariant 3 structural rather than procedural.

---

## 4. Module reference

### `app/config.py`

Single source of tunable constants. Reads a gitignored `.env` file into the process environment at import time using a nine-line parser rather than a dependency, with real environment variables taking precedence over file values.

| Constant | Value | Meaning |
|---|---|---|
| `MODEL` | `gemma4:e2b` | Ollama tag, overridable by `TINYME_MODEL` |
| `KEEP_ALIVE` | `10m` | How long Ollama holds the model resident |
| `MODEL_TIMEOUT_S` | `60.0` | Hard ceiling on one model call |
| `LANGUAGE` | `English` | Language the model writes instructions in |
| `HOTKEY_OPEN` | `<ctrl>+<alt>+h` | Opens the prompt window |
| `HOTKEY_PAUSE` | `<ctrl>+<alt>+p` | Stops everything immediately |
| `MAX_STEPS` | `12` | Step budget per task |
| `POLL_INTERVAL_MS` | `1000` | Watcher tick |
| `PAUSED_POLL_INTERVAL_MS` | `2500` | Slower tick while the guard holds a private screen |
| `STEP_HINT_AFTER_S` | `20.0` | Rephrase once after this much inactivity |
| `REGION_DIFF_THRESHOLD` | `8.0` | Mean absolute pixel difference counting as change |
| `REGION_PAD_PX` | `20` | Padding around the watched region |
| `TELEMETRY_ENABLED` | `False` | True only when `TINYME_TELEMETRY=1` |

### `app/capture.py`

Wraps `mss`. Grabs `monitors[1]`, the primary display, rather than `monitors[0]`, which is the union of all displays. Converts the BGRA buffer to a contiguous RGB array with an explicit copy, because the buffer `mss` returns is reused on the next grab and a slice would alias the following screenshot.

Also hosts three win32 geometry helpers, placed here because they return physical-pixel screen layout rather than UI Automation data:

- `foreground_rect()` returns `GetWindowRect(GetForegroundWindow())`.
- `taskbar_rect()` finds the taskbar by class `Shell_TrayWnd`, never by title.
- `screen_scale()` measures the display scale rather than querying it. The reason is that the obvious queries lie in this process: `GetDpiForSystem` and `GetDeviceCaps(LOGPIXELSX)` both return 96 on a 125 percent display, because the Python interpreter is not marked DPI-aware and Windows virtualises the answer. What Windows does not virtualise is `mss`, which reads the desktop composition surface and sees true physical pixels, while `GetSystemMetrics(0)` sees the virtualised logical width. The scale is the ratio of the two:

  ```
  scale = physical_width / logical_width
  ```

  On the development machine this evaluates to 1920 / 1536 = 1.25, matching Qt's `devicePixelRatio` to four decimal places.

### `app/ocr.py`

RapidOCR on the ONNX Runtime CPU execution provider. The package is `rapidocr`, not the older `rapidocr_onnxruntime`; the two have incompatible APIs, which is why the installed version is checked before use.

The engine is constructed once per process and cached in a module-level global, because construction loads three ONNX graphs from disk.

Filtering rules applied to raw output:

- Drop boxes with recognition score below `MIN_SCORE = 0.5`.
- Drop boxes shorter than `MIN_HEIGHT_PX = 8` pixels, which are artefacts rather than user interface text.
- Drop empty or whitespace-only strings.

`polygon_to_bbox` collapses RapidOCR's 4-point quadrilateral, shape `(4, 2)`, into an axis-aligned rectangle by taking per-axis extrema:

```
left   = min(x_i)      right  = max(x_i)
top    = min(y_i)      bottom = max(y_i)        for i in 0..3
```

The whole OCR path is wrapped so that any failure returns an empty list rather than raising. A step with no OCR elements can still proceed on UI Automation elements or fall back to a keyboard hint; an exception would end the task.

### `app/uia.py`

Windows UI Automation through the `uiautomation` package. This is the only source that can see controls with no visible text, which is what Scene A's first step depends on: the File Explorer taskbar icon has no label, so OCR cannot find it.

Scope is deliberately narrow. The walk visits only two roots, the taskbar (`Shell_TrayWnd`) and the foreground window, with depth limits of 7 and 6 respectively, under a total budget of 300 ms. Controls are kept only when they are named, on screen and enabled.

Two implementation details that cost real time and are therefore ordered carefully:

1. **COM initialisation is expensive on first call**, measured at 232 to 381 ms. This would consume the entire budget on the first step, so `main.TinyMe.start` calls `uia.warm_up()` during application startup. The expensive automation client is per-process rather than per-thread, so creating a new `QThread` for each step costs nothing extra.
2. **Every property read is a cross-process COM call.** `_snapshot` therefore reads `Name` first and control type second, discarding unnamed controls and pure containers before paying for the remaining three property reads. The measured effect was that the File Explorer taskbar icon moved from the 17th control the walk yielded to the 11th, because unnamed image children and named pane and group containers no longer occupy slots ahead of it. Position in the walk matters because the budget truncates from the end.

### `app/elements.py`

The data model and all pure geometry. Contains no I/O, which is what makes it exhaustively unit-testable.

```python
@dataclass(frozen=True)
class Element:
    id: int                      # assigned by number_elements; what the model returns
    text: str
    source: Literal["ocr", "uia"]
    role: str                    # UIA control type, or "text" for OCR
    region: str                  # one of nine screen regions
    bbox_px: Bbox                # physical pixels; never sent to the model
```

**Region classification.** The screen is divided into a 3 by 3 grid. An element is named by the grid cell containing its centre, not a corner, so a wide element is described by where it actually sits. For centre coordinate `c`, monitor origin `o` and monitor extent `s`:

```
index = clamp(floor(3 * (c - o) / s), 0, 2)
```

Boundaries resolve to the later region, so on a 1920-pixel-wide monitor an element centred at x = 640 is centre-column, not left-column.

**Intersection over union.** Used for deduplication:

```
IoU(A, B) = area(A ∩ B) / area(A ∪ B)
          = area(A ∩ B) / (area(A) + area(B) - area(A ∩ B))
```

**Merging.** A UI Automation element and an OCR box are treated as one thing when both conditions hold:

```
IoU > 0.5   AND   rapidfuzz.ratio(casefold(a), casefold(b)) >= 85
```

The UI Automation element survives, because its role is what tells the model the thing is clickable. Matching is greedy: each UIA element claims the OCR box it overlaps most, and each OCR box can be claimed once. This is not globally optimal, but two OCR boxes can only both exceed IoU 0.5 against one UIA box if they also heavily overlap each other, which OCR does not produce. Ties keep the earlier OCR index and the outer loop follows input order, so the result is deterministic.

**Capping.** `MAX_ELEMENTS = 80`. When the merged list is longer, elements are ranked by a priority function and the top 80 are kept, then restored to reading order so numbering remains sensible:

```
priority = 2  if the element lies inside the taskbar rect
           1  if it lies inside the foreground window rect
           0  otherwise
```

The taskbar outranks the foreground window because taskbar icons are precisely the elements OCR cannot see, and they are the entry point for Scene A.

**Reading order.** Elements are sorted top to bottom, then left to right, with rows of near-equal vertical position banded together so that a row of controls is numbered left to right rather than by sub-pixel vertical jitter.

**Physical to logical conversion.** `to_logical(bbox, dpr)` is the single conversion point required by invariant 9:

```
logical = physical / devicePixelRatio
```

It raises `ValueError` on a non-positive ratio rather than silently drawing the circle at the origin.

### `app/brain.py`

The only module that talks to the model.

The Ollama client is created once with `timeout=config.MODEL_TIMEOUT_S`. Each call sets `temperature=0`, passes the Pydantic JSON schema as `format`, and sets `keep_alive`. Gemma's thinking output is disabled with `think=False`; some Ollama server and model builds reject that parameter outright, so a rejection whose message mentions `think` is retried once without it, rather than being treated as a dead server. The capability is not cached between calls, because cached capability state made the test suite order-dependent, and the wasted call fails locally and immediately before any tokens are generated.

Control flow of `plan_step`:

1. If the element list is empty, return a fallback without calling the model. There is no question worth asking, and she would wait several seconds for a guess.
2. Build messages, attach any teach notes matching the goal.
3. Call the model. On transport failure, return a fallback immediately. A transport failure is **not** retried, because a server that is not running will not be running one second later, and retrying would make her wait two timeouts to learn the same thing.
4. Validate the reply against the schema and against the set of valid ids.
5. On validation failure, retry **once** with the error message appended to the conversation.
6. On a second failure, synthesise a `cannot_see_it` fallback plan.

The function always returns a usable `BrainResult`. There is no error path that reaches the user as an exception.

**Teach notes.** `notes/her_setup.txt` holds facts about the user's machine, one per line. `notes_for_goal` selects notes sharing a content word with the goal and includes them in the prompt. The matching is lexical rather than semantic, which is a known limitation documented in section 20.

### `app/guard.py`

See section 8.

### `app/overlay.py`

See section 11.

### `app/watcher.py`

See section 7.

### `app/actions.py`

See section 12.

### `app/telemetry.py`

See section 13.

### `app/main.py`

Orchestration. Contains `StepOutcome`, `TaskLoop`, `Job`, `HotkeyBridge` and `TinyMe`.

- `plan_next_step` is the composed pipeline function: capture, read, cap, number, guard, plan, resolve target. It takes every stage as a keyword argument defaulting to the real implementation, which is what allows the whole pipeline to be tested with no screen, no model and no Windows.
- `TaskLoop` is a pure state machine over `LoopPhase`, holding the goal, the history of completed instructions and the step counter. It has no Qt dependency.
- `Job` wraps a callable so it can be run on a `QThread` and deliver its result as a signal.
- `HotkeyBridge` owns the `pynput` listener and converts OS-thread callbacks into Qt signals.
- `TinyMe` is the Qt object that owns the windows, the timer and the worker thread, and wires the above together.

The resolved target element is looked up by id rather than trusted. `brain` has already validated that the id was in the list, and `plan_next_step` returns `None` rather than a guess if the lookup somehow fails. A confident circle around the wrong control is worse than no circle.

### `app/ui.py`, `app/widgets.py`, `app/theme.py`, `app/icons.py`

The prompt window and its custom-drawn widget set. `PromptWindow` is a normal, clickable, frameless window with a custom title bar, a sidebar and five pages: Home, History, Examples, Settings and About. `widgets.py` contains the painted primitives: surfaces with soft shadows, cards, chips, buttons, toggles, status pills, animated thinking dots, banners, step rows, a progress track, a fading stack and toasts. `theme.py` holds the colour tokens and `icons.py` the vector icon paths.

---

## 5. The element list, the model's entire world

The model never receives an image, a coordinate or a window handle. It receives a numbered list of lines in exactly this format:

```
17 | "Downloads" | uia:ListItem | left-middle
```

The four fields are the id, the visible text, the source and role, and the region. A real list from the development machine, truncated:

```
 1 | "Minimize"            | uia:Button | top-right
 2 | "Maximize"            | uia:Button | top-right
 3 | "Close"               | uia:Button | top-right
 6 | "Start"               | uia:Button | bottom-left
 7 | "Search"              | uia:Button | bottom-center
10 | "File Explorer"       | uia:Button | bottom-center
11 | "Microsoft Edge"      | uia:Button | bottom-center
```

This representation is the central design decision of the project. Its consequences:

- An invalid answer is detectable. `target_id` either is or is not a key in the current list.
- The prompt is small and textual, so a 2-billion-parameter edge model can be used instead of a vision model.
- Bounding boxes never enter the prompt, so the model cannot leak them and cannot be wrong about them.
- The same frozen lists can be replayed offline, which is what makes the evaluation harness and the fine-tuning dataset possible without storing images.

---

## 6. The model contract

Ollama structured output is used, with the Pydantic JSON schema passed as `format` and `temperature` fixed at 0.

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

Validation performed in Python after the model returns:

| Rule | Behaviour on violation |
|---|---|
| `target_id` must be a key in this step's list, unless `goal_reached` or `cannot_see_it` is set | One retry with the error appended, then fall back to `cannot_see_it` |
| `instruction` at most 90 characters | Same retry path |
| `success_check.type` must be one of the five enum members | Same retry path |
| `cannot_see_it` true | No circle is drawn; `hint_if_missing` is shown instead |

The five permitted check types are `window_class_is`, `window_title_contains`, `text_appears`, `text_disappears` and `region_changed`.

---

## 7. Deterministic success checking

`watcher.py` answers "did that work?" without consulting the model.

| Check type | Implementation | Requires OCR |
|---|---|---|
| `window_class_is` | `GetClassName(GetForegroundWindow())`, case-insensitive exact comparison | No |
| `window_title_contains` | `GetWindowText(GetForegroundWindow())`, case-insensitive substring | No |
| `text_appears` | fresh OCR, `rapidfuzz.ratio >= 85` against any recognised string | Yes |
| `text_disappears` | the same match, negated, and false when OCR returned nothing | Yes |
| `region_changed` | mean absolute pixel difference over the padded target box | No |

Two details worth stating:

**Exact class, substring title.** `window_class_is` compares for equality rather than prefix, because a substring match would accept a different window class that happens to share a prefix. `window_title_contains` is a substring test by definition, and rejects an empty value, which would otherwise be contained in every title and produce instant false success.

**`ratio`, not `partial_ratio`.** An earlier version used `partial_ratio`, which ordinary screen text clears at threshold 85, producing false successes. The same mistake had previously produced a false positive in the overlay capture-exclusion verification. Both now use whole-string `ratio`.

**Region differencing.** For two equally shaped arrays `B` (before) and `A` (after) of the padded target region:

```
d = mean( |int16(B) - int16(A)| )     over all pixels and channels
changed = d > REGION_DIFF_THRESHOLD   (8.0 on a 0 to 255 scale)
```

The cast to a signed type is required. Subtracting `uint8` arrays wraps around, so `0 - 255` evaluates to 1, and a black-to-white flip would be reported as no change at all. Differing shapes return positive infinity, because a window that moved or resized has changed by any reading and is not a condition to crash on mid-task.

The threshold was calibrated rather than guessed. Measured noise floor across 15 captures of untouched regions was exactly 0.000, because a desktop screenshot has no sensor noise. Real content changes measured 24.2, 37.6 and 41.6. The arithmetic maximum, all black against all white, is 255.0. The threshold of 8.0 therefore sits roughly 3 times below the smallest observed real change and roughly 20 times above the largest plausible incidental change, such as a blinking text caret, which for a 2 by 16 pixel caret in a 240 by 80 padded region occupies 0.17 percent of the area and can contribute at most about 0.4 to the mean.

### Poll outcomes

`StepWatch.poll` returns one of four outcomes, evaluated in this order:

1. **PASSED.** The success check returned true.
2. **REPLAN, window changed.** A different window handle is now in the foreground.
3. **REPLAN, target changed.** The pixels under the circle changed but the check still says no, meaning something happened that the check did not predict.
4. **PENDING.** Nothing yet. A hint is emitted once, after `STEP_HINT_AFTER_S = 20` seconds.

Success is tested first and this ordering is deliberate: opening File Explorer both satisfies the check and changes the foreground window, and a step that worked must never be reported as an unexpected change. The target-changed branch is skipped for `region_changed` checks, since for those a changed region is the success condition rather than a surprise.

A fifth outcome, **MANUAL**, is produced not by polling but by the always-visible "I did it" control, which forces success and advances the loop.

Cheap checks are ordered before expensive ones. Window class and title checks and region differencing require no OCR; only the two text checks pay for a recognition pass. On a CPU-bound laptop this is the difference between a responsive poll and an unusable one.

---

## 8. The guard

`guard.py` answers two independent questions, kept in one file so the policy can be read in one place.

### 8.1 Is this screen private?

`is_sensitive(elements, window_title)` returns a verdict. When it says yes, nothing is sent to the model and nothing is clicked.

Applying MASTERSPEC's keyword list naively causes constant false pauses. The string "bank" appears in a file called `Bank holiday list.docx`, and fuzzy-matching a four-letter word puts "Back" one character slip away from "bank" on every browser toolbar. Two refinements resolve this.

**Tiering.** A strong keyword is sufficient on its own. A weak keyword counts only in the company of a second, different weak keyword, which generalises the specification's own clause about "sign in" counting when combined with a password field.

| Tier | Keywords |
|---|---|
| Strong | password, passcode, otp, one time, one time code, one time password, cvv, cvc, card number, upi pin, net banking, netbanking, security code, verification code |
| Weak | bank, sign in, signin, log in, login, expiry, expiration, card, credit card, debit card, payment, pay now, username, account number |

**Length-gated fuzziness.** Fuzzy matching applies only to keywords of `MIN_FUZZY_LEN = 6` characters or more, at threshold `FUZZY_RATIO = 85`. Short keywords such as "otp", "cvv" and "bank" are matched exactly, on a word boundary. Consequently "Back" is not "bank" and "Options" is not "OTP", while a single misread character in "password" is still caught.

Text is normalised before matching. All non-alphanumeric runs collapse to single spaces and the string is case-folded, so "Card-Number:" and "CARD  NUMBER" both become `card number` and multi-word keywords reduce to a substring search.

The asymmetry is intentional and stated plainly: failing to detect a password screen breaks the product's central promise, whereas pausing on an ordinary screen costs one tap on "I did it". When uncertain, this module pauses.

### 8.2 May "Do it for me" act at all?

`can_do_it(task_type)` is an allow-list membership test. Anything not explicitly permitted is refused and the task falls back to guide mode. Structuring it as an allow-list rather than a deny-list means that adding a new `TaskType` cannot accidentally grant permission.

| Task type | Permitted |
|---|---|
| `FIND_FILE` | Yes |
| `OPEN_FOLDER` | Yes |
| `BROWSE_AND_FILL` | Yes, visibly, in a headed browser |
| `CREDENTIALS` | No |
| `PAYMENT` | No |
| `DELETE` | No |
| `SEND` | No |
| `UNKNOWN` | No |

### 8.3 The browser guard

Inside a browser the guard does better than reading pixels, because the DOM states what a field is. An `input[type=password]` is a password box regardless of its label, and `autocomplete="one-time-code"` is a one-time code box even on a page with no other clue.

A field is classified as hers when any of the following hold:

- `type` is `password`
- `autocomplete` begins with `cc-`, the HTML specification prefix for every card field
- `autocomplete` is `one-time-code`
- the label, name, id or placeholder contains a sensitive keyword as a whole word

The desktop guard pauses on a *screen*. The browser guard refuses a *field*, by name, before anything is typed. Scene B is therefore held to a stricter standard than Scene A.

One false positive is retained deliberately: in India "PIN code" means a postal code, so an address form containing that label is handed to her. The cost is that she types her own postcode on a page where she was typing anyway. The alternative, removing `pin` from the keyword list, would permit typing into a UPI PIN box.

---

## 9. Coordinate systems and DPI

Three coordinate spaces are in play and conflating any two misplaces the circle.

| Space | Produced by | Units |
|---|---|---|
| Physical pixels | `mss`, win32 `GetWindowRect` | true display pixels |
| Logical pixels | Qt widget geometry | physical divided by scale factor |
| CSS pixels | `getBoundingClientRect` in Playwright | page pixels, scale factor pinned to 1 |

The conversion is:

```
logical = physical / devicePixelRatio
```

performed in `elements.to_logical` and nowhere else. On the development machine at 125 percent scaling the measured ratio is 1920 / 1536 = 1.2500, equal to Qt's `devicePixelRatio` to four decimal places.

The browser case required a correction worth recording. Playwright launches Chromium with `deviceScaleFactor` pinned to 1, so the page reports `window.devicePixelRatio === 1` on a 125 percent display. Scaling a field rectangle by the page's own number placed the circle one field too high, producing a tidy ring around the username box on the login page. The scale now comes from the screen, through `capture.screen_scale()` or Qt's `devicePixelRatio` when the application is running, and a regression test supplies two different scales and requires the computed rectangle to follow them.

A field's screen position in the browser is computed as:

```
screen_x = window_origin_x + rect.left * scale
screen_y = window_origin_y + (outerHeight - innerHeight) + rect.top * scale
```

The `outerHeight - innerHeight` term attributes the entire window-chrome difference to the top of the window. That is correct for a default headed Chromium with no bookmarks bar, no bottom bar and no sidebar, and is a documented limitation otherwise.

---

## 10. Threading model

| Thread | Owns | Constraint |
|---|---|---|
| Qt main | every widget, the overlay, the poll timer, `TaskLoop` | all widget access happens here |
| pynput listener | the global hotkey hook | emits Qt signals only, never touches widgets |
| `QThread` worker | capture, OCR, UI Automation, the model call, Playwright | returns results by signal |

Creating a worker thread per operation is acceptable because the expensive UI Automation client is per-process rather than per-thread, measured by comparing `collect` in the main thread against `collect` in a fresh thread and finding no additional cost beyond normal variance.

---

## 11. The overlay

A frameless, translucent, always-on-top, click-through window covering the primary screen. Window flags:

```
FramelessWindowHint | WindowStaysOnTopHint | Tool | WindowTransparentForInput
plus WA_TranslucentBackground
```

`WindowTransparentForInput` is what allows her clicks to pass through to the application underneath.

### Capture exclusion

The overlay must not appear in the application's own screenshots, or OCR reads the instruction text back on the next step and the model sees its own output as part of the screen.

The primary mechanism is `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` where the constant is `0x11`, available on Windows 10 version 2004 and later. The fallback, used when that call fails, is to hide the overlay, capture, then show it again.

Verification on the development machine: the call succeeds, `GetWindowDisplayAffinity` reads back `0x11`, and a distinctive probe token rendered on the overlay never appears in OCR of a screenshot taken while the overlay is displayed. Checked at settle delays of 0, 0.15, 0.5 and 1.0 seconds, with no leak in any case. The fast path is therefore active and the hide-capture-show fallback is not exercised here.

The first version of that verification reported a false positive, because it probed with the words "TINYME OVERLAY CHECK" and accepted `fuzz.partial_ratio >= 85`, a threshold ordinary screen text clears. It would have silently forced the slow path forever. The probe is now the distinctive token `XQZ7-TINYME-PROBE-XQZ7` matched at ratio 90, and the matcher is covered by a unit test.

### Drawing

The mark is a pulsing ring by default, or a rounded rectangle frame when the target is wide and short, chosen by `choose_shape` from the box's dimensions and aspect ratio against `FRAME_MIN_WIDTH = 56`, `FRAME_MIN_HEIGHT = 14` and `FRAME_MIN_RATIO = 2.6`. A label carrying the instruction is placed adjacent to the mark, with a leader line when the gap exceeds a threshold, and is kept inside the screen margin. A pulse animation runs at 50 ms intervals with an amplitude of 3.5 logical pixels.

A separate banner reading "Tiny Me is looking" is displayed whenever capture is active, satisfying the requirement for a visible indicator.

---

## 12. Direct actions

`actions.py` implements the two "do it for me" capabilities.

### 12.1 Find it for me

Resolves the real Downloads folder through the Known Folder API, using `FOLDERID_DOWNLOADS` = `{374DE290-123F-4565-9164-39C4925E467B}`, rather than assuming `~/Downloads`. The distinction matters because the folder can be and has been relocated.

Goal text is tokenised into name hints with filler words removed. Candidate files are scored with `rapidfuzz.WRatio` against each hint. If the best score reaches `NAME_MATCH_RATIO = 72` the match is offered; otherwise the newest file is offered and the message explicitly says so, so that an answer to a different question is visible as such. A confident presentation of an unrelated file would not be visible as a mistake.

The result is revealed with `explorer /select,"<path>"`, which opens Explorer with the file already highlighted.

Search is one level deep and Downloads only. Downloads folders commonly contain an archive subfolder, and descending into it converts a 50 ms answer into a disk crawl.

### 12.2 The booking flow

A headed Chromium instance driven by Playwright against `demo_site/`, a five-page static stand-in served by `python -m http.server`. Real ticketing sites forbid automation in their terms and present CAPTCHAs, so none was ever targeted. The handover logic reads the DOM and is not site-specific, but the claim that it would work on a particular live site is not one this repository has earned.

The flow parses an origin, a destination and a date from her sentence using a small set of regular expressions covering `DD Month`, `Month DD`, ISO `YYYY-MM-DD` and `DD/MM/YYYY`, fills the search form, selects the first train, and proceeds until the DOM guard identifies a field as hers. At that point it draws a circle around the field, displays the handover message, and waits.

The resume condition is that the field has disappeared, not that it contains text. Waiting for a non-empty value does not work: the password box is still present and still hers, so the guard finds it again on the next pass and hands over in a loop. Waiting for the field to vanish means she presses the submit button herself, which is correct in any case because signing in is not on the permission table. It also means Tiny Me never asks the page what she typed.

A handover has a ceiling of `HANDOVER_TIMEOUT_S = 300` seconds. If she walks away, the flow stops rather than waiting indefinitely. Her partly filled form is left exactly as it is and the browser stays open.

---

## 13. Telemetry

Sentry agent tracing, for developer and evaluation runs only. This is the one module that could contradict the privacy claim, so it is written to make that difficult rather than merely unlikely.

- **Off unless asked.** `init` does nothing unless `TINYME_TELEMETRY=1` and `SENTRY_DSN` are both set. On her machine neither is, so every helper is a no-op context manager and `sentry_sdk` is never imported.
- **Closed vocabulary.** `clean` drops anything that is not a number, a boolean, or a string drawn from a declared vocabulary, under a key drawn from a declared key set. There is no code path that places an arbitrary string on a span, so a goal, an instruction, a window title, a filename or a line of OCR cannot reach Sentry by being passed to the wrong argument. It would be dropped and logged.
- **No breadcrumbs, no error events, no local variables.** Spans are the only thing transmitted. Breadcrumbs would carry the application's own INFO log lines and an exception event would carry local variables, and `goal` is a local variable in half the call stack. Both are disabled in `init`.
- `send_default_pii=False`.

Trace shape, one trace per task:

```
invoke_agent "Tiny Me"              op gen_ai.invoke_agent
 ├─ execute_tool capture            ms
 ├─ execute_tool read_screen        ms, element_count, uia_ms, ocr_ms
 ├─ execute_tool guard              outcome = clear | paused
 ├─ chat <model>                    op gen_ai.chat, request.model,
 │                                  usage.input_tokens, usage.output_tokens,
 │                                  ms, valid_json, retried
 └─ execute_tool wait_check         check_type, outcome, wait_ms
```

Token counts come from Ollama's `prompt_eval_count` and `eval_count`. `gen_ai.provider.name` is set to `ollama` rather than `google`, because that is where the call actually goes.

Spans are created by explicit parenting with `parent.start_child(...)` rather than through Sentry's ambient scope, because the stages execute on a worker thread while the task's root span was started on the main thread, and only explicit parents survive that hop.

Nothing in this module may raise. Every public entry point swallows its own failures and logs them, because a telemetry bug must not end her task.

---

## 14. Evaluation harness

`eval/run_eval.py` scores several systems against the same frozen screens, all answering one question: which element on this screen should be clicked next.

| System | Description |
|---|---|
| `pipeline_<tag>` | This application's pipeline, numbered elements, at the named Ollama tag |
| `vision_coords_<tag>` | The same model given the screenshot and asked for pixel coordinates; correct when the returned point falls inside a correct box |
| `tinker_base` | The untuned Qwen model hosted on Tinker, same prompt |
| `tinker_tuned` | The LoRA-tuned variant |

Metrics are correct-element rate, JSON validity, and seconds per step split into capture, read and model.

Labels live in `eval/labels.jsonl`, one JSON object per line, with `//` comment lines and blank lines ignored:

```json
{"screenshot": "...", "goal": "...", "history": [...], "correct_ids": [17, 23], "app": "explorer"}
```

Multiple correct ids are permitted, because a menu item and its icon are both defensible answers.

Element lists are frozen to `eval/elements/*.json` so that scoring is reproducible and requires no images at run time. Screenshots are gitignored. `eval/forbidden.txt`, also gitignored, lists text fragments that must never appear in a retained capture; a capture containing one is deleted rather than saved.

The current state of the dataset and what may be claimed from it is given in section 20.

---

## 15. Fine-tuning track

A LoRA supervised fine-tune of `Qwen/Qwen3.5-4B` on Tinker, framed as a model-swap demonstration: open weights allow the brain to be replaced, with Gemma remaining the default.

`finetune/build_dataset.py` converts `eval/labels.jsonl` into Tinker chat-format JSONL. Prompts are generated by calling `app.brain.build_messages` itself, so the training data cannot drift from the shipped prompt. Augmentation shuffles and renumbers the element list, which is the dimension that matters for a picker, thins non-target elements, and paraphrases the goal wrapper. The split is by application, the script refuses to run when only one application is present, and it checks for train and test leakage by row id.

`finetune/tinker_train.py` implements the documented cookbook flow: `conversation_to_datum` with `LAST_ASSISTANT_MESSAGE`, `forward_backward`, `optim_step`, `save_weights_for_sampler`. It is a dry run by default, prints a cost estimate, and refuses to proceed above `--budget-usd`.

**Status.** The pipeline is built and tested. The training run has not been performed and no tuned number exists. `finetune/DECISION.md` is the record. The blockers are documented there and in section 20.

---

## 16. Measured performance

Machine: Windows 11 build 10.0.26200, 12 logical CPUs, 1920 by 1080 physical at 125 percent scaling, logical 1536 by 864, Python 3.11, no GPU execution provider available to ONNX Runtime.

All figures below were measured on this machine. Nothing in this section is estimated. Where a number does not exist, the row says so rather than offering a plausible value.

### Stage costs

| Stage | Measurement |
|---|---|
| `mss` primary-monitor grab | 15 to 80 ms |
| RapidOCR engine first load | 647 ms, once per process |
| OCR, warm, full 1920 by 1080 screen | 9.6 to 16.7 s across repeated runs |
| UI Automation first COM call | 232 to 381 ms |
| UI Automation warm `collect`, taskbar plus foreground | 74 to 305 ms depending on machine load |
| Elements found, merged and capped | 65 to 80 |

### The dominant cost

OCR is the bottleneck, not the model, and by a wider margin than the specification anticipated. The specification expected roughly 60 percent of step time; on this hardware it is closer to 90 percent of the non-model time, and about 10 seconds elapses before the model has been asked anything.

Two measurements narrow the cause. A full 1920 by 1080 screen takes 15.4 s and yields 62 text boxes. A 1920 by 60 taskbar strip, which is 3 percent of the pixel count, still takes 4.8 s and yields 10 text boxes. Cost therefore does not scale with pixel count. It scales with the number of detected text regions, at roughly 0.25 to 0.5 s per recognised box, plus a fixed per-call overhead.

A contributing factor identified in the default configuration is `Det.limit_type: min` with `Det.limit_side_len: 736`, which scales the image so that its **shorter** side becomes 736 pixels. For a thin horizontal strip this is an upscale: a 1920 by 60 crop becomes approximately 23552 by 736. Cropping to the taskbar is therefore actively pathological under the default settings, which explains why an earlier attempt at a half-scale downscale measured slower rather than faster.

Mitigations available in the RapidOCR configuration, which the application currently does not set, are disabling the orientation classifier (`Global.use_cls`), since screenshots are never rotated, and switching detection to `limit_type: max` so the longer side is bounded instead of the shorter side being inflated.

### The model

| Measurement | Value |
|---|---|
| Cold model load plus one token, `gemma4:e2b` | 31.8 s |
| Warm call plus one token | 0.9 s |
| Warm `plan_step`, real 80-element screen | 26.5 s |
| The same with the machine loaded by a concurrent OCR benchmark | 50.5 s |

#### A cold-start defect, found and fixed

The first `plan_step` of a session against a cold server measured 60.8 s and returned a fallback. Load at 31.8 s plus inference at roughly 26 s overran `MODEL_TIMEOUT_S = 60.0`, the HTTP client raised a read timeout, and `plan_step` converted it into the `cannot_see_it` hint it is designed to produce. The user-visible symptom was that the **first** step of every session was a generic hint instead of a circle, while every subsequent step worked. It was invisible to the unit tests because those mock the client.

The fix is `brain.warm_up()`, called from `TinyMe.start` on a daemon thread by `TinyMe._warm_model`. This is the same move `uia.warm_up()` already makes for COM, two orders of magnitude larger, which is why it needs the thread: 31.8 s on the GUI thread would freeze the prompt window before she could type. Verified against a genuinely cold server:

```
[warm_up ] finished in 22.0s
[step 1  ] 50.5s  fell_back=False  retried=False
           instruction: 'Click on the File menu.'
           target_id=9  check=text_appears='File menu'
           resolves to: 'File' (ocr:text)
```

A residual race remains and is documented rather than papered over. If she types a goal within a few seconds of launch, her step queues behind the still-running warm-up and pays the load anyway. Ollama serialises the two requests so nothing breaks, but the 60 s ceiling can still be reached in that narrow window. `MODEL_TIMEOUT_S` was deliberately not widened: it encodes a product judgement about how long she should watch a spinner, and changing it to conceal a four-second race is the wrong trade to make silently.

#### OCR tuning, attempted and rejected

Five RapidOCR configurations were measured against the same screen, one warm-up plus three timed runs each.

| Configuration | Min | Median | Texts |
|---|---|---|---|
| defaults | 12.88 s | 14.56 s | 140 |
| `Global.use_cls=False` | 13.15 s | 38.70 s | 141 |
| `use_cls=False`, `Det.limit_type=max`, 960 | 30.31 s | 31.45 s | 141 |
| `use_cls=False`, `Det.limit_type=max`, 736 | 14.15 s | 15.71 s | 141 |
| the 960 case plus `Rec.rec_batch_num=16` | 18.96 s | 22.91 s | 141 |

No configuration beats the default, and the spread within a single configuration, 13 s to 39 s for identical settings, exceeds every difference between configurations. The knobs are therefore not the problem, and tuning them further is wasted effort. The remaining options are architectural: lean on UI Automation and treat OCR as a fallback, or keep OCR off the per-step path entirely except for the two checks that genuinely require it. Neither has been done.

### Derived per-step budget

Summing the warm measurements for a guide step whose success check does not require OCR:

```
capture   0.07 s
OCR      10.8  s
UIA       0.23 s
model    26.5  s
          ------
total    37.6  s per step
```

This is far above the 15 s per step figure the project plan set as its checkpoint threshold. Stated plainly: on this hardware, in its current configuration, the application is functionally correct but too slow for comfortable interactive use, and the first step additionally fails to a hint.

### Not measured

- Task completion rate and recovery rate. These require the application running against a changing screen with a person in front of it. `eval/live_runs.md` is the log for those and is empty.
- The vision-coordinate baseline. One call with a 1920 by 1080 screenshot did not return within 180 s on this CPU while OCR was also running.
- Any tuned-model number. The training run has not happened.

---

## 17. Installation and running

### Prerequisites

- Windows 10 version 2004 or later, or Windows 11
- Python 3.11, specifically; `requires-python` is pinned to `==3.11.*`
- Ollama

### Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
ollama pull gemma4:e2b
```

### Run

```
python -m app.main
```

The application starts with no visible window. Press `Ctrl+Alt+H` to open the prompt window, type a goal, and choose "Show me how" or "Do it for me". Press `Ctrl+Alt+P` at any time to stop everything.

To exercise the booking scene, serve the demo site from the repository root in a second terminal:

```
python -m http.server 8000
```

The flow targets `http://localhost:8000/demo_site/index.html`.

### Other entry points

```
pytest -q                           # unit tests
python eval\run_eval.py --systems all
python scripts\bench_gemma.py       # model latency table
python scripts\calibrate.py         # DPI and overlay placement check
python scripts\dump_elements.py --timings
```

### Environment variables

| Variable | Effect |
|---|---|
| `TINYME_MODEL` | Override the Ollama tag |
| `TINYME_LANGUAGE` | Language the model writes instructions in |
| `TINYME_TELEMETRY` | `1` enables Sentry; anything else disables it |
| `SENTRY_DSN` | Required in addition to the above for Sentry to initialise |

A gitignored `.env` file at the repository root is read at import time. Real environment variables take precedence over its contents.

---

## 18. Testing

```
pytest -q
```

The suite covers guard keyword and DOM rules, element merging, deduplication, capping and numbering, brain schema validation and the retry path with Ollama mocked, each watcher check type against fixture images, physical to logical conversion, overlay geometry and probe matching, the Known Folder and file-matching logic, the booking flow against a fake page object, telemetry attribute scrubbing, the evaluation collector and runner, and dataset construction.

Every stage of `plan_next_step` is injected as a keyword argument, which is what permits the full pipeline to be tested with no screen, no model and no Windows API available.

---

## 19. Repository layout

```
tiny-me/
  README.md               this file
  CLAUDE.md               agent working rules
  MASTERSPEC.md           the specification
  PROJECTPLAN.md          schedule and scope
  PROMPTS.md              the build prompts
  pyproject.toml
  .claude/skills/         project skills
  app/
    main.py               QApplication, hotkeys, prompt window, task loop
    config.py             constants and environment knobs
    capture.py            mss capture, window and taskbar geometry, screen scale
    ocr.py                RapidOCR to elements
    uia.py                UI Automation to elements
    elements.py           Element model, geometry, merge, cap, numbering
    brain.py              Ollama call, schema, validation, retry, teach notes
    overlay.py            click-through marks, labels, capture exclusion
    watcher.py            success checks, hint timing, replan detection
    guard.py              sensitive-screen rules, permission table, DOM guard
    actions.py            find file, explorer /select, Playwright booking flow
    telemetry.py          Sentry init and span helpers, no-op when disabled
    ui.py                 prompt window and pages
    widgets.py            painted widget primitives
    theme.py              colour tokens
    icons.py              vector icon paths
  demo_site/              five-page static mock booking site
  notes/
    bench.md              every measured number
    limitations.md        the honest list, with how each was found
    her_setup.txt         teach notes
    user_test.md          user session notes
  docs/
    sentry.md             tracing notes
  eval/
    collect.py            screen collection
    capture_tool.py
    labels.jsonl          goal and correct-id labels
    elements/             frozen element lists
    screenshots/          gitignored
    forbidden.txt         gitignored leak blocklist
    run_eval.py           scoring harness
    live_runs.md          live-run log, currently empty
  finetune/
    build_dataset.py      labels to Tinker chat JSONL
    tinker_train.py       LoRA training flow
    data_card.md
    DECISION.md           why the run has not happened
  scripts/
    bench_gemma.py
    calibrate.py
    dump_elements.py
  tests/                  pytest suite
```

---

## 20. Limitations

Stated plainly, because the value of the project rests on the numbers being trustworthy.

### Performance

- **Too slow for comfortable interactive use on this hardware.** Approximately 37.6 s per warm step, against a target of 15 s. See section 16. This is the single biggest outstanding problem and it is not a configuration issue; the remaining fixes are architectural.
- **A residual cold-start race.** The cold-start defect itself is fixed by the startup warm-up, but a goal typed within a few seconds of launch still queues behind the load and can reach the 60 s ceiling.
- **OCR cost scales with text-box count, not pixel count,** and the default detection configuration upscales thin crops rather than downscaling them, which makes cropping to the taskbar actively counterproductive and defeats the obvious optimisation.

### Platform

- **Windows only.** Mac is out of scope.
- **Primary monitor only.**
- **Text input only.** Voice was cut.
- **Verified at 125 percent display scaling.** The 100 percent case is covered by the conversion arithmetic and a calibration script but has not been eyeballed.

### Recognition

- **Icon-only buttons without UI Automation names are invisible** to both sources. The fallback is a keyboard-shortcut hint.
- **Non-English interface text** is not covered by the keyword lists in `guard.py`.
- **Under machine load the foreground window may receive little or no UI Automation budget,** because the taskbar is walked first and may consume the allocation down to its reserve. This is a deliberate trade: taskbar icons are the elements OCR cannot see, whereas foreground controls almost always carry readable text.
- **The element cap can hide a relevant item.** In one capture, `cap_elements` retained 80 of 106 elements and a file plainly visible in the folder was not among them, because the taskbar contributed roughly twenty buttons ranked above the foreground window. The same screen captured again after clearing the desktop did include it. This is recorded rather than fixed, because a fix aimed at an unreproduced failure is a guess.
- **Duplicate entries occur** where a UIA control's box is much wider than the text inside it. The taskbar Search button and the word "Search" survive as two elements because their IoU is approximately 0.1, below the 0.5 deduplication threshold. This produces a redundant line in the list, not a wrong circle.
- **`Maximize` and `Restore` both report as on-screen** and both survive, though only one is visible. The circle is correct either way; the word in the instruction may not be.
- **Verbose system-tray names reach the instruction unshortened,** for example "Volume Speakers (Realtek(R) Audio): 12%".

### Scene B

- **It drives the local demo site only.** No live ticketing site was ever targeted.
- **"PIN code" is handed over to her,** because in India it denotes a postal code and the alternative risks typing into a UPI PIN box.
- **Circle placement assumes a default Chromium layout and 100 percent page zoom.** A bookmarks bar, a bottom bar or a sidebar shifts the ring by its height; the 20 pixel padding absorbs some of that, not all.

### Scene A

- **Downloads only, one level deep.**
- **Teach notes match on shared words, not meaning.** A note reading "She uses Microsoft Edge, not Chrome" does not reach the goal "which browser do I use", because the note never contains the word "browser". The measured alternative was worse: whole-string fuzzy scoring gives 48 for the correct note and 44 for an unrelated one, so no threshold separates them.

### Evaluation

- **The dataset covers one application, not five.** `eval/labels.jsonl` contains 20 labelled rows across 3 File Explorer screens. The specification called for roughly 40 rows across 4 or 5 applications. Every figure produced by the harness is therefore a figure about File Explorer, and the per-application table exists precisely so that an average cannot conceal this.
- **Collection stopped for a mechanical reason, not a conceptual one.** The collector requires an idle desktop: it launches an application, waits for its window to reach the foreground, and discards the capture if anything else intervened. On a machine in concurrent use, focus is taken back between the check and the grab. Four of the five applications were lost to this.
- **Labels were written by the agent that built the harness, not reviewed by a second person.** A label is a judgement about what she would want clicked. Rows where two targets are defensible list both ids rather than selecting one.
- **No live numbers exist.** Task completion rate and recovery rate require a person in front of a changing screen. `eval/live_runs.md` is empty.
- **No vision-coordinate baseline number exists.** The single attempt did not return within 180 s.
- **No tuned-model number exists.** The Tinker run has not been performed. Two blockers: only File Explorer is labelled, so there is no held-out *application* to test against, and 20 rows produce a 6-example test split in which one example is worth 17 percentage points. Cost is not the blocker, estimated at roughly 0.12 US dollars for three epochs.

### Privacy posture

- Captures occur only after the hotkey or during an active task, never continuously.
- Screenshots are held in memory and discarded after each step.
- Telemetry is off by default and carries numbers and closed-vocabulary enums only.
- The application functions with networking disabled.
