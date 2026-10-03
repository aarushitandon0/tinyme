# Speed on real hardware

Produced by `scripts/bench_gemma.py` (Gemma) and `scripts/calibrate.py` plus a
one-off probe (capture and OCR). Every number in the README and the post comes
from this file, never from memory. Nothing here is estimated.

Machine: this laptop, Windows 11 (10.0.26200), 1920x1080 physical at **125%**
scaling (devicePixelRatio 1.2500, 1536x864 logical). Python 3.11.9.

---

## Gemma: NOT MEASURED YET

Ollama is **not installed** on this machine as of Fri 2 Oct 2026, so there is no
Gemma number. `scripts/bench_gemma.py` is written, its prompt builder and
report formatter are verified, and it exits cleanly with "Cannot reach the
Ollama server". To fill this in:

```
winget install Ollama.Ollama        # or the installer from ollama.com/download
ollama pull gemma4:e2b
ollama pull gemma4:e4b
python scripts\bench_gemma.py
```

That appends a row per model with median/p90 seconds, prompt and output token
counts, and how many of the 10 outputs validated against `app.brain.StepPlan`.

Prompt shape it measures, fixed so runs are comparable: 60 elements in the
MASTERSPEC 5.2 format, a goal, 2 history steps, **3,290 characters** total,
structured output against the real `StepPlan` schema, `temperature 0`,
`keep_alive 10m`, 1 untimed warm-up then 10 timed calls.

Thinking text: the installed client is **ollama 0.6.3**, whose `chat()` takes a
`think` parameter, so it is disabled with `think=False`. If a model or server
build rejects that parameter the script retries without it and records
`thinking off = no` in the table, so a number is never quietly measured under
different settings.

---

## Capture and OCR: measured, and this is the bottleneck

RapidOCR 3.9.2 on onnxruntime 1.30.0, CPU, PP-OCRv6 small det + rec models,
full 1920x1080 primary monitor.

| stage | measurement |
|---|---|
| `mss` primary-monitor grab | ~15-30 ms |
| RapidOCR engine first load | 647 ms (once per process) |
| **OCR, warm, full screen** | **9.6 s / 11.5 s / 12.3 s / 12.4 s / 16.1 s / 16.7 s** |
| OCR on a 1/2-downscaled image (960x540) | 18.9 s - **not faster** |
| elements found | 69-156 depending on what was on screen |

Six warm full-screen runs on a busy desktop. The spread is wide and drifts
upward over consecutive runs, which points at CPU contention or thermal
throttling rather than image size; the half-size run came last and was the
slowest of all, so downscaling is not the fix it looks like.

**What this means for the plan.** MASTERSPEC 10 guessed "OCR is 60% of step
time, not the model". On this machine it is more like 90%, and ~10 s of OCR
alone sits against the 15 s/step budget in PROJECTPLAN's Friday checkpoint
before Gemma has been asked anything. Options, cheapest first, none of them
done yet:

1. OCR only the foreground window rect plus the taskbar rect, not the whole
   screen. Both rects are already available from `capture.foreground_rect()`
   and `capture.taskbar_rect()`.
2. Lean on UIA (P5) for most elements and treat OCR as the fallback. UIA has a
   300 ms budget, two orders of magnitude cheaper.
3. Keep OCR off the per-step path entirely where MASTERSPEC 5.4 already allows
   it: window class and title checks and region diff need no OCR, so only
   `text_appears` / `text_disappears` should pay for it.
4. Try a smaller detection input size via RapidOCR `params`.

This needs a decision before P6, because a 1 Hz watcher poll cannot run a
10 s OCR pass.

---

## UI Automation (P5): measured, and it is cheap

`uiautomation` 2.0.29, taskbar (`Shell_TrayWnd`) + foreground window, depth
limits 7 / 6, 300 ms budget. Same laptop as the OCR runs above.

Measured **twice**: once on a quiet desktop, and again after the P5 review while
the machine was loaded (the same run recorded OCR at 22 s, against 9.6-16.7 s
above). Both sets are below, because the spread is the finding. The quiet column
is the code **before** the review fixes and the loaded column is **after**, so the
two columns differ by machine state *and* by code; the element counts are not
comparable across them (the fixes also drop taskbar text labels).

| stage | quiet | loaded (post-review) |
|---|---|---|
| first COM call in a process (`warm_up`, taskbar only) | 232 ms | 374-381 ms |
| warm `uia.collect`, taskbar + foreground | 74 / 88 / 107 / 118 / 183 ms | 301 / 301 / 301 / 302 / 305 / 305 ms |
| elements kept | 29 (25 in the taskbar, 18 of them buttons) | 11-27 |
| full taskbar walk, budget removed | 85 ms, 30 named controls | 206 / 247 / 260 / 288 ms, 26 named |
| `collect` in a fresh thread (what a QThread step does) | 220-248 ms | 303-304 ms |

So UIA costs **two orders of magnitude less than the OCR pass** either way, which
makes option 2 in the OCR section above (lean on UIA, treat OCR as the fallback)
the one to take.

**The budget is the binding constraint under load, and it holds.** Loaded,
`collect` pins at 301-305 ms against a 300 ms budget: the overshoot is the cost of
the one control being read when the deadline passes, ~5 ms. Which walk gets cut
depends on load, and the logs now name it (`UIA <root> walk hit its deadline
after N controls`): in one loaded run the taskbar finished inside its share and
the *foreground* walk was cut after 33 controls; in others the taskbar itself ran
out. On the quiet desktop nothing was cut.

Two things cost real time and are worth knowing:

* **COM's first call** costs 232-381 ms, which alone would blow the budget on her
  first step. `main.TinyMe.start` and `scripts/dump_elements.py` both call
  `uia.warm_up()` first. The fresh-thread numbers show the expensive automation
  client is per-process, not per-thread, so a new QThread per step is fine.
* **Each property read is a cross-process COM call**, so `_snapshot` reads `Name`
  first and the control type second, and drops unnamed controls and containers
  before paying for the remaining three reads. Effect, measured rather than
  reasoned about: the File Explorer taskbar icon moved from the **17th** control
  the walk yielded to the **11th**, because unnamed `ImageControl` children and
  named `Pane`/`Group` containers no longer take slots ahead of it. Earlier in the
  walk is the whole point - the budget cuts from the end.

I am not quoting a per-COM-read cost. An earlier draft of this section inferred
"~15 ms per control" from one timing that included the cold first
`ControlFromHandle`; re-measurement put it near 0.5 ms per read, so the inference
was wrong and is gone. The walk timings above are what was actually measured.

P5 acceptance, from `python scripts\dump_elements.py --timings`, loaded run after
the review fixes:

```
# ocr 54 + uia 11 -> merged 65, kept 65 (limit 80)
56 | "File Explorer" | uia:Button | bottom-center
# capture 132 ms | ocr 22227 ms | uia 308 ms
```

On the quiet run before the fixes the same line was `62 | "File Explorer" |
uia:Button | bottom-center` with `# ocr 146 + uia 29 -> merged 172, kept 80
(limit 80)`. The icon survives in every run measured, quiet or loaded, which is
what scene A's first step needs.

Known gaps, stated plainly:

* Under load the foreground window gets little or nothing, because the taskbar is
  walked first and may spend the budget down to the 80 ms reserve. That is the
  deliberate trade: the taskbar icons are the elements OCR *cannot* see, while
  foreground controls almost always carry text OCR can read.
* The taskbar Search button and the word "Search" OCR reads inside it survive as
  two elements: the button's box is much wider than the text, so IoU is ~0.1,
  under the 0.5 dedupe threshold. A duplicate line in the list, not a wrong
  circle.
* `Maximize` and `Restore` both report `IsOffscreen=False` and both survive,
  though only one is visible. A UIA quirk; the circle is right either way, the
  word in the instruction may not be.
* Verbose tray names reach the instruction unshortened ("Volume Speakers
  (Realtek(R) Audio): 12%"). Not fixed; it would want a trim in `clean_name`.

## region_changed threshold (P6): calibrated

`config.REGION_DIFF_THRESHOLD = 8.0`, a mean absolute pixel difference on a
0-255 scale over the circled bbox padded by `REGION_PAD_PX = 20`.

| measurement | value |
|---|---|
| noise floor: same region, 5 consecutive captures, 3 regions, nothing touched | **0.000 every time** (15 of 15) |
| one region against different screen content, for scale | 24.2 / 37.6 / 41.6 |
| all-black against all-white, the arithmetic maximum | 255.0 |

The noise floor is exactly zero: a desktop screenshot has no sensor noise, so any
non-zero difference is a real pixel change. The threshold therefore is not about
rejecting noise but about ignoring changes too small to mean anything. A text
caret blinking in a 240x80 padded region is 2x16 px, 0.17% of the area, so at most
~0.4 mean difference (arithmetic from the measured geometry, not a separate
measurement); a clock digit is of the same order. 8.0 clears those by 20x and sits
3x below the smallest content change measured.

Measured with `scripts/calibrate.py`-style probing on the same 1920x1080 at 125%
desktop as the rows above.

## Overlay capture exclusion: verified working

`SetWindowDisplayAffinity(hwnd, 0x11)` succeeds, `GetWindowDisplayAffinity`
reads back `0x11`, and the probe string never appears in OCR of a screenshot
taken while the overlay is displayed. Checked at four settle delays (0, 0.15,
0.5, 1.0 s), leaked = False every time. So the **fast path is active** and the
hide->capture->show fallback is not needed on this machine.

Worth recording: the first version of that verification reported a false
positive, because it probed with the words "TINYME OVERLAY CHECK" and accepted
`fuzz.partial_ratio >= 85`, which ordinary screen text clears. It would have
silently forced the slow path forever. The probe is now a distinctive token and
the matcher is covered by `tests/test_overlay.py`.

## DPI: exact at 125%

`mss` width / Qt logical width = 1.2500, equal to `devicePixelRatio` to four
decimal places, so `elements.to_logical` is converting correctly. Still needs
the eyeball check at 100% - see README / the testing steps in the P3 handover.

## Gemma: measured, Fri 3 Oct 2026

Ollama is now installed (`gemma4:e2b` 4.6 GB, `gemma4:e4b` 6.6 GB). These come
from a probe script, not `bench_gemma.py`, so the prompt is a **real** 80-element
screen rather than the script's fixed 60-element one. Same machine as every row
above.

| measurement | value |
|---|---|
| cold model load + 1 token, `gemma4:e2b` | **31.8 s** |
| warm call + 1 token | **0.9 s** |
| warm `plan_step`, real 80-element screen, quiet-ish machine | **26.5 s** |
| warm `plan_step`, same, machine loaded by a concurrent OCR benchmark | **50.5 s** |

### The cold-start defect this exposed, and the fix

First `plan_step` of a session on a cold server measured **60.8 s** and came back
`fell_back=True`: load (31.8 s) plus inference (~26 s) overran
`config.MODEL_TIMEOUT_S = 60.0`, httpx raised `ReadTimeout`, and `plan_step`
turned it into the `cannot_see_it` hint it is supposed to. So her **first** step
of every session was a generic hint instead of a circle, and every step after it
was fine. That is why it was invisible in the unit tests, which mock the client.

Fixed by `brain.warm_up()`, called from `TinyMe.start` on a daemon thread
(`main.TinyMe._warm_model`) -- the same move `uia.warm_up` already makes for
COM, two orders of magnitude bigger, hence the thread: 31.8 s on the GUI thread
would freeze the prompt window. Re-measured after the fix, cold server, worst
case with the machine still loaded:

```
[warm_up ] finished in 22.0s
[step 1  ] 50.5s  fell_back=False  retried=False
           instruction: 'Click on the File menu.'
           target_id=9  check=text_appears='File menu'
           resolves to: 'File' (ocr:text)
```

**Residual, not fixed.** If she types a goal within a few seconds of launch, her
step queues behind the still-running warm-up and pays load + inference anyway.
Ollama serialises the two, so nothing breaks, but the 60 s ceiling can still be
hit in that narrow window. Left alone rather than widened: `MODEL_TIMEOUT_S` is
a product judgement about how long she should stare at a spinner, and changing
it to paper over a 4-second race is the wrong trade to make silently.

## OCR tuning: attempted, and it does not work

Testing bench.md's own option 4 ("try a smaller detection input size"). Five
RapidOCR configurations, same 1920x1080 screen, 1 warm-up + 3 timed runs each:

| configuration | min | median | texts |
|---|---|---|---|
| defaults | 12.88 s | 14.56 s | 140 |
| `Global.use_cls=False` | 13.15 s | 38.70 s | 141 |
| `use_cls=False` + `Det.limit_type=max`, 960 | 30.31 s | 31.45 s | 141 |
| `use_cls=False` + `Det.limit_type=max`, 736 | 14.15 s | 15.71 s | 141 |
| the above at 960 + `Rec.rec_batch_num=16` | 18.96 s | 22.91 s | 141 |

**No configuration beats the default**, and the within-configuration spread
(13 s to 39 s for the *same* settings) is larger than any between-configuration
difference. So the knobs are not the problem and tuning them further is wasted
effort. The spread is the finding, and it matches the drift already recorded
above: this is CPU contention on the recognition pass, not image size.

Two measurements that explain *why* downscaling never helped:

* Full screen, 1920x1080: **15.4 s**, 62 boxes.
* Taskbar strip, 1920x60, i.e. **3%** of the pixels: **4.8 s**, 10 boxes.

Cost tracks the number of detected text boxes (~0.25-0.5 s each) plus a large
fixed overhead, not the pixel count. And the default `Det.limit_type: min` with
`limit_side_len: 736` scales the image so its **short** side becomes 736, which
for a thin strip is an *upscale*: the 1920x60 taskbar crop is inflated to roughly
23552x736 before detection runs. That is why the half-scale run recorded earlier
came out slower, and it means **option 1 (crop to the taskbar rect) is actively
pathological** under the stock settings, not merely unhelpful.

That leaves option 2 (lean on UIA, OCR as fallback) and option 3 (keep OCR off
the per-step path except for the two text checks) as the only ones still open.
Both are architecture changes, not settings, and neither has been made.
