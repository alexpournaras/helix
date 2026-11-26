"""Research steps (PLAN B8), one module per step, and the pipeline factory the worker and eval use.

Stages that aren't built yet are stubs that end the run as `incomplete` with the task that builds them, so
the eval runner can exercise what exists (1C.1 onward).
"""

from __future__ import annotations

from typing import Any, Callable

from ..pipeline import Complete, Pipeline, StageDef
from . import brief_update, grounding, intake, plan, queries, questions, safety, screen, search

NOT_BUILT = {"reading": "1E"}


def not_built(name: str) -> StageDef:
    task = NOT_BUILT.get(name, "a later task")
    return StageDef(name, on_complete=lambda sc: Complete("incomplete", f"Stage {name} is not built yet ({task})."))


def stop_stage(name: str) -> StageDef:
    return StageDef(name, on_complete=lambda sc: Complete("incomplete", f"Stopped at stage {name} (stop_at)."))


def build_pipeline(ctx: Any = None, *, stop_at: str | None = None) -> tuple[Pipeline, dict[str, Callable]]:
    """(Pipeline, resolvers). A resolver maps a `waiting_for` value to fn(case, investigation) → apply
    (or None when the case has no scripted answer). `stop_at` ends the run as `incomplete` on entering that
    stage, before it queues anything (tests and evals that check how far a run gets)."""
    stages = {"intake": intake.stage("safety"), "safety": safety.stage(),
              "safety_finalize": safety.finalize_stage(), "grounding": grounding.stage(),
              "questions": questions.stage(), "brief_update": brief_update.stage(), "planning": plan.stage(), "querying": queries.stage(),
              "searching": search.searching_stage(), "prefiltering": search.prefilter_stage(),
              "screening": screen.screening_stage(), "selecting": screen.selecting_stage(),
              "fetching": screen.fetching_stage()}
    stages.update({name: not_built(name) for name in NOT_BUILT})
    if stop_at is not None:
        stages[stop_at] = stop_stage(stop_at)
    steps = {"intake": intake.STEP, "safety": safety.STEP, "safety_finalize": safety.FINALIZE_STEP,
             "ground": grounding.STEP, "ground_merge": grounding.MERGE_STEP,
             "questions": questions.STEP, "brief_update": brief_update.STEP, "plan": plan.STEP, "queries": queries.STEP,
             "search": search.SEARCH_STEP, "seed": search.SEED_STEP, "prefilter": search.PREFILTER_STEP,
             "screen": screen.SCREEN_STEP, "select": screen.SELECT_STEP, "fetch": screen.FETCH_STEP}
    resolvers: dict[str, Callable] = {
        "problem_rewrite": lambda case, inv: intake.rewrite(case.rewrite) if getattr(case, "rewrite", None) else None,
        "safety_ack": lambda case, inv: safety.acknowledge() if getattr(case, "acknowledge", True) else None,
        "answers": lambda case, inv: questions.scripted(getattr(case, "answers", {}) or {})(case, inv),
        "plan_approval": lambda case, inv: plan.approve() if getattr(case, "approve_plan", True) else None,
    }
    return Pipeline(stages=stages, steps=steps, first_stage="intake"), resolvers
