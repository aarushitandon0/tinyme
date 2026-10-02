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
