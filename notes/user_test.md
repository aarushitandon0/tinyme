# User session notes

**No session has happened yet. There is nothing in this file to quote.**

PROJECTPLAN.md puts user session #1 on Saturday night, and P11 ("fix what she got
stuck on") takes its input from here. Until she has sat in front of it, there are
no stuck points, no timings and no quotes — and CLAUDE.md's honesty rules mean
none may be written down, estimated or imagined in the meantime. If the post
needs a sentence about what she said, that sentence does not exist yet.

---

## How to run it

Three tasks with Tiny Me, three the usual way (calling me). Same three kinds of
task in both halves, order swapped between them so the second half is not simply
the practised half.

Before starting, tell her exactly what it can see — the whole screen, only while
it is working, nothing leaving the laptop — and ask whether that is all right.
If she says no, that is the result, and it goes in the post.

Record while it happens, not afterwards:

- wall-clock time per task, both ways
- every point where she stopped and waited, and what she was waiting for
- **her exact words**, including the complaints and the bored silences
- what she did that the design did not expect

Do not coach her through a step. A step she cannot do unaided is the finding.

---

## Session 1 — _not yet run_

| | |
|---|---|
| date | |
| build (git sha) | |
| model | |
| her laptop or mine | |

### Task 1 — with Tiny Me

- goal she typed:
- time:
- stuck at:
- exact words:

### Task 2 — with Tiny Me

### Task 3 — with Tiny Me

### Tasks 4–6 — the usual way (calling me)

| task | time | what she had to ask |
|---|---|---|

### Stuck points, in her order of annoyance

1.
2.
3.

### What she said about the privacy explanation

---

## Feeding this into P11

For each stuck point: the smallest fix that removes it, implemented if it fits in
the time, and written into `notes/limitations.md` honestly if it does not. Then
re-run `pytest -q` and `python eval\run_eval.py --systems all`, and say which
metric moved and which did not. A fix that moves nothing measurable is still a
fix if she stops getting stuck — say that too, rather than quietly claiming a
number.
