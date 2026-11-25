"""Research steps (PLAN B8), one module per step, and the pipeline factory the worker and eval use.

Stages that aren't built yet are stubs that end the run as `incomplete` with the task that builds them, so
the eval runner can exercise what exists (1C.1 onward).
"""

from __future__ import annotations

from typing import Any, Callable

from ..pipeline import Complete, Pipeline, StageDef
from . import intake, safety

NOT_BUILT = {"grounding": "1C.3", "questions": "1C.4", "brief_update": "1C.4", "planning": "1D"}


def not_built(name: str) -> StageDef:
    task = NOT_BUILT.get(name, "a later task")
    return StageDef(name, on_complete=lambda sc: Complete("incomplete", f"Stage {name} is not built yet ({task})."))


def build_pipeline(ctx: Any = None) -> tuple[Pipeline, dict[str, Callable]]:
    """(Pipeline, resolvers). A resolver maps a `waiting_for` value to fn(case, investigation) → apply
    (or None when the case has no scripted answer)."""
    stages = {"intake": intake.stage("safety"), "safety": safety.stage(),
              "safety_finalize": safety.finalize_stage()}
    stages.update({name: not_built(name) for name in NOT_BUILT})
    steps = {"intake": intake.STEP, "safety": safety.STEP, "safety_finalize": safety.FINALIZE_STEP}
    resolvers: dict[str, Callable] = {
        "problem_rewrite": lambda case, inv: intake.rewrite(case.rewrite) if getattr(case, "rewrite", None) else None,
        "safety_ack": lambda case, inv: safety.acknowledge() if getattr(case, "acknowledge", True) else None,
    }
    return Pipeline(stages=stages, steps=steps, first_stage="intake"), resolvers
