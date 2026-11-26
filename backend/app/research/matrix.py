"""The evidence matrix (PLAN B8.19), pure code: direction and certainty per hypothesis.

Aggregation:
- per paper and direction: w1 + 0.25·(w2 + w3) of its heaviest claims, capped at 1.5·w1;
- per study (unit): the maximum over its papers;
- a primary study included in a review that has evidence on the same hypothesis: × 0.5 (`independence.
  included_in_review_factor`) and not an independent unit.
Direction: supported = S ≥ dominance·C and (≥ min_units independent supporting units, or a guideline / SR unit
contributing ≥ strong_unit_min); contradicted mirrors it; insufficient = fewer than min_units units and no
guideline / SR unit; otherwise mixed.
Certainty (n/a when insufficient): start from the driving unit (the heaviest in the dominant direction): guideline,
SR of RCTs or RCT → high; SR of other designs, cohort, case-control, diagnostic → low; else very low. Downgrade one
level each for bias, inconsistency, indirectness and imprecision (B8.19). No upgrades.
Everything is deterministic: the same inputs give the same ratings.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import weights as W
from .db import _decode

LEVELS = ["very_low", "low", "moderate", "high"]
REVIEWS = ("guideline", "review_sr")
HIGH_START = {"rct"}
LOW_START = {"cohort", "case_control", "diagnostic_accuracy"}


def _cfg(settings: dict | None) -> dict:
    s = settings or {}
    return {"dominance_ratio": 2, "min_units": 2, "strong_unit_min": 0.5, **(s.get("direction") or {}),
            **{f"cert_{k}": v for k, v in {"bias_share": 0.5, "inconsistency_ratio": 0.5, "indirectness_below": 0.6,
                                           "imprecision_min_participants": 200, **(s.get("certainty") or {})}.items()},
            "review_factor": float(((s.get("independence") or {}).get("included_in_review_factor")) or 0.5)}


@dataclass
class Item:
    """One claim's effective link to one hypothesis, weighted."""
    claim_id: int
    read_id: int
    direction: str
    weight: float
    directness: float  # applicability × case match
    n: int | None


@dataclass
class Data:
    items: dict[int, list[Item]]  # hypothesis id → items
    study_of: dict[int, int]  # read id → study id
    kind: dict[int, str]  # study id → kind
    design: dict[int, str]  # read id → design
    risk: dict[int, str]  # read id → overall risk of bias
    sr_of_rcts: dict[int, bool]  # read id → a systematic review of RCTs
    included_in: dict[int, set[int]] = field(default_factory=dict)  # primary study → reviews including it


def load(conn: sqlite3.Connection, investigation_id: int, settings: dict | None = None) -> Data:
    reads = W.read_context(conn, investigation_id)
    claims = {r["id"]: _decode(r) for r in conn.execute(
        "SELECT * FROM evidence_claims WHERE investigation_id = ? AND status = 'active'", (investigation_id,))}
    w = W._w(settings)
    items: dict[int, list[Item]] = {}
    for cid, links in W.effective_links(conn, investigation_id).items():
        c = claims[cid]
        rd = reads.get(c["read_id"], {})
        for hid, link in links.items():
            weight, parts = W.claim_weight(c, link, rd, settings=settings, concern=rd.get("concern", False))
            n = c.get("n") or W.parse_n(((rd.get("profile") or {}).get("n") or {}).get("value"))
            items.setdefault(hid, []).append(Item(cid, c["read_id"], link["direction"], weight,
                                                  parts["applicability"] * parts["case_match"], n))
    study_of: dict[int, int] = {}
    kind: dict[int, str] = {}
    for r in conn.execute("SELECT s.id, s.kind, m.read_id FROM studies s JOIN study_members m ON m.study_id = s.id "
                          "WHERE s.investigation_id = ?", (investigation_id,)):
        study_of[r["read_id"]] = r["id"]
        kind[r["id"]] = r["kind"]
    for rid, rd in reads.items():  # a read not grouped yet is its own study
        if rid not in study_of:
            study_of[rid] = -rid
            design = rd.get("design")
            kind[-rid] = {"guideline": "guideline", "systematic_review": "review_sr",
                          "narrative_review": "review_narrative"}.get(design or "", "primary")
    included: dict[int, set[int]] = {}
    for r in conn.execute("SELECT i.review_study_id, i.included_study_id FROM review_inclusions i JOIN studies s ON "
                          "s.id = i.review_study_id WHERE s.investigation_id = ?", (investigation_id,)):
        included.setdefault(r["included_study_id"], set()).add(r["review_study_id"])
    designs = {rid: (rd.get("design") or "other") for rid, rd in reads.items()}
    risk = {rid: ((rd.get("appraisal") or {}).get("overall_risk") or "unclear") for rid, rd in reads.items()}
    sr_rct = {}
    for rid, rd in reads.items():
        inc = (((rd.get("profile") or {}).get("review_included_designs") or {}).get("value") or "").lower()
        sr_rct[rid] = "random" in inc or "rct" in inc
    return Data(items, study_of, kind, designs, risk, sr_rct, included)


def paper_weight(ws: list[float]) -> float:
    ws = sorted(ws, reverse=True)
    if not ws:
        return 0.0
    w1 = ws[0]
    return min(w1 + 0.25 * sum(ws[1:3]), 1.5 * w1)


@dataclass
class Unit:
    study: int
    kind: str
    weight: float
    read: int  # the paper giving the unit its weight
    independent: bool = True
    n: int | None = None
    directness: list[float] = field(default_factory=list)


def units(items: list[Item], direction: str, data: Data, factor: float) -> list[Unit]:
    by_read: dict[int, list[Item]] = {}
    for it in items:
        if it.direction == direction:
            by_read.setdefault(it.read_id, []).append(it)
    by_study: dict[int, Unit] = {}
    for rid, its in sorted(by_read.items()):
        sid = data.study_of.get(rid, -rid)
        w = paper_weight([i.weight for i in its])
        u = by_study.setdefault(sid, Unit(sid, data.kind.get(sid, "primary"), -1.0, rid))
        if w > u.weight:  # the study counts as its best paper (same cohort counted once)
            u.weight, u.read = w, rid
        ns = [i.n for i in its if i.n] + ([u.n] if u.n else [])
        u.n = max(ns) if ns else None
        u.directness += [i.directness for i in its]
    # A primary study included in a review that has evidence on this hypothesis: half weight, not independent.
    reviews_here = {i.read_id for i in items}
    review_studies = {data.study_of.get(r, -r) for r in reviews_here
                      if data.kind.get(data.study_of.get(r, -r)) in ("review_sr", "review_narrative", "guideline")}
    for u in by_study.values():
        if u.kind == "primary" and data.included_in.get(u.study, set()) & review_studies:
            u.weight *= factor
            u.independent = False
    return sorted(by_study.values(), key=lambda u: (-u.weight, u.study))


def _start(u: Unit, data: Data) -> str:
    if u.kind == "guideline" or (u.kind == "review_sr" and data.sr_of_rcts.get(u.read)) \
            or data.design.get(u.read) in HIGH_START:
        return "high"
    if u.kind == "review_sr" or data.design.get(u.read) in LOW_START:
        return "low"
    return "very_low"


def rate(items: list[Item], data: Data, settings: dict | None = None) -> dict:
    cfg = _cfg(settings)
    sup = units(items, "supports", data, cfg["review_factor"])
    con = units(items, "contradicts", data, cfg["review_factor"])
    S = round(sum(u.weight for u in sup), 4)
    C = round(sum(u.weight for u in con), 4)
    all_units = {u.study for u in sup} | {u.study for u in con}
    strong = float(cfg["strong_unit_min"])

    def holds(mine: list[Unit], mine_w: float, other_w: float) -> bool:
        independent = sum(1 for u in mine if u.independent and u.weight > 0)
        anchor = any(u.kind in REVIEWS and u.weight >= strong for u in mine)
        return mine_w > 0 and mine_w >= cfg["dominance_ratio"] * other_w and (independent >= cfg["min_units"] or anchor)
    if holds(sup, S, C):
        direction = "supported"
    elif holds(con, C, S):
        direction = "contradicted"
    elif len(all_units) < cfg["min_units"] and not any(u.kind in REVIEWS for u in sup + con):
        direction = "insufficient"
    else:
        direction = "mixed"
    out = {"direction": direction, "S": S, "C": C,
           "units": {"supports": [_unit_dict(u) for u in sup], "contradicts": [_unit_dict(u) for u in con]},
           "certainty": None, "start": None, "downgrades": []}
    if direction == "insufficient":
        return out
    if direction == "supported":
        dominant, opposing = sup, C
    elif direction == "contradicted":
        dominant, opposing = con, S
    else:  # mixed: the heavier side drives
        dominant, opposing = (sup, C) if S >= C else (con, S)
    driving = dominant[0] if dominant else (sup + con)[0]
    level = LEVELS.index(_start(driving, data))
    out["start"] = LEVELS[level]
    dom_w = sum(u.weight for u in dominant)
    downgrades = []
    risky = sum(u.weight for u in dominant if data.risk.get(u.read) in ("high", "unclear"))
    if dom_w and risky > cfg["cert_bias_share"] * dom_w:
        downgrades.append("bias")
    if direction == "mixed" or opposing > cfg["cert_inconsistency_ratio"] * dom_w:
        downgrades.append("inconsistency")
    direct = [d for u in dominant for d in u.directness]
    if direct and sum(direct) / len(direct) < cfg["cert_indirectness_below"]:
        downgrades.append("indirectness")
    participants = sum(u.n or 0 for u in dominant)
    if participants < cfg["cert_imprecision_min_participants"] or len(dominant) < cfg["min_units"]:
        downgrades.append("imprecision")
    out["downgrades"] = downgrades
    out["certainty"] = LEVELS[max(0, level - len(downgrades))]
    return out


def _unit_dict(u: Unit) -> dict:
    return {"study": u.study, "kind": u.kind, "weight": round(u.weight, 4), "read": u.read,
            "independent": u.independent, "n": u.n}


def case_fit(fits: list[str], n_features: int) -> str:
    """B8.19 case fit from the feature ratings (explains, compatible, against, unknown)."""
    E, A, U = fits.count("explains"), fits.count("against"), fits.count("unknown")
    F = n_features
    if A == 0 and E >= max(1, F / 3):
        return "good"
    if A >= 2 or A > E:
        return "poor"
    if F and U >= F / 2:
        return "unknown"
    return "partial"
