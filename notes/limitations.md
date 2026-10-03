# Known limitations

The honest list, kept as things are found rather than written from memory at
the end. README "Limitations" (P14) is written from this file. Nothing here is
a guess: each line says how it was found.

---

## Scene B: booking (P9)

**It only drives our own demo site.** `demo_site/` is a static five-page stand-in
served by `python -m http.server`. Real Indian ticketing sites forbid
automation in their terms and use CAPTCHAs (MASTERSPEC 3B), so nothing was ever
pointed at one. The handover logic reads the DOM and is not site-specific, but
"it would work on IRCTC" is a claim this repo has not earned and must not make.

**"PIN code" is handed over to her.** The DOM guard treats a field whose label
or name contains `pin` as hers, and in India "PIN code" means the postal code.
So on an address form she types her own postcode. Deliberate: the alternative
is dropping `pin` from the keyword list, which would let Tiny Me type into a
UPI PIN box. Documented in `app/guard.py` next to the keyword list.

**The circle's position assumes one browser layout and 100% page zoom.** A
field's place on screen is computed from `getBoundingClientRect` plus the
window origin plus `outerHeight - innerHeight` for the browser's chrome. That
last part attributes the whole difference to the top of the window, which is
right for Chromium with no bookmarks bar, no bottom bar and no sidebar. A
different layout shifts the ring by the height of whatever was added; the 20 px
of padding around the circle absorbs a little of it, not all. Verified correct
for a default headed Chromium on this laptop: the computed rectangle was
cropped out of a real screenshot and OCR read "Your password" inside it and
"Password" just above it.

**Not `window.devicePixelRatio`.** Worth recording because it looked right for
an hour: Playwright launches Chromium with `deviceScaleFactor` pinned to 1, so
the page reports a ratio of 1 on a 125% display. Scaling the field's rectangle
by the page's own number put the circle one field too high -- a tidy ring
around the *username* box on the login page. The scale now comes from the
screen (`capture.screen_scale`, or Qt's `devicePixelRatio` when the app is
running), and `tests/test_actions.py` has a regression test that hands in two
scales and requires the answer to follow them.

**The resume condition is "her field is gone", not "she typed something".**
Waiting for the box to be non-empty does not work: the password box is still
there, still hers, so the guard finds it again on the next pass and hands over
in a loop. Waiting for the field to disappear means she presses "Sign in"
herself, which is correct anyway -- signing in is not on the permission table.
It also means Tiny Me never asks the page what she typed.

**A handover has a 5-minute ceiling.** If she walks away, the flow stops rather
than waiting on her form indefinitely. Her half-filled booking is left exactly
as it is and the browser stays open.

---

## Scene A: find it for me (P8)

**Downloads only, and not recursively.** "Find it for me" looks in the real
Downloads folder (resolved through the Known Folder API, because hers has been
moved) and nowhere else, one level deep. Downloads folders tend to contain an
"old stuff" folder, and walking into it turns a 50 ms answer into a disk crawl.

**A filename match is attempted, then abandoned.** If her words do not match
any filename at ≥ 72 (rapidfuzz WRatio), the newest file is offered and the
message says so ("This is your newest download: ..."). She can see that is an
answer to a different question. A confident circle around an unrelated file
would not be visible as a mistake.

**Teach notes match on shared words, not meaning.** `notes_for_goal` sends the
notes that mention a word she mentioned. So a note saying "She uses Microsoft
Edge, not Chrome" does not reach the goal "which browser do I use", because the
note never says "browser". Measured alternative was worse: on the shipped notes
file a whole-string fuzzy score gives 48 for the right note about photos and 44
for an unrelated one, so no threshold separates them.

---

## Measured, not estimated

* Known Folder lookup: 5.6 ms. `find_file` over the real Downloads folder:
  50 ms. Explorer opened with the file selected: 30 ms end to end.
* Full booking flow on the served demo site, headed, with a stand-in for her
  typing: 11.8 s to fill the route and date, choose the first train, and stop
  at the password, the OTP and the card in turn.
* Gemma: still not measured. See `notes/bench.md` -- Ollama is not installed on
  this machine.
