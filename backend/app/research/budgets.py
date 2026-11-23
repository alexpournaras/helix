"""Guards per run segment (PLAN B4 "Guards", B15 `guards`, B1.13 "guards, not budgets").

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
