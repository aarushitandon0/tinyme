# Live runs — task completion and recovery

Hand-written, unlike `results.md`. These are the two numbers the frozen-screen
eval cannot produce, because both need the whole app running against a real
screen that changes while it watches:

* **task completion** — she (or I) states a goal and the app guides to the end
  without a human explaining a step.
* **recovery** — after a *deliberate* wrong click, the app re-plans from the new
  screen instead of repeating the old step.

Rules, so these stay comparable with `results.md`:

- One row per attempt, including the ones that failed. An attempt deleted because
  it went badly makes every number here a lie.
- Record the model tag, because `e2b` and `e4b` are different systems.
- "Steps" counts model calls, not clicks.
- Timing is wall clock from pressing Ctrl+Alt+H to the goal being reached,
  **including** her own thinking time, because that is the thing she experiences.
  Note separately if she was interrupted.
- `notes/user_test.md` holds what she said. This file holds only what happened.

---

## Format

| date | goal | model | steps | completed | recovered | wall clock | what happened |
|---|---|---|---|---|---|---|---|
| | | | | yes/no | n/a or yes/no | | one line |

---

## Runs

_Not yet collected. The task loop has not been run end to end against a live
screen with this logged, so there is no completion or recovery number anywhere in
this repo. Do not quote one in the post until there are rows here._

---

## Deliberate-wrong-click protocol

So "recovery" means the same thing each time:

1. Start a task and let it circle step 1.
2. Instead of clicking the circle, click something that changes the foreground
   window (open a different app from the taskbar).
3. Correct behaviour: the watcher notices the screen changed in a way its success
   check did not ask for, and the next plan is made from the *new* screen. It must
   not re-draw the original circle.
4. Record `recovered = yes` only if the next instruction makes sense on the new
   screen. "It did not crash" is not recovery.
