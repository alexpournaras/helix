"""Research steps (PLAN B8), one module per step, and the pipeline factory the worker and eval use.

Stages that aren't built yet are stubs that end the run as `incomplete` with the task that builds them, so
the eval runner can exercise what exists (1C.1 onward).
"""

from __future__ import annotations

from typing import Any, Callable

from ..pipeline import Complete, Pipeline, StageDef
from . import intake

NOT_BUILT = {"safety": "1C.2"}


def not_built(name: str) -> StageDef:
    task = NOT_BUILT.get(name, "a later task")
    return StageDef(name, on_complete=lambda sc: Complete("incomplete", f"Stage {name} is not built yet ({task})."))


def build_pipeline(ctx: Any = None) -> tuple[Pipeline, dict[str, Callable]]:
    """(Pipeline, resolvers). A resolver maps a `waiting_for` value to fn(case, investigation) → apply."""
    stages = {"intake": intake.stage("safety"), "safety": not_built("safety")}
    steps = {"intake": intake.STEP}
    resolvers: dict[str, Callable] = {
        "problem_rewrite": lambda case, inv: intake.rewrite(case.rewrite) if getattr(case, "rewrite", None) else None,
    }
    return Pipeline(stages=stages, steps=steps, first_stage="intake"), resolvers
