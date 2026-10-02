"""The model's output contract (MASTERSPEC 5.3).

Only the schema lives here so far. P4 adds the Ollama call, validation and the
retry path around it; ``scripts/bench_gemma.py`` already benchmarks against
this exact schema, so the timings describe the real thing rather than a copy.

CLAUDE.md rule 1: the model returns a ``target_id`` from the numbered element
list. It never returns coordinates, and no bbox ever enters the prompt.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class CheckType(str, Enum):
    """The deterministic success checks from MASTERSPEC 5.4.

    The model proposes which check to run; ``watcher.py`` runs it. The model
    never judges success itself.
    """

    WINDOW_CLASS_IS = "window_class_is"
    WINDOW_TITLE_CONTAINS = "window_title_contains"
    TEXT_APPEARS = "text_appears"
    TEXT_DISAPPEARS = "text_disappears"
    REGION_CHANGED = "region_changed"


class SuccessCheck(BaseModel):
    """How to tell, in code, that the step worked."""

    type: CheckType
    value: str = Field(default="", max_length=120)


class StepPlan(BaseModel):
    """One step: what to say, what to circle, and how to know it worked.

    Mirrors the JSON in MASTERSPEC 5.3 exactly.
    """

    #: Plain words for a non-technical reader. Capped at 90 chars by MASTERSPEC.
    instruction: str = Field(max_length=90)
    #: An id from this step's numbered element list. 0 means "none", which is
    #: only valid when goal_reached or cannot_see_it is true. P4 validates that
    #: the id actually exists in the list.
    target_id: int = Field(ge=0)
    success_check: SuccessCheck
    goal_reached: bool = False
    cannot_see_it: bool = False
    #: Shown instead of a circle when cannot_see_it is true.
    hint_if_missing: str = Field(default="", max_length=90)
