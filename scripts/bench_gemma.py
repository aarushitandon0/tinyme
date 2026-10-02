"""Measure how long one Tiny Me step costs on this machine.

MASTERSPEC 14 lists "Gemma slow on her CPU laptop" as the first risk, and
PROJECTPLAN puts this before any feature work: if a step costs more than ~15 s
on e2b we need shorter prompts or fewer elements, and we need to know tonight.

What it does: builds a realistic prompt (a 60-line element list in the exact
MASTERSPEC 5.2 format, a goal, and two history steps), then runs 1 warm-up plus
N timed calls per model with structured output against the real
``app.brain.StepPlan`` schema, temperature 0, and ``keep_alive`` so the model
stays resident between calls.

    python scripts\\bench_gemma.py
    python scripts\\bench_gemma.py --models gemma4:e2b --runs 5

Installed client API, checked before writing this (P1 asks for exactly that):

    ollama 0.6.3
    ollama.chat(model, messages, *, format=<json schema dict>, options=...,
                keep_alive=..., think=bool|str|None)

``think`` exists on this version, so thinking text is turned off with
``think=False``. Some models reject the parameter outright; when that happens
the script retries without it and records which path it used, so the number in
notes/bench.md is never silently measured under different settings.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.brain import StepPlan  # noqa: E402

DEFAULT_MODELS = "gemma4:e2b,gemma4:e4b"
BENCH_NOTES = REPO_ROOT / "notes" / "bench.md"

#: Keep the model resident between calls, as MASTERSPEC 14 prescribes.
KEEP_ALIVE = "10m"

SYSTEM_PROMPT = (
    "You help a non-technical person use a Windows laptop. "
    "You are given her goal, the steps already done, and a numbered list of "
    "things currently on her screen. "
    "Choose exactly one element from the list and return its number as "
    "target_id. Never invent a number that is not in the list. "
    "Write instruction in plain short words, under 90 characters. "
    "If the right thing is not in the list, set cannot_see_it true and put a "
    "short suggestion in hint_if_missing. "
    "Never ask her for a password, OTP or card number."
)

# A plausible Explorer-plus-browser screen. Written out rather than generated so
# the prompt is stable across runs and across machines; a benchmark that
# changes shape between runs measures nothing.
FAKE_ELEMENT_TEXTS: list[tuple[str, str, str]] = [
    ("File Explorer", "uia:Button", "bottom-left"),
    ("Start", "uia:Button", "bottom-left"),
    ("Search", "uia:Edit", "bottom-left"),
    ("Microsoft Edge", "uia:Button", "bottom-left"),
    ("WhatsApp", "uia:Button", "bottom-left"),
    ("Home", "uia:TreeItem", "left-middle"),
    ("Gallery", "uia:TreeItem", "left-middle"),
    ("Desktop", "uia:TreeItem", "left-middle"),
    ("Downloads", "uia:TreeItem", "left-middle"),
    ("Documents", "uia:TreeItem", "left-middle"),
    ("Pictures", "uia:TreeItem", "left-middle"),
    ("Music", "uia:TreeItem", "left-middle"),
    ("Videos", "uia:TreeItem", "left-middle"),
    ("This PC", "uia:TreeItem", "left-middle"),
    ("Network", "uia:TreeItem", "left-middle"),
    ("New", "uia:Button", "top-left"),
    ("Cut", "uia:Button", "top-left"),
    ("Copy", "uia:Button", "top-left"),
    ("Paste", "uia:Button", "top-left"),
    ("Rename", "uia:Button", "top-center"),
    ("Share", "uia:Button", "top-center"),
    ("Delete", "uia:Button", "top-center"),
    ("Sort", "uia:Button", "top-center"),
    ("View", "uia:Button", "top-center"),
    ("Filter", "uia:Button", "top-right"),
    ("Details", "uia:Button", "top-right"),
    ("Name", "ocr:text", "top-center"),
    ("Date modified", "ocr:text", "top-center"),
    ("Type", "ocr:text", "top-right"),
    ("Size", "ocr:text", "top-right"),
    ("Today", "ocr:text", "center"),
    ("electricity bill september.pdf", "ocr:text", "center"),
    ("aadhaar scan.pdf", "ocr:text", "center"),
    ("medical report.pdf", "ocr:text", "center"),
    ("ticket confirmation.pdf", "ocr:text", "center"),
    ("photo 2026-09-28.jpg", "ocr:text", "center"),
    ("recipe notes.docx", "ocr:text", "center"),
    ("Earlier this week", "ocr:text", "center"),
    ("bank statement august.pdf", "ocr:text", "center"),
    ("insurance renewal.pdf", "ocr:text", "center"),
    ("wedding invite.pdf", "ocr:text", "center"),
    ("grocery list.txt", "ocr:text", "center"),
    ("Last month", "ocr:text", "center"),
    ("tax form 16.pdf", "ocr:text", "center"),
    ("passport photo.jpg", "ocr:text", "center"),
    ("4 October 2026", "ocr:text", "right-middle"),
    ("2 October 2026", "ocr:text", "right-middle"),
    ("28 September 2026", "ocr:text", "right-middle"),
    ("PDF File", "ocr:text", "right-middle"),
    ("JPG File", "ocr:text", "right-middle"),
    ("Word Document", "ocr:text", "right-middle"),
    ("248 KB", "ocr:text", "right-middle"),
    ("1,204 KB", "ocr:text", "right-middle"),
    ("15 items", "ocr:text", "bottom-left"),
    ("1 item selected", "ocr:text", "bottom-center"),
    ("Address bar", "uia:Edit", "top-center"),
    ("Downloads - File Explorer", "uia:TitleBar", "top-left"),
    ("Minimise", "uia:Button", "top-right"),
    ("Maximise", "uia:Button", "top-right"),
    ("Close", "uia:Button", "top-right"),
]

FAKE_GOAL = "I can't find the electricity bill I downloaded"

FAKE_HISTORY = [
    "Click the folder icon at the bottom of the screen",
    "Click Downloads in the left panel",
]


def build_element_lines(count: int = 60) -> str:
    """Render ``count`` lines in the exact model-facing format of MASTERSPEC 5.2."""
    rows = FAKE_ELEMENT_TEXTS[:count]
    if len(rows) < count:
        raise ValueError(f"only {len(rows)} fake elements defined, need {count}")
    return "\n".join(
        f'{index} | "{text}" | {source_role} | {region}'
        for index, (text, source_role, region) in enumerate(rows, start=1)
    )


def build_messages(element_lines: str) -> list[dict[str, str]]:
    """The same message shape P4 will send, so the timings transfer."""
    history = "\n".join(f"{i}. {step}" for i, step in enumerate(FAKE_HISTORY, start=1))
    user = (
        f"Her goal: {FAKE_GOAL}\n\n"
        f"Steps already done:\n{history}\n\n"
        f"On screen now:\n{element_lines}\n\n"
        "Give the next single step."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# --- machine info ----------------------------------------------------------


def _ram_gb() -> str:
    """Total physical RAM in GB, best effort, no extra dependency."""
    try:
        if sys.platform == "win32":
            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return f"{status.ullTotalPhys / (1024 ** 3):.0f} GB"
    except Exception:
        pass
    return "unknown"


def _run(command: list[str], timeout: int = 15) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return done.stdout.strip() if done.returncode == 0 else ""
    except Exception:
        return ""


def _cpu_name() -> str:
    if sys.platform == "win32":
        out = _run([
            "powershell", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_Processor).Name",
        ])
        if out:
            return " ".join(out.split())
    return platform.processor() or platform.machine() or "unknown"


def _gpu_name() -> str:
    """GPU, if any. Ollama only uses it when it can, so record what is present."""
    nvidia = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if nvidia:
        return " / ".join(line.strip() for line in nvidia.splitlines() if line.strip())
    if sys.platform == "win32":
        out = _run([
            "powershell", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_VideoController).Name",
        ])
        if out:
            return " / ".join(line.strip() for line in out.splitlines() if line.strip())
    return "none detected"


def machine_info() -> dict[str, str]:
    import ollama

    return {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": _cpu_name(),
        "cores": str(os.cpu_count() or "?"),
        "ram": _ram_gb(),
        "gpu": _gpu_name(),
        "python": platform.python_version(),
        "ollama_client": getattr(ollama, "__version__", "unknown"),
        "ollama_server": _run(["ollama", "--version"]) or "unknown",
    }


# --- the benchmark ---------------------------------------------------------


@dataclass
class ModelResult:
    model: str
    seconds: list[float] = field(default_factory=list)
    prompt_tokens: list[int] = field(default_factory=list)
    eval_tokens: list[int] = field(default_factory=list)
    valid: int = 0
    errors: list[str] = field(default_factory=list)
    think_disabled: bool = True

    @property
    def runs(self) -> int:
        return len(self.seconds)

    @property
    def median_s(self) -> float:
        return statistics.median(self.seconds) if self.seconds else float("nan")

    @property
    def p90_s(self) -> float:
        if not self.seconds:
            return float("nan")
        ordered = sorted(self.seconds)
        # Nearest-rank p90: ceil(0.9 * n). With 10 runs that is the 9th slowest,
        # which is what "p90" should mean on a sample this small.
        index = max(0, min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1))
        return ordered[index]

    def mean_int(self, values: list[int]) -> float:
        return statistics.mean(values) if values else float("nan")


def one_call(client, model: str, messages, schema: dict, think: bool | None):
    """One structured, deterministic chat call. Returns (seconds, response)."""
    kwargs = dict(
        model=model,
        messages=messages,
        format=schema,
        options={"temperature": 0},
        keep_alive=KEEP_ALIVE,
    )
    if think is not None:
        kwargs["think"] = think
    started = time.perf_counter()
    response = client.chat(**kwargs)
    return time.perf_counter() - started, response


def bench_model(client, model: str, messages, schema: dict, runs: int) -> ModelResult:
    result = ModelResult(model=model)

    # Warm-up: loads the model into memory. Not timed, because the first call
    # includes a multi-second load that no later step pays.
    think: bool | None = False
    print(f"\n{model}: warm-up ...", flush=True)
    try:
        one_call(client, model, messages, schema, think)
    except Exception as exc:
        if "think" in str(exc).lower():
            # This model or server build rejects the parameter. Fall back and
            # say so, rather than quietly measuring a different configuration.
            print(f"  think=False rejected ({exc}); retrying without it")
            think = None
            result.think_disabled = False
            try:
                one_call(client, model, messages, schema, think)
            except Exception as exc2:
                result.errors.append(f"warm-up failed: {exc2}")
                print(f"  warm-up failed: {exc2}")
                return result
        else:
            result.errors.append(f"warm-up failed: {exc}")
            print(f"  warm-up failed: {exc}")
            return result

    for run in range(1, runs + 1):
        try:
            seconds, response = one_call(client, model, messages, schema, think)
        except Exception as exc:
            result.errors.append(f"run {run}: {exc}")
            print(f"  run {run}/{runs}: FAILED {exc}")
            continue

        result.seconds.append(seconds)
        if getattr(response, "prompt_eval_count", None) is not None:
            result.prompt_tokens.append(int(response.prompt_eval_count))
        if getattr(response, "eval_count", None) is not None:
            result.eval_tokens.append(int(response.eval_count))

        content = response.message.content or ""
        try:
            StepPlan.model_validate_json(content)
            result.valid += 1
            verdict = "valid"
        except Exception as exc:
            verdict = f"INVALID ({type(exc).__name__})"
            result.errors.append(f"run {run} schema: {str(exc)[:160]}")
        print(f"  run {run}/{runs}: {seconds:5.2f} s  {verdict}", flush=True)

    return result


def format_report(results: list[ModelResult], info: dict[str, str], runs: int) -> str:
    lines = [
        f"## Run {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- machine: {info['cpu']} ({info['cores']} cores), {info['ram']} RAM",
        f"- gpu: {info['gpu']}",
        f"- os: {info['os']}",
        f"- python {info['python']}, ollama client {info['ollama_client']}, "
        f"server {info['ollama_server']}",
        f"- prompt: 60 elements in MASTERSPEC 5.2 format, goal + 2 history steps, "
        f"structured output against StepPlan, temperature 0, keep_alive {KEEP_ALIVE}",
        f"- {runs} timed calls per model after 1 untimed warm-up",
        "",
        "| model | median s | p90 s | prompt tokens | output tokens | schema valid | thinking off |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if not r.seconds:
            lines.append(f"| {r.model} | did not run | - | - | - | - | - |")
            continue
        lines.append(
            f"| {r.model} | {r.median_s:.2f} | {r.p90_s:.2f} | "
            f"{r.mean_int(r.prompt_tokens):.0f} | {r.mean_int(r.eval_tokens):.0f} | "
            f"{r.valid}/{r.runs} | {'yes' if r.think_disabled else 'no'} |"
        )

    problems = [(r.model, e) for r in results for e in r.errors]
    if problems:
        lines += ["", "Problems:"]
        lines += [f"- `{model}`: {err}" for model, err in problems[:12]]

    budget = [r for r in results if r.seconds]
    if budget:
        fastest = min(budget, key=lambda r: r.median_s)
        verdict = (
            "within the 15 s/step budget"
            if fastest.median_s <= 15
            else "OVER the 15 s/step budget - shorten the prompt or cut elements"
        )
        lines += ["", f"Verdict: fastest is `{fastest.model}` at "
                      f"{fastest.median_s:.2f} s median, {verdict}."]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default=DEFAULT_MODELS,
                        help=f"comma-separated model tags (default {DEFAULT_MODELS})")
    parser.add_argument("--runs", type=int, default=10, help="timed calls per model")
    parser.add_argument("--elements", type=int, default=60, help="fake element lines")
    parser.add_argument("--no-write", action="store_true",
                        help="print the report but do not append to notes/bench.md")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    try:
        import ollama
    except ImportError:
        print("The ollama python client is not installed. pip install -e \".[dev]\"")
        return 2

    client = ollama.Client()
    try:
        installed = {m.model for m in client.list().models}
    except Exception as exc:
        print(f"Cannot reach the Ollama server: {exc}")
        print("Start Ollama (it runs as a service after install) and try again.")
        return 2

    schema = StepPlan.model_json_schema()
    element_lines = build_element_lines(args.elements)
    messages = build_messages(element_lines)
    prompt_chars = sum(len(m["content"]) for m in messages)
    print(f"Prompt: {args.elements} elements, {prompt_chars} characters")
    print(f"Schema: {json.dumps(schema)[:120]}...")

    wanted = [m.strip() for m in args.models.split(",") if m.strip()]
    missing = [m for m in wanted if m not in installed and f"{m}:latest" not in installed]
    if missing:
        print(f"\nNot installed: {', '.join(missing)}")
        print(f"Pull with: {'; '.join(f'ollama pull {m}' for m in missing)}")
        print(f"Installed: {', '.join(sorted(installed)) or '(none)'}")
        wanted = [m for m in wanted if m not in missing]
        if not wanted:
            return 2

    results = [bench_model(client, m, messages, schema, args.runs) for m in wanted]
    report = format_report(results, machine_info(), args.runs)
    print("\n" + report)

    if not args.no_write:
        BENCH_NOTES.parent.mkdir(parents=True, exist_ok=True)
        if not BENCH_NOTES.exists():
            BENCH_NOTES.write_text(
                "# Gemma speed on real hardware\n\n"
                "Produced by `scripts/bench_gemma.py`. Every number in the post "
                "and README comes from this file, never from memory.\n\n",
                encoding="utf-8",
            )
        with BENCH_NOTES.open("a", encoding="utf-8") as handle:
            handle.write(report + "\n")
        print(f"Appended to {BENCH_NOTES.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
