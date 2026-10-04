*This is a submission for the [Hacktoberfest Weekend Challenge: Build for a Friend](https://dev.to/challenges/hacktoberfest-weekend-2026-10-01)*

## What I Built

I have a lot of smart friends. They're researchers, designers, writers and artists, and they're very good at what they do.

Every so often one of them messages me: "Hey, how do I do this on my laptop?" My mom does the same thing. How do I find this file? Where did my download go? Which button do I press? Why is this not working?

They aren't unintelligent. They're just not computer people. They know what they want to accomplish, but unfamiliar folders, settings, buttons and pop-ups are frustrating, and I'm usually the computer person they call.

So: what if a small version of me could live on their laptop?

That's **Tiny Me**. It's a local desktop helper for non-technical users. You press a hotkey and say what you're trying to do in plain words, like "Find my train ticket for tomorrow" or "Show me how to search for trains." Tiny Me looks at the screen, works out the next control, and draws a circle around exactly where to click. You click it. It checks that the step worked, then circles the next one.

It's like having a tech-savvy friend sitting next to you saying "Yep. Click this." Except that friend lives on your laptop.

**It is designed to teach, not to take over.** The goal is "let AI make your computer understandable," not "let AI use your computer." The user stays in control.

It also knows when to stop. When the screen reaches a password, one-time code, card number or payment, Tiny Me says "This part is yours. I'll wait." It doesn't type, doesn't click, and doesn't try to get around the boundary.

**Who it is for.** My mom, and friends who are brilliant at their jobs but get stuck on their laptops. [[TODO: Add the concrete moment you watched someone get stuck, in 3 to 5 sentences, using only what actually happened. Only if you have it.]]

Everything runs on the user's Windows machine. Reading the screen, finding controls and the language model are all local. No screen content leaves the laptop.

## Demo

[[TODO: Upload `SS/07_demo_walkthrough.mp4` (currently untracked in git) and paste the link, e.g. {% embed https://youtu.be/XXXX %}]]

The demo runs against a local booking website included in the repo (`demo_site/`), and shows:

1. Press `Ctrl+Alt+H` and type a goal.
2. Tiny Me circles the next control and explains the step in one line.
3. The user clicks it.
4. Tiny Me checks whether the step worked.
5. The circle moves to the next control.
6. At a sign-in page, Tiny Me stops and hands control back.
7. `Ctrl+Alt+P` stops it at any time.

![TinyMe home screen](https://raw.githubusercontent.com/aarushitandon0/tinyme/main/SS/01_home.png)

*Press the hotkey, then describe what you're trying to do in plain language.*

![TinyMe guiding the user step by step](https://raw.githubusercontent.com/aarushitandon0/tinyme/main/SS/02_guiding_steps.png)

*Tiny Me circles the control and waits for the user to click it.*

![TinyMe handing control back at a sensitive step](https://raw.githubusercontent.com/aarushitandon0/tinyme/main/SS/03_safety_stop.png)

*At a sensitive step like sign-in, Tiny Me stops instead of interacting.*

![TinyMe example goals](https://raw.githubusercontent.com/aarushitandon0/tinyme/main/SS/04_examples.png)

*Example goals.*

![TinyMe helping find a file](https://raw.githubusercontent.com/aarushitandon0/tinyme/main/SS/05_find_the_file.png)

*Finding a downloaded file, one circled click at a time.*

All screenshots are in the repo's [SS folder](https://github.com/aarushitandon0/tinyme/tree/main/SS).

## Code

{% embed https://github.com/aarushitandon0/tinyme %}

**Repository:** [github.com/aarushitandon0/tinyme](https://github.com/aarushitandon0/tinyme)

### How to Run

Requires Windows 10 (version 2004 or later) or Windows 11, Python 3.11, and [Ollama](https://ollama.com).

```bash
git clone https://github.com/aarushitandon0/tinyme
cd tinyme
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
playwright install chromium
ollama pull gemma4:e2b
```

To run the demo, open two terminals in the repository folder.

Terminal 1:

```bash
python -m http.server 8000
```

Terminal 2:

```bash
python -m app.main
```

TinyMe starts with no visible window.

- `Ctrl+Alt+H` opens the prompt. Type a goal and choose "Show me how" or "Do it for me."
- `Ctrl+Alt+P` stops TinyMe at any time.

The demo targets `http://localhost:8000/demo_site/index.html`.

Optional settings go in a `.env` file at the repo root: `TINYME_MODEL` (Ollama model tag), `TINYME_LANGUAGE`, and `TINYME_TELEMETRY` (off unless set to `1`).

## How I Built It

The core idea: **the language model chooses a control, never a coordinate.**

Tiny Me builds a numbered list of the on-screen controls, and asks the model to pick one number from it. The model answers `17`, not `click(834, 492)`. The app can then check that `17` is a real, interactable control. A model that generates pixel coordinates can produce plausible but wrong ones. A number from a list the app built can be validated.

**The model proposes. The app decides.**

**Success is checked in code.** After each step, a watcher polls the screen about once per second and checks window titles or classes, visible text, pixel changes, or browser structure. The model can suggest which check to run, but Python runs it. I deliberately don't ask the model "did that work?" A model judging its own action can easily talk itself into success.

**The safety guard runs before the model.** Before every model call and every automated action, the guard checks for sensitive screens. If one is present, nothing is sent to the model and nothing is clicked or typed. In the browser, the guard uses page structure, so a password field counts as a password field whatever its label says. Screenshots stay in memory and are never written to disk.

**Everything runs locally:**

- **Gemma 4 (`gemma4:e2b`)** via **Ollama**, for choosing the control
- **RapidOCR** on **ONNX Runtime**, for screen text
- **Windows UI Automation**, for discovering controls
- **Python** for orchestration, guard, validation and execution
- **Playwright** for the browser demo
- **Qwen3.5-4B fine-tuned on Tinker** as a swappable alternative picker (see below)

### The Tinker experiment

I fine-tuned `Qwen/Qwen3.5-4B` with LoRA (rank 16, 3 epochs, 80 training examples, about $0.17) on Tinker. The held-out test was the whole `demo_site` app, which the model never saw in training, with 22 labelled examples.

| System | Correct control | Valid answer format |
|---|---|---|
| Untuned Qwen3.5-4B | 0/22 (0%) | 0/22 (0%) |
| Fine-tuned Qwen3.5-4B | 3/22 (14%) | 22/22 (100%) |

The untuned model's 0/22 is mostly a format failure: its replies didn't parse. Fine-tuning fixed the format but not the choice. In 19 of 22 examples the tuned model still picked the wrong element. For example, asked to "pay for my ticket," it picked a heading when the right answer was "not on this screen."

That's a modest result, and it's a useful failure. It says the next problem is teaching the model when to act, which control to pick, and when no safe action exists, not only following the output format.

## Why Does Open Innovation Matter?

For Tiny Me, local and open models aren't a preference; they're part of the product. If I'm asking my friends and my mom to let software look at their screen, I don't want that to depend on trusting a closed hosted API.

With open weights:

- the model runs on the laptop, and screen content stays there,
- sensitive controls are blocked before any model call,
- the code is inspectable,
- and I could swap the model, fine-tune it, and measure where it fails.

A closed API would have hidden the 0/22 and the 3/22 behind a single "it works" demo. Open weights let me train a model, test it on an app it hadn't seen, and report that it mostly still fails.

## My Agent Session

[[TODO: No Entire session exists in this repo yet (no session links, no `.entire` data). Either record and link the session(s), then add one line per link on the decision it shows, or delete this section and the Entire prize entry below.]]

## Prize Categories

**Best Use of Gemma**
Gemma 4 (`gemma4:e2b`) is the default model, run fully locally through Ollama. It reads the numbered list of on-screen controls and picks one. Nothing in the runtime path is a hosted API.

**Best Use of Tinker**
I fine-tuned Qwen3.5-4B on Tinker and tested it on a whole held-out app. Valid answer format went from 0/22 to 22/22, and correct control selection from 0/22 to 3/22. The gain is small, and the write-up documents where it still fails.

[[TODO: Best Use of Entire: only enter if you have linked sessions in "My Agent Session" above. Otherwise remove this entry.]]

[[TODO: Best Use of Sentry Agent Tracing: only enter once the dashboard and trace screenshots exist in `docs/` and the "what the traces told me" paragraph is written from them. Otherwise remove this entry.]]

## What's Next?

The next step is to put Tiny Me in front of the people I built it for: friends, family, and my mom. I want to find out where they hesitate, which instructions make sense right away, what confuses them, whether they remember the steps next time, where Tiny Me should step back, and most importantly, **after using Tiny Me, do they need it less?**

That's the metric I care about. I don't want to build something that makes people feel they can't use their own computer without it. I want the opposite: a small version of your tech-savvy friend that says "Here. Click this." and, eventually, you don't need to ask.

#devchallenge #weekendchallenge #hf26challenge
