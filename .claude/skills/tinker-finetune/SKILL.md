---
name: tinker-finetune
description: Fine-tune a small open model on Tinker (Thinking Machines) to pick the correct numbered element for Tiny Me, and compare it with the untuned model and Gemma. Use when the user mentions Tinker, fine-tuning, LoRA, or the tuned picker.
---

# Tinker fine-tune for element picking

## Re-verify first (docs change)
- Read the current lineup at tinker-docs.thinkingmachines.ai/model-lineup. As of early Oct 2026 it lists Qwen3, Qwen3-VL, Llama 3.x, gpt-oss, DeepSeek-V3.1 and Kimi models, and **no Gemma**. Default choice: `Qwen/Qwen3-4B-Instruct-2507` (instruction-tuned, compact, no chain-of-thought latency).
- Check current pricing and the user's credits. Estimate tokens = examples × average tokens × epochs and show the cost before training. Ask before exceeding the credits.
- Check whether trained LoRA weights can be downloaded and how; this decides whether the tuned model can run locally in Ollama.
- Start from the official Tinker cookbook's supervised-learning example rather than writing a training loop from scratch.

## Dataset (`finetune/build_dataset.py`)
- Source: `eval/labels.jsonl` + frozen element JSON. No images.
- Input: the exact system + user message `brain.py` sends. Target: the exact StepPlan JSON (instruction may be templated; `target_id` is what matters).
- Augment: renumber element IDs consistently, paraphrase goals, drop random non-target elements.
- **Split by app**: one whole app held out for test. No augmented copy of a test screenshot may appear in train.
- Write `finetune/data_card.md`: counts, apps per split, augmentation used.

## Train and evaluate
- LoRA, small rank, few epochs; log loss.
- Evaluate on the held-out app: untuned Qwen3-4B, tuned Qwen3-4B, Gemma 4 e2b/e4b, same prompt, temperature 0. Metrics: correct-element rate, JSON validity, latency (say where it was measured: Tinker sampler vs local).
- Add rows to `eval/results.md` through `run_eval.py`, not by hand.

## Local deployment (only if weights are downloadable)
- Merge LoRA into base weights, convert to GGUF with llama.cpp tools, `ollama create tinyme-picker -f Modelfile`, then measure on the target laptop with `scripts/bench_gemma.py --models tinyme-picker`.
- If not done in time, say plainly in results and post that the tuned model was evaluated via Tinker only.

## Honesty
- With about 40 base screenshots, gains may be noise. Report counts and say so.
