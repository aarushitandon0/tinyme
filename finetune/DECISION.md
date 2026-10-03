# P13 — Tinker fine-tune: where this actually stands

Written 4 Oct 2026. This is the honest record MASTERSPEC §7 ("Tier 3, only if
the rest is solid") and PROJECTPLAN's Sunday row ask for: *a row in the results
table, or a decision to skip written down*.

**Short version: the pipeline is built and tested; the training run has not
happened, and no tuned number exists. Nothing tuned may be claimed anywhere
until `eval/run_eval.py --systems tinker_base,tinker_tuned` has written the
rows.**

## Facts re-verified against the Tinker docs, 4 Oct 2026

The `tinker-finetune` skill says to re-check these because the docs move. They
had moved.

| What MASTERSPEC §1/§9 said | What the docs say now |
|---|---|
| Smallest instruction model: `Qwen/Qwen3-4B-Instruct-2507` | **That id is gone.** The smallest instruction-tuned model is **`Qwen/Qwen3.5-4B`** (dense, 64K context, hybrid + vision) |
| No Gemma on Tinker | Still true — the lineup is Qwen, Nemotron, GLM, Kimi, gpt-oss, DeepSeek, Inkling |
| Pricing to be checked | **$0.737 / M training tokens**; sampling $0.33/M prefill, $1.005/M output |
| LoRA weights downloadable? | **Yes** — `build_lora_adapter`, `download`, `publish_to_hf_hub`, and a "Build a PEFT LoRA Adapter" cookbook guide. So the merge → GGUF → `ollama create tinyme-picker` path in MASTERSPEC §9 is possible, not just hoped for |
| LoRA only, no full fine-tune | Confirmed |

The model id change is the one that matters for the write-up: the spec's
"Qwen3-4B-Instruct-2507" must not appear in the post. MASTERSPEC has been
corrected in place.

## What is built and tested

- **`build_dataset.py`** — turns `eval/labels.jsonl` into Tinker chat-format
  JSONL. Prompts come from `app.brain.build_messages` itself, so the dataset
  cannot drift from the shipped prompt. Augments by shuffling and renumbering
  the element list (the one that matters), thinning non-target elements, and
  wrapping the goal. Splits by app, refuses when there is only one, and checks
  for train/test leakage by row id.
- **`tinker_train.py`** — the documented cookbook flow
  (`conversation_to_datum` with `LAST_ASSISTANT_MESSAGE`, `forward_backward`,
  `optim_step`, `save_weights_for_sampler`). Dry run by default; prints the
  cost estimate; refuses above `--budget-usd`.
- **`eval/run_eval.py --systems tinker_base,tinker_tuned`** — the comparison,
  so any tuned row is generated rather than typed (the skill's rule). Same
  prompt and same validation as the pipeline rows.
- **`tests/test_build_dataset.py`** — 22 tests, mostly about the one bug that
  would poison this in silence: the label following the renumbering.
- **`data_card.md`** — generated, with the counts and the warnings.

## Why it has not been run

Two blockers, in order of how badly they matter.

### 1. One labelled app, so there is no held-out app

`eval/labels.jsonl` has **20 rows across 3 screenshots of File Explorer, and
nothing else.** MASTERSPEC §9 and the skill both require holding out a whole
app, so that the test says something about a screen the model has never seen.
With one app that is impossible.

`build_dataset.py` refuses by default rather than quietly splitting by
screenshot, because a number from that weaker split reads like generalisation
and is not. `--holdout-screenshot` forces it and stamps the data card with the
sentence that has to travel with the numbers.

**Fix:** `python eval\collect.py --group all` on a quiet machine — the
collector needs the desktop to itself — then label the new screens. The other
four app groups (settings, calculator, paint, demo_site) are already in
`collect.py`; the note at the bottom of `labels.jsonl` says why they are
unlabelled.

### 2. Sample size

20 labelled rows, 56 train examples after augmentation, **6 test examples.**
Augmentation multiplies passes over the same three screens; it does not add
screens. On six test examples, one example is 17 percentage points. A
tuned-vs-untuned difference measured here would be noise wearing a number's
clothes, and the data card says so.

Cost is not a blocker: the estimate for 3 epochs is **~$0.12**, and
`tinker_train.py` prints it before spending anything.

Also, practically: this machine has no `TINKER_API_KEY` and neither `tinker`
nor `tinker-cookbook` is installed, so `tinker_train.py` has been run as a dry
run only. It is written against the documented API, not against a successful
run, and the first real run will probably need a line corrected.

## A limitation of the dataset itself, regardless of size

`eval/labels.jsonl` labels **which element is correct** and nothing else. So in
the target JSON, `target_id` and `cannot_see_it` are real labels, while
`instruction` and `success_check` are templates — and `success_check` is
templated to `region_changed` every time, because that is the only check valid
for any element without knowing more about the screen.

Consequences:

- A model trained on this may be judged on **correct-element rate and JSON
  validity only.**
- Dropping it in as `app/brain.py`'s model wholesale would **regress** the app:
  it would answer `region_changed` always, and the cheap window checks are what
  keep a CPU laptop responsive (MASTERSPEC §5.4).
- The fix is to add `instruction` and `success_check` to the labels, not to make
  the templates cleverer.

So even with a second app labelled, the honest claim is narrower than "a tuned
drop-in brain". It is "a tuned *picker*", which is what MASTERSPEC §9 calls it.

## The decision

**Skip the training run for the hackathon deadline unless a second app gets
labelled first.** In priority order, if Sunday has time left:

1. Label a second and third app (this also improves every existing eval row).
2. Then `build_dataset.py` with a real held-out app, `tinker_train.py --yes`
   (~$0.12), and `run_eval.py --systems tinker_base,tinker_tuned`.
3. Only then, if the gap is larger than the noise floor, write it up.

If it stays skipped, the post says this, in one line: *the open-weights
argument is that the brain is swappable — the dataset builder and the Tinker
training script are in `finetune/`, and they were not run because there was
only one labelled app to hold out, which would have made the number
meaningless.* That is a stronger paragraph than an invented improvement, and
it is the only one the evidence supports.

## What must not be said

- No tuned accuracy, latency or cost figure. There is no run.
- Not "we fine-tuned Qwen3-4B-Instruct-2507". That model is not on Tinker any
  more.
- Not "the tuned picker runs locally in Ollama". The export path is documented
  and plausible; it has not been done, and local speed has not been measured.
