---
name: dev-post-writer
description: Draft or check the DEV.to submission post for the Hacktoberfest 2026 "Build for a Friend" weekend challenge about Tiny Me. Use when the user asks to write, outline, edit, or review the post, the README story, or the submission.
---

# DEV post for Tiny Me

## Hard requirements
- Tags: `#devchallenge #weekendchallenge #hf26challenge` (the submission template adds them; verify they are present).
- Say what was built and who it is for, show a demo (video link/embed), and explain why open innovation matters for this project.
- List teammates' DEV handles if any. One post per team.
- Link the repo. Link agent sessions (Entire); a DevRelay embed is optional.
- Judging: writing quality is weighted most, then relevance to the theme, creativity, technical execution, partner tech.

## Sources of truth (only these)
- Numbers: `eval/results.md`, `notes/bench.md`, Sentry screenshots in `docs/`.
- Her words: `notes/user_test.md`, quoted exactly. Never invent or polish a quote.
- If a needed fact is missing, write `[[TODO: ...]]` instead of guessing.

## Structure
1. The moment: the 9 p.m. phone call, in 3–5 concrete sentences.
2. Demo: video first, then 2–3 screenshots with one-line captions.
3. How it works: one diagram (capture → read → guard → Gemma picks a number → circle → wait/check → repeat), then the key idea: the model picks a number, never pixels.
4. Why open: runs offline on her laptop; her screen never leaves it; no per-use cost; I could swap or fine-tune the brain. Say where it beat the vision/closed baseline and where it didn't.
5. Numbers with failures: results table, counts not only percentages, three failures.
6. Handing it over: what she did, timings, her exact words, complaints included.
7. Safety and privacy: handover at passwords/payments, capture only on hotkey, telemetry off on her laptop.
8. How I built it: 2–3 Entire session links with one line each on the decision they show; what Sentry traces revealed; the Tinker result if any.
9. Limitations and what's next.

## Style
- Plain, warm, first person. Short paragraphs. No hype words.
- Every claim checkable against the repo.
- Finish with a checklist of every `[[TODO]]` and every number with its source file.
