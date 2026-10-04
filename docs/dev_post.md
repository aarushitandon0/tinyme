*This is a submission for the [Hacktoberfest Weekend Challenge: Build for a Friend](https://dev.to/challenges/hacktoberfest-weekend-2026-10-01)*

## What I Built

Tiny Me is a Windows desktop helper built for one non-technical person. She presses a hotkey and types what she wants in plain words, for example "find my train ticket for tomorrow" or "search for trains." Tiny Me then draws a circle around the exact button or box to click next, and waits until she clicks it. When the next step moves on, it draws the next circle. The goal is that she learns the steps herself, not that the computer quietly does them for her.

It also knows when to step back. When the screen asks for a password, a one-time code, a card number or a payment, Tiny Me stops, says "This part is yours. I'll wait," and does nothing until she has finished that part herself. It never types into or clicks those fields.

Everything runs on her laptop. Reading the screen, finding the controls and the language model are all local. No screen content leaves the machine, and the app works with the network turned off.

**Who it is for.** My friend [[TODO: confirm how you describe your friend, and whether she is happy to be named or described]]. [[TODO: the 9 p.m. phone call, in 3 to 5 concrete sentences, using only what actually happened]]

## Demo

[[TODO: paste the screen recording link here, for example {% embed https://youtu.be/XXXX %}]]

The recording shows the full flow:

1. Press Ctrl+Alt+H and type a goal.
2. Tiny Me circles the next control and explains it in one line.
3. She clicks, and the circle moves to the next control.
4. On the sign-in page, Tiny Me stops and hands control back.
5. Ctrl+Alt+P stops everything at any time.

![Demo site: finding trains](https://raw.githubusercontent.com/TODO/tiny-me-kit/main/eval/screenshots/demo_site_01.png)

*The circle lands on the search box. Captions are one line each, as in the tutorial screenshots.*

![Demo site: sign-in handover](https://raw.githubusercontent.com/TODO/tiny-me-kit/main/eval/screenshots/demo_site_03.png)

*The sign-in page. Tiny Me stops here.*

## Code

Repository: [[TODO: GitHub repository URL]]

{% embed https://github.com/TODO/tiny-me-kit %}

### How to download and run it

Tiny Me runs on Windows 10 (version 2004 or later) or Windows 11, with Python 3.11 and Ollama.

```
git clone https://github.com/TODO/tiny-me-kit
cd tiny-me-kit
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
ollama pull gemma4:e2b
```

To run the demo, open two terminals in the repository folder.

In the first terminal, serve the demo site:

```
python -m http.server 8000
```

In the second terminal, start the app:

```
python -m app.main
```

The app starts with no visible window. Press Ctrl+Alt+H to open the prompt, type a goal, and choose "Show me how" or "Do it for me." Press Ctrl+Alt+P at any time to stop. The demo flow targets `http://localhost:8000/demo_site/index.html`.

Optional settings go in a `.env` file at the repository root. Real environment variables override it. The main ones are `TINYME_MODEL` (the Ollama model tag), `TINYME_LANGUAGE`, and `TINYME_TELEMETRY` (off unless set to `1`).

## How I Built It

**Open-weight models, all local.** The language model is Gemma 4 (`gemma4:e2b`) served by Ollama on the laptop. Text on screen is read by RapidOCR on the ONNX Runtime CPU provider, and controls are read through Windows UI Automation. Nothing calls a hosted API in the runtime path.

**The model picks a number, never pixels.** This is the key design decision. The app builds a numbered list of the controls on screen and asks the model for one number. The model never produces coordinates. A language model asked for pixel positions gives plausible numbers that are often wrong. A number from a list the app built can be checked: if it is not in the list, the answer is rejected.

**Success is checked in code.** After each step, a watcher polls the screen once a second and checks whether the step worked, using a window title or class, a piece of text, or a change in pixels. The model proposes which check to run, but Python runs it. A model asked "did that work?" tends to agree with itself.

**A fine-tuning track.** I also tried to fine-tune a small open model on Tinker to pick the right number. I compared it with the untuned base model on the same 22 demo-site rows. See the results in the next section.

## Why Does Open Innovation Matter?

Tiny Me runs on her laptop with open-weight models. Her screen never leaves the machine, there is no per-use bill, and nobody can change the terms or shut the service down.

Open weights also meant I could change the brain. I could swap the model, read the code that decides when to hand control back, and test the fine-tuning idea myself. A closed API would have made the privacy promise a matter of trust. Here the promise is in code I can point to: the guard runs before every model call, and the model has no way to type into a password box, because the app never gives it one.

Open-weight models are also slower on a laptop CPU, and I say so below. That is the trade: a slower answer that stays private, against a faster answer that leaves the machine.

## My Agent Session

[[TODO: Entire or DevRelay session links, one line each on the decision the session shows, for example {% agent_session ID planning %}]]

## Numbers, Including the Bad Ones

All numbers below come from files in the repository. Nothing is estimated.

**Speed on a Windows 11 laptop at 125% display scaling** (from `notes/bench.md`):

| Stage | Measurement |
|---|---|
| Screen capture | about 15 to 30 ms |
| OCR on the full screen (warm) | 9.6 s to 16.7 s across six runs |
| UI Automation, quiet desktop | 74 ms to 118 ms |
| Model call, warm, on a real 80-element screen | 26.5 s on a quiet machine, 50.5 s with the machine under load |

OCR is the bottleneck, not the model. On this laptop it takes most of the time in a step. Downscaling the image did not help, and neither did five OCR configurations I tried. The one change I kept is to lean on UI Automation for most controls, which is about two orders of magnitude cheaper.

**The cold-start bug.** The first step of each session timed out at 60.8 seconds, because loading the model took 31.8 seconds and the call itself took about 26 seconds. The app then showed a generic hint instead of a circle. The unit tests missed this because they mock the model. The fix warms the model up in the background when the app starts. After the fix, the first step finished in 50.5 seconds with the correct circle. One gap remains: if a goal is typed in the first few seconds after launch, the step can still wait behind the warm-up.

**Picking the right control on the demo site** (from `eval/results_tinker.md`, 22 rows):

| System | Correct element | Valid answer format |
|---|---|---|
| Untuned base model (Qwen3.5-4B, on Tinker) | 0 of 22 (0%) | 0 of 22 |
| Tuned model (same base, fine-tuned on Tinker) | 3 of 22 (14%) | 22 of 22 |

The tuning fixed the answer format but not the choice of control. The tuned model still picked the wrong control in 19 of 22 rows. Three examples from the table: for the goal "pay for my ticket," it picked a heading when the right answer was "not on this screen." This is a failure, and the post should say so.

[[TODO: the Gemma 4 picking-accuracy result on the same 22 rows. `eval/results.md` is not in the repository yet. Run `python eval\run_eval.py --systems all` and paste the correct-element rate for `gemma4:e2b` and `gemma4:e4b` here, with the counts.]]

**Live task completion and recovery.** [[TODO: this is not measured yet. The live log in `eval/live_runs.md` has no rows. Fill in the completion and recovery counts only after the live runs are logged, including the failed attempts.]]

**The first user session.** [[TODO: the session with my friend has not happened yet. When it has, add the three tasks, the time for each, the exact points where she stopped, and her exact words from `notes/user_test.md`, complaints included. Do not paraphrase her.]]

## Safety and Privacy

- Tiny Me captures the screen only when she presses the hotkey.
- The guard runs before every model call. On a password, one-time code or payment screen, nothing is sent to the model and nothing is clicked or typed.
- In the browser, the guard reads the page structure rather than guessing from pixels. A password field is recognised as a password field whatever its label says.
- Screenshots exist only in memory. Nothing in the app writes an image to disk.
- Telemetry is off unless a developer sets `TINYME_TELEMETRY=1`. Even then, only numbers and fixed categories are sent. Never screen text, goals, file names or window titles.

## Limitations and What's Next

- **It only drives my demo site.** The booking flow runs against a five-page static site that I wrote. Real ticketing sites forbid automation in their terms of use and use CAPTCHAs. This project does not claim it works on any real site.
- **The circle assumes a standard browser layout at 100% page zoom.** A different layout moves the circle by the height of whatever was added. It is verified only for a default Chromium window on my laptop.
- **"PIN code" is treated as private.** In India a PIN code is a postal code. So on an address form, Tiny Me hands over a field she could have typed herself. I kept this on purpose, because the alternative risks typing into a UPI PIN box.
- **Do-it-for-me is limited.** Only three task types can run automatically: find a file, open a folder, and browse and fill. Anything else falls back to guide mode.
- **Only Windows 10 and 11, one display, primary monitor.** Tested at 125% scaling. 100% scaling is not yet verified.
- **The tuned model is not better yet.** It fixed the answer format but not the choice of control. The next step is more training data for the choice itself, not more tuning of the format.
- **Next:** run the first user session, fix the stuck points it reveals, and re-run the evaluation to see which numbers move.

#devchallenge #weekendchallenge #hf26challenge
