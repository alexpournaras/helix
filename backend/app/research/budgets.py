"""Guards per run segment (PLAN B4 "Guards", B15 `guards`) and the case budget of the `budget` profile (B17.4).

- Acquisition stop: no new search, snowballing, screening, fetching, reading or grounding starts. Papers and
  screenings stop at their maximum; wall time, tokens and cost stop at (1 − reserve) of theirs, so the
  remaining reserve finishes processing and the report.
- Hard stop: wall time, tokens or cost at their maximum → finalize with the best checked report, else `incomplete`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import ResearchDB

ACQUISITION_STEPS = frozenset({"search", "seed", "snowball", "screen", "fetch", "read", "ground", "queries"})


@dataclass
class GuardState:
    acquisition_stop: str | None
    hard_stop: str | None
    usage: dict


def evaluate(db: ResearchDB, investigation: dict, guards: dict) -> GuardState:
    usage = db.usage(investigation["id"], investigation.get("segment_started_at"))
    reserve = float(guards.get("reserve") or 0)
    acq: str | None = None
    hard: str | None = None

    papers_cap = guards.get("max_total_papers")
    if papers_cap and usage["papers_read"] >= papers_cap:
        acq = f"max_total_papers ({papers_cap}) reached"
    screened_cap = guards.get("max_screened")
    if not acq and screened_cap and usage["screened"] >= screened_cap:
        acq = f"max_screened ({screened_cap}) reached"

    checks = (
        ("max_wall_minutes", usage["active_seconds"] / 60.0, "minutes"),
        ("max_tokens", usage["tokens"], "tokens"),
        ("max_cost_usd", usage["cost"], "USD"),
    )
    for key, value, unit in checks:
        cap = guards.get(key)
        if not cap:
            continue
        if value >= cap:
            hard = hard or f"{key} ({cap} {unit}) reached"
        if value >= cap * (1 - reserve):
            acq = acq or f"{key} reserve reached ({value:.0f} of {cap} {unit})"
    return GuardState(acquisition_stop=acq, hard_stop=hard, usage=usage)


# ---------- case budget (PLAN B17.4, `budget` profile) ----------

# Task step → stage group (llm_calls.step holds the task step name).
STAGE_GROUPS: dict[str, str] = {
    "intake": "intake_safety", "safety": "intake_safety", "questions": "intake_safety",
    "brief_update": "intake_safety",
    "ground": "grounding", "ground_merge": "grounding",
    "plan": "plan", "queries": "plan",
    "screen": "screen",
    "read": "read", "verify": "read", "reread": "read",
    "map": "assess", "confirm": "assess", "fit": "assess", "analysis": "assess", "gaps": "assess",
    "synthesis": "report", "critic": "report", "support": "report", "revise": "report", "final_check": "report",
}
# Acquisition tasks start new work and are skipped when the case budget can't cover them (B17.4).
BUDGET_ACQUISITION = {"ground": "grounding", "screen": "screen", "read": "read"}
FINALIZATION_GROUPS = ("assess", "report")
RETRY_ATTEMPTS = ("repair", "truncation_retry")


def group_usage(db: ResearchDB, investigation_id: int) -> dict:
    """Provider tokens (cached = 0) per stage group, plus `total` and `retry` (repairs and truncation retries)."""
    rows = db.query("SELECT step, attempt_kind, COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS t "
                    "FROM llm_calls WHERE investigation_id = ? AND cached = 0 GROUP BY step, attempt_kind",
                    (investigation_id,))
    out: dict = {"total": 0, "retry": 0}
    for r in rows:
        group = STAGE_GROUPS.get(r["step"] or "", "other")
        out[group] = out.get(group, 0) + int(r["t"])
        out["total"] += int(r["t"])
        if r["attempt_kind"] in RETRY_ATTEMPTS:
            out["retry"] += int(r["t"])
    return out


def protected_unspent(usage: dict, budget: dict) -> int:
    """Tokens the acquisition rule must leave untouched: what is left of the finalization and retry reserves."""
    protected = budget.get("protected") or {}
    final_used = sum(usage.get(g, 0) for g in FINALIZATION_GROUPS)
    return (max(0, int(protected.get("finalization") or 0) - final_used)
            + max(0, int(protected.get("retry") or 0) - usage.get("retry", 0)))


def acquisition_refusal(db: ResearchDB, investigation_id: int, step: str, settings: dict) -> str | None:
    """B17.4 acquisition rule: a grounding, screening or reading task starts only if
    case used + its estimate ≤ case cap − protected reserves still unspent. Returns the skip reason, or None."""
    budget = settings.get("budget") or {}
    group = BUDGET_ACQUISITION.get(step)
    cap = (settings.get("guards") or {}).get("max_tokens")
    if not group or not cap or not (budget.get("targets") or budget.get("protected")):
        return None
    usage = group_usage(db, investigation_id)
    estimate = int((budget.get("task_estimates") or {}).get(step) or 0)
    room = int(cap) - protected_unspent(usage, budget)
    if usage["total"] + estimate > room:
        return f"budget:{group}"
    return None


def over_targets(usage: dict, budget: dict) -> list[str]:
    """Groups past their round-1 target, and the finalization groups past their reserve (B17.4 `budget_over`)."""
    targets = budget.get("targets") or {}
    over = [g for g, t in targets.items() if usage.get(g, 0) > int(t)]
    final = int((budget.get("protected") or {}).get("finalization") or 0)
    if final and sum(usage.get(g, 0) for g in FINALIZATION_GROUPS) > final:
        over.append("finalization")
    return over
