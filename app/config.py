"""One place for the knobs (MASTERSPEC 11).

Values here are read at import time. Environment variables let the developer
change them without editing code, which matters for the two that differ between
her laptop and mine: the model tag and telemetry.

CLAUDE.md rule 7: telemetry is off unless ``TINYME_TELEMETRY=1``. That default
lives here and nowhere else.
"""

from __future__ import annotations

import os

# --- model -----------------------------------------------------------------

#: Ollama tag for the brain. ``e2b`` is the smaller edge size; notes/bench.md
#: records what each one costs per step on real hardware.
MODEL = os.environ.get("TINYME_MODEL", "gemma4:e2b")

#: Keep the model resident between steps (MASTERSPEC 14). A cold load costs
#: seconds that she would wait through on every single step.
KEEP_ALIVE = "10m"

#: Hard ceiling on one model call. Past this she is better served by a hint
#: than by a spinner.
MODEL_TIMEOUT_S = 60.0

#: The language Gemma writes ``instruction`` in (MASTERSPEC 7, Tier 2). Plain
#: English name of the language, dropped straight into the system prompt.
LANGUAGE = os.environ.get("TINYME_LANGUAGE", "English")

# --- hotkeys ---------------------------------------------------------------
# pynput GlobalHotKeys syntax (MASTERSPEC 5.7).

HOTKEY_OPEN = "<ctrl>+<alt>+h"
HOTKEY_PAUSE = "<ctrl>+<alt>+p"

# --- task loop -------------------------------------------------------------

#: MASTERSPEC 5.5: stop after this many steps rather than looping forever.
MAX_STEPS = 12

#: MASTERSPEC 5.5: how often the watcher checks whether the step worked.
POLL_INTERVAL_MS = 1000

#: MASTERSPEC 5.5: no change for this long and we rephrase once.
STEP_HINT_AFTER_S = 20.0

#: MASTERSPEC 6: while the guard is holding a private screen back, we re-read
#: the screen this often to see whether she has moved on. Slower than the normal
#: poll on purpose: each paused poll costs a capture plus OCR, and she is typing
#: a password, not waiting on us.
PAUSED_POLL_INTERVAL_MS = 2500

#: MASTERSPEC 5.4 region_changed: mean absolute pixel difference, 0-255, above
#: which the circled area counts as having changed. Calibrated in
#: notes/bench.md, not guessed.
REGION_DIFF_THRESHOLD = 8.0

#: Padding around the circled bbox when watching it, so a change just outside
#: the text (a highlight, a menu opening under it) still registers.
REGION_PAD_PX = 20

# --- telemetry -------------------------------------------------------------

#: CLAUDE.md rule 7. Off unless explicitly switched on, never on her laptop.
TELEMETRY_ENABLED = os.environ.get("TINYME_TELEMETRY") == "1"
