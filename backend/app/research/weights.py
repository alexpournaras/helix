"""Claim weights and effective links (PLAN B8.19, B8.16), shared by confirm (1E.3) and the matrix (1E.4).

w = strength × design_weight × (1 − bias) × applicability × case_match × size × type × scope × verification
    × agreement × concern

Effective links: for each (claim, hypothesis), a mapping link wins over the extraction link and a neutral link
(either origin) means no link.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import _decode
from .scoring import DESIGN_WEIGHT

DEFAULTS = {"strength": {"weak": 0.5, "moderate": 1, "strong": 1.5},
            "bias": {"low": 0, "some": 0.25, "high": 0.5, "unclear": 0.35},
            "applicability": {"high": 1, "partial": 0.7, "low": 0.4},
            "case_match": {"high": 1, "partial": 0.7, "low": 0.4, "unknown": 0.6},
            "size": {"large": 1, "medium": 0.85, "small": 0.7, "unknown": 0.85},
            "type": {"background": 0.3, "mechanism": 0.8},
            "scope": {"full": 1, "partial": 0.85, "abstract_only": 0.6},
            "partial_verification": 0.6, "single_extractor": 0.9, "concern": 0.8}
NO_SIZE = {"guideline_recommendation", "mechanism", "background"}  # size is n/a → 1


def _w(settings: dict | None) -> dict:
    w = {**DEFAULTS, **((settings or {}).get("weights") or {})}
    return w


def design_weights(settings: dict | None) -> dict:
    return {**DESIGN_WEIGHT, **((settings or {}).get("design_weights") or {})}


def parse_n(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d[\d,]*", text)
    return int(m.group(0).replace(",", "")) if m else None


def size_key(n: int | None) -> str:
    if n is None:
        return "unknown"
    return "large" if n >= 100 else "medium" if n >= 30 else "small"


def claim_weight(claim: dict, link: dict, read: dict, *, settings: dict | None = None,
                 concern: bool = False) -> tuple[float, dict]:
    """(w, parts) for one claim's link to one hypothesis. `read` carries appraisal, applicability, scope, profile
    and the paper's design (`design`)."""
    w = _w(settings)
    design = claim.get("design") or read.get("design") or "other"
    if design == "other" and read.get("design"):
        design = read["design"]
    bias = ((read.get("appraisal") or {}).get("overall_risk")) or "unclear"
    applic = ((read.get("applicability") or {}).get("overall")) or "partial"
    etype = claim.get("evidence_type") or "finding"
    n = claim.get("n") or parse_n(((read.get("profile") or {}).get("n") or {}).get("value"))
    parts = {
        "strength": w["strength"].get(link.get("strength") or "moderate", 1),
        "design": design_weights(settings).get(design, 0.3),
        "bias": 1 - w["bias"].get(bias, 0.35),
        "applicability": w["applicability"].get(applic, 0.7),
        "case_match": w["case_match"].get(link.get("case_match") or "unknown", 0.6),
        "size": 1.0 if etype in NO_SIZE else w["size"][size_key(n)],
        "type": w["type"].get(etype, 1.0),
        "scope": w["scope"].get(read.get("scope") or "full", 1),
        "verification": w["partial_verification"] if claim.get("verification") == "partial" else 1.0,
        "agreement": 1.0 if claim.get("agreement") == 2 else w["single_extractor"],
        "concern": w["concern"] if concern else 1.0,
    }
    total = 1.0
    for v in parts.values():
        total *= float(v)
    return round(total, 4), parts


def effective_links(conn: sqlite3.Connection, investigation_id: int) -> dict[int, dict[int, dict]]:
    """{claim id: {hypothesis id: link}} for active claims; mapping beats extraction; neutral means no link."""
    rows = conn.execute(
        "SELECT l.* FROM claim_links l JOIN evidence_claims c ON c.id = l.claim_id WHERE c.investigation_id = ? AND "
        "c.status = 'active'", (investigation_id,)).fetchall()
    chosen: dict[tuple[int, int], dict] = {}
    for r in rows:
        d = dict(r)
        k = (d["claim_id"], d["hypothesis_id"])
        if k not in chosen or d["origin"] == "mapping":
            chosen[k] = d
    out: dict[int, dict[int, dict]] = {}
    for (cid, hid), d in chosen.items():
        if d["direction"] in ("supports", "contradicts"):
            out.setdefault(cid, {})[hid] = d
    return out


def read_context(conn: sqlite3.Connection, investigation_id: int) -> dict[int, dict]:
    """{read id: {appraisal, applicability, scope, profile, design, concern}} for the weight formula."""
    rows = conn.execute(
        "SELECT r.id, r.appraisal, r.applicability, r.scope, r.profile, s.design, c.concern_flag FROM paper_reads r "
        "LEFT JOIN candidates c ON c.id = r.candidate_id LEFT JOIN screenings s ON s.candidate_id = r.candidate_id "
        "WHERE r.investigation_id = ?", (investigation_id,)).fetchall()
    out = {}
    for r in rows:
        d = _decode(r)
        d["concern"] = bool(d.get("concern_flag"))
        out[d["id"]] = d
    return out


def provisional(conn: sqlite3.Connection, investigation_id: int, settings: dict | None = None) -> list[dict]:
    """[{claim_id, hypothesis_id, direction, strength, weight}] for every active claim's effective links."""
    reads = read_context(conn, investigation_id)
    claims = {r["id"]: _decode(r) for r in conn.execute(
        "SELECT * FROM evidence_claims WHERE investigation_id = ? AND status = 'active'", (investigation_id,))}
    out: list[dict[str, Any]] = []
    for cid, links in effective_links(conn, investigation_id).items():
        c = claims[cid]
        rd = reads.get(c["read_id"], {})
        for hid, link in links.items():
            w, _ = claim_weight(c, link, rd, settings=settings, concern=rd.get("concern", False))
            out.append({"claim_id": cid, "hypothesis_id": hid, "direction": link["direction"],
                        "strength": link.get("strength"), "weight": w})
    return out
