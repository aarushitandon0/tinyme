---
name: tinyme-guardrails
description: Safety and privacy review for Tiny Me. Use before committing any change to guard.py, actions.py, capture.py, overlay.py, brain.py prompts, telemetry.py, or the Playwright flow, and whenever the user says "check guardrails", "safety check", or "ready to commit".
---

# Tiny Me guardrails review

Run every check below against the current diff (`git diff --staged`, or `git diff` if nothing is staged) and the files it touches. Report PASS/FAIL per item with file:line evidence. Do not fix silently; list fixes, then apply them only if the user agrees.

## Invariants
1. `guard.is_sensitive` is called before every `brain` call and before every automated click/type. Trace each call path.
2. No code path calls Playwright `fill`, `type`, `press`, or `click` on an element matched by the DOM guard (password, `cc-*`, `one-time-code`, OTP/CVV/card/UPI PIN labels).
3. "Do it for me" checks `guard.can_do_it`; unknown task types fall back to guide mode.
4. The model prompt contains no bbox/pixel values; the model output contains no coordinates; `target_id` is validated against the current element list.
5. Screenshots are never written to disk outside `eval/` tools. Search for `.save(`, `imwrite`, `open(..., "wb")` on image data.
6. Capture happens only after the hotkey or inside an active task. No timers that capture while idle.
7. Telemetry: no-op unless `TINYME_TELEMETRY=1`; `send_default_pii=False`; span attributes are numbers/enums only. Search for any span attribute receiving goal, instruction, element text, window title, filename, or path.
8. Logging: screen text only at DEBUG.
9. Overlay is click-through and capture-excluded (or uses the hide→capture→show fallback).
10. No Qt widget touched from a non-main thread.
11. No secrets in the diff (`SENTRY_DSN`, API keys); `.env` is gitignored.
12. No real user data in the repo or in this session (screenshots, file names, personal details).

## Then
- Run `pytest -q` and report the result.
- If anything changed user-visible safety behaviour, remind the user to update README "Safety and privacy".
