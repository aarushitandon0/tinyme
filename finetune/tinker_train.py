"""LoRA fine-tune the element picker on Tinker (P13, MASTERSPEC 9).

    python finetune\\tinker_train.py --dry-run          # cost estimate only
    python finetune\\tinker_train.py --yes              # actually train

Needs `pip install tinker tinker-cookbook` and `TINKER_API_KEY`. Neither is in
``pyproject.toml``: this is a stretch goal that runs on the developer's machine
and must not become a dependency of the app she uses (CLAUDE.md -- no new
dependencies without a reason, and no cloud services in the runtime path).

**--dry-run is the default.** Training spends credits, so nothing is sent until
``--yes``, and the estimate is printed either way.

## Verified against the Tinker docs on 4 Oct 2026

MASTERSPEC 9 expected ``Qwen/Qwen3-4B-Instruct-2507``. **That model id is gone
from the lineup.** The current smallest instruction-tuned model is
``Qwen/Qwen3.5-4B`` (dense, 64K context), at **$0.737 per million training
tokens**, with sampling at $0.33/M prefill and $1.005/M output. There is still
no Gemma on Tinker, which is the whole reason this fine-tune is a Qwen and the
story is "swap the brain" rather than "tune Gemma". Trained LoRA weights *can*
be exported (``build_lora_adapter``, ``download``, ``publish_to_hf_hub``), so
the local-Ollama path in MASTERSPEC 9 is at least possible.

## What has not happened

**This script has never been run.** It is written against the documented API,
not against a successful run, because this machine has no ``TINKER_API_KEY`` and
the packages are not installed. Until it runs, ``finetune/DECISION.md`` is the
honest statement of where P13 stands, and no tuned number may appear anywhere.
The first real run will almost certainly need a line or two corrected here; that
is expected, and it is why ``--dry-run`` prints the plan before spending
anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from finetune.build_dataset import TRAIN_PATH, tokens_estimate  # noqa: E402

log = logging.getLogger("tinker_train")

OUT_DIR = Path(__file__).resolve().parent
RUN_PATH = OUT_DIR / "run.json"

#: The smallest instruction-tuned model on Tinker as of 4 Oct 2026. Not the id
#: MASTERSPEC 9 expected -- see the module docstring.
BASE_MODEL = "Qwen/Qwen3.5-4B"

#: ``tinker_cookbook.renderers.get_renderer`` needs the chat template's name,
#: which tracks the model family rather than the exact id.
#:
#: **The disable-thinking variant, deliberately.** ``Qwen3_5Renderer`` ends the
#: generation prompt with an open ``<think>\n``, which prefills the model into
#: reasoning; with ``max_tokens=256`` in ``eval/run_eval.py`` the budget can go
#: entirely on reasoning and the JSON never arrives, which would score as an
#: invalid reply for reasons that have nothing to do with picking an element.
#: ``Qwen3_5DisableThinkingRenderer`` prefills a closed empty block instead, so
#: the answer starts immediately. tinker-cookbook's own examples
#: (``rl/train.py``, ``preference/*``, ``eval/custom_evaluators.py``) all use
#: this variant for structured output. ``run_eval.py`` must sample with the same
#: renderer this trained with, or the tuned model meets a template it never saw.
RENDERER = "qwen3_5_disable_thinking"

#: USD per million training tokens for :data:`BASE_MODEL`, from the Models &
#: Pricing page on 4 Oct 2026. Re-check before quoting a cost: it is a published
#: price and it moves.
TRAIN_USD_PER_MTOK = 0.737

#: Small rank, few epochs: 20 labelled screens cannot support more, and the
#: tinker skill's warning about gains being noise applies at any rank.
DEFAULT_RANK = 16
DEFAULT_EPOCHS = 3
DEFAULT_LR = 2e-4
DEFAULT_BATCH = 8

#: The model's own cap. A prompt longer than this is truncated, which would
#: silently cut the end of an eighty-element list -- so the builder counts how
#: many examples are over and says so rather than letting it happen quietly.
MAX_LENGTH = 4096


def load_conversations(path: Path = TRAIN_PATH) -> list[list[dict[str, str]]]:
    """Read ``train.jsonl`` into plain chat conversations."""
    if not path.exists():
        raise SystemExit(f"{path} is missing. Run finetune\\build_dataset.py first.")
    conversations = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            conversations.append(json.loads(line)["messages"])
    if not conversations:
        raise SystemExit(f"{path} is empty")
    return conversations


def estimate(conversations: list[list[dict[str, str]]], epochs: int) -> dict[str, object]:
    """Tokens and dollars, before anything is sent.

    The token count is characters over four -- an estimate, and labelled as one
    wherever it is printed. It is here to answer "is this cents or hundreds of
    dollars", which it does with room to spare; it is not a billing figure.
    """
    from finetune.build_dataset import Example

    fake = [Example(row_id="", app="", messages=conv[:-1],
                    completion=conv[-1]["content"]) for conv in conversations]
    per_epoch = tokens_estimate(fake)
    total = per_epoch * epochs
    return {
        "examples": len(conversations),
        "tokens_per_epoch": per_epoch,
        "epochs": epochs,
        "total_tokens": total,
        "usd": round(total / 1_000_000 * TRAIN_USD_PER_MTOK, 4),
        "usd_per_mtok": TRAIN_USD_PER_MTOK,
        "base_model": BASE_MODEL,
    }


def print_estimate(numbers: dict[str, object]) -> None:
    print(f"base model        {numbers['base_model']}")
    print(f"examples          {numbers['examples']}")
    print(f"epochs            {numbers['epochs']}")
    print(f"tokens (est.)     ~{numbers['total_tokens']:,} "
          f"(~{numbers['tokens_per_epoch']:,} per epoch, chars/4)")
    print(f"training cost     ~${numbers['usd']} "
          f"at ${numbers['usd_per_mtok']}/M tokens (price checked 4 Oct 2026)")
    print()
    print("Estimate only: the real tokenizer is on Tinker's side, and the price")
    print("is a published figure that moves. Re-check before quoting either.")


async def train(conversations: list[list[dict[str, str]]], *, rank: int, epochs: int,
                lr: float, batch: int) -> dict[str, object]:
    """Run the LoRA fine-tune and save weights for sampling.

    Written from the documented cookbook flow: ``conversation_to_datum`` with
    ``TrainOnWhat.LAST_ASSISTANT_MESSAGE`` so the loss falls only on the JSON we
    want back, then ``forward_backward`` / ``optim_step`` per batch, then
    ``save_weights_for_sampler``. The returned path is what
    ``eval/run_eval.py --systems tinker_tuned`` samples from.
    """
    import tinker
    from tinker_cookbook.renderers import TrainOnWhat, get_renderer
    from tinker_cookbook.supervised.data import conversation_to_datum

    service = tinker.ServiceClient()
    client = await service.create_lora_training_client_async(
        base_model=BASE_MODEL, rank=rank
    )
    renderer = get_renderer(RENDERER, client.get_tokenizer())

    data = [
        conversation_to_datum(conv, renderer, max_length=MAX_LENGTH,
                              train_on_what=TrainOnWhat.LAST_ASSISTANT_MESSAGE)
        for conv in conversations
    ]
    log.info("prepared %d datums, rank %d, %d epochs", len(data), rank, epochs)

    losses: list[float] = []
    started = time.perf_counter()
    for epoch in range(1, epochs + 1):
        for start in range(0, len(data), batch):
            chunk = data[start:start + batch]
            fwdbwd = await client.forward_backward_async(chunk, "cross_entropy")
            optim = await client.optim_step_async(tinker.AdamParams(learning_rate=lr))
            result = await fwdbwd.result_async()
            await optim.result_async()
            loss = getattr(result, "loss", None)
            if loss is not None:
                losses.append(float(loss))
        log.info("epoch %d/%d done, last loss %s", epoch, epochs,
                 f"{losses[-1]:.4f}" if losses else "unreported")

    saved = await client.save_weights_for_sampler_async("tinyme-picker")
    path = getattr(await saved.result_async(), "path", None) if hasattr(saved, "result_async") \
        else getattr(saved, "path", None)

    return {
        "base_model": BASE_MODEL,
        "rank": rank,
        "epochs": epochs,
        "learning_rate": lr,
        "batch": batch,
        "examples": len(conversations),
        "train_seconds": round(time.perf_counter() - started, 1),
        "losses": losses,
        "checkpoint": path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true",
                        help="actually train; without it this is a dry run")
    parser.add_argument("--dry-run", action="store_true", help="the default")
    parser.add_argument("--rank", type=int, default=DEFAULT_RANK)
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--lr", type=float, default=DEFAULT_LR)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--budget-usd", type=float, default=5.0,
                        help="refuse to train above this estimate (default 5)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conversations = load_conversations()
    numbers = estimate(conversations, args.epochs)
    print_estimate(numbers)

    if not args.yes:
        print("\nDry run. Nothing was sent to Tinker. Add --yes to train.")
        return 0

    if float(numbers["usd"]) > args.budget_usd:  # type: ignore[arg-type]
        raise SystemExit(
            f"estimated ${numbers['usd']} is over the --budget-usd of "
            f"${args.budget_usd}. Raise the budget deliberately or cut --epochs."
        )

    outcome = asyncio.run(train(conversations, rank=args.rank, epochs=args.epochs,
                                lr=args.lr, batch=args.batch))
    outcome["estimate"] = numbers
    RUN_PATH.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8",
                        newline="\n")
    print(f"\ncheckpoint: {outcome['checkpoint']}")
    print(f"run recorded in {RUN_PATH}")
    print("\nNow score it, and let run_eval.py write the row:")
    print(f'  set TINYME_TINKER_CHECKPOINT={outcome["checkpoint"]}')
    print("  python eval\\run_eval.py --systems tinker_base,tinker_tuned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
