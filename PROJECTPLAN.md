# Tiny Me — Project Plan

All times IST. Hard deadline **Mon 5 Oct, 12:29 PM**. Aim to submit by **Mon 10:00 AM**.
Prompt numbers (P0, P1, …) refer to `PROMPTS.md`.

---

## Friday 2 Oct (evening → night): foundations

| Block | Task | Prompt | Done when |
|---|---|---|---|
| 0:00–0:30 | Create **new** GitHub repo `tiny-me` (timestamp proves it's in the window). Copy in `CLAUDE.md`, `MASTERSPEC.md`, `PROJECTPLAN.md`, `.claude/skills/`. Install Entire, run `entire enable --agent claude-code`. First commit. | — | `git log` shows first commit; `entire status` OK |
| 0:30–1:00 | Claim credits at hacktoberfest.com/my/promos (Tinker). Make Sentry project, keep DSN in `.env` (gitignored). Update Ollama, `ollama pull gemma4:e2b` and `gemma4:e4b`. | — | Credits visible, models listed |
| 1:00–1:30 | **Speed test on HER laptop** (or the closest machine you have): run `scripts/bench_gemma.py` from P1 with a fake 60-element list. | P1 | Seconds/step for e2b and e4b written in `notes/bench.md` |
| 1:30–3:00 | Scaffold + capture + OCR + elements + overlay with DPI calibration. | P2, P3 | Press hotkey → circles appear on 3 hard-coded OCR words, positioned correctly at 100% and 125% scaling; overlay not visible in captures |
| 3:00–4:00 | Brain: Ollama structured output + validation, wired to the overlay for **one step**. | P4 | Type "open downloads" → correct circle once |

**Friday checkpoint:** one task, one step, end to end, with Gemma choosing. If the speed test says > 15 s/step on e2b, note it now and plan shorter prompts / fewer elements.

---

## Saturday 3 Oct: make it a guide

| Block | Task | Prompt | Done when |
|---|---|---|---|
| Morning (3 h) | UIA elements (taskbar + foreground window), merge/dedupe. | P5 | File Explorer taskbar icon appears as a numbered element |
| Midday (3 h) | Watcher: success checks, "I did it", 20 s hint, re-plan on unexpected change. Task loop. | P6 | Scene A runs 3 steps unaided, including one deliberate wrong click that recovers |
| Afternoon (1.5 h) | Guard: sensitive-screen pause, do-it permission table, pause hotkey, "Tiny Me is looking" indicator. Unit tests. | P7 | Opening a page with "Password" pauses before any model call; tests green |
| Late afternoon (2 h) | Collect ~40 eval screenshots on your own laptop (dummy files only) + label them. | P10 (labeling part) | `eval/labels.jsonl` has ≥ 40 rows across ≥ 4 apps |
| Evening (2 h) | Eval runner: our pipeline (e2b, e4b) vs Gemma-vision-coordinates baseline. | P10 | `eval/results.md` table generated |
| Night (1 h) | **User session #1 with her.** 3 tasks with Tiny Me, 3 the usual way. Time it, write exact quotes in `notes/user_test.md`. Explain what it sees; get her OK. | — | Notes written the same night |

**Saturday cut line:** if Scene A is not working multi-step by 10 PM, stop here. Sunday = fix Scene A + eval + post only. Skip Tier 2 and 3 entirely.

---

## Sunday 4 Oct: Tier 2, partners, demo

| Block | Task | Prompt | Done when |
|---|---|---|---|
| Morning (2 h) | Fix exactly what she got stuck on. Nothing else first. | P11 | Each stuck point has a fix or an honest "known limitation" line |
| Late morning (1.5 h) | "Find it for me" + `explorer /select`. Teach notes. | P8 | File highlighted in Explorer in < 3 s |
| Midday (2.5 h) | Mock booking site + Playwright headed flow + DOM guard handover. | P9 | Fills route/date, stops at password with circle and "this part is yours" |
| Afternoon (1.5 h) | Sentry spans (dev mode only). Run 10 tasks to populate dashboard. Screenshot dashboard + 1 recovery trace. | P12 | Screenshots saved in `docs/` |
| Afternoon (optional, 3 h) | **Tinker** only if everything above works. Build dataset, train, evaluate on held-out app. | P13 | Row added to results table, or decision to skip written down |
| Evening (1.5 h) | Record demo video (Wi-Fi off). Two takes max. | — | 60–90 s video uploaded |
| Night (2 h) | Write the post in one sitting with the `dev-post-writer` skill. README final pass. | P14 | Draft saved on DEV (unpublished) |

---

## Monday 5 Oct: submit only

| Time | Task |
|---|---|
| 8:00–9:30 | Re-read post once. Check tags, links (repo, video, Entire sessions), DEV handles of any teammates. Run `pytest` and the `tinyme-guardrails` skill one last time. |
| 9:30–10:00 | **Publish.** Screenshot the published post with timestamp. |
| After | Any commit after 12:29 PM goes in the README's "Post-deadline commits" section. |

---

## Daily non-negotiables
- Commit at least every hour (Entire checkpoints ride on commits).
- No screenshots/files of hers in Claude Code sessions or in the repo.
- `.env` never committed.
- Every number in the post comes from `eval/results.md`, `notes/bench.md`, `notes/user_test.md`, or a Sentry screenshot.

## Partner checklist
- [ ] **Gemma**: core brain, plus vision baseline in eval. Mention model tag + sizes measured.
- [ ] **Entire**: enabled from hour 0; link 2–3 sessions in the post with one line each on what decision they show.
- [ ] **Sentry**: dashboard screenshot, one trace screenshot, one paragraph "what the traces told me".
- [ ] **Tinker** (stretch): baseline vs tuned on held-out app, cost, latency.
- [ ] Read the FAQ "Can one submission win more than once?" and the official rules once.
