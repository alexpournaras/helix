"""matrix (PLAN B8.19, code), fit (B8.20) and analysis (B8.21).

matrix:r{round}  rates every active hypothesis from the active claims' effective links (`matrix.rate`): direction,
                 certainty, the units and downgrades behind them (`hypotheses.ratings`). A hypothesis whose rating
                 changed gets `evidence_version + 1`, so fit and analysis rerun only for it. An `evidence_matrix` note.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import ensemble, llm, matrix as M, weights as W
from ..db import _decode, now
from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import FitSet, HypothesisAnalysis
from . import safety


async def run_matrix(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])

    def commit(conn: sqlite3.Connection) -> None:
        data = M.load(conn, inv["id"], tc.settings)
        counts: dict[str, int] = {"changed": 0}
        for h in [_decode(r) for r in conn.execute("SELECT * FROM hypotheses WHERE investigation_id = ? AND "
                                                   "status = 'active' ORDER BY id", (inv["id"],))]:
            rating = M.rate(data.items.get(h["id"], []), data, tc.settings)
            counts[rating["direction"]] = counts.get(rating["direction"], 0) + 1
            if (h.get("ratings") or None) != rating:
                counts["changed"] += 1
                conn.execute("UPDATE hypotheses SET direction = ?, certainty = ?, ratings = ?, "
                             "evidence_version = evidence_version + 1 WHERE id = ?",
                             (rating["direction"], rating["certainty"], json.dumps(rating), h["id"]))
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "evidence_matrix",
                                           "artifact_key": f"matrix:r{rnd}", "created_at": now()}, or_ignore=True)
        tc.db.event(inv["id"], "matrix", f"r{rnd}", stage="matrix", conn=conn, data={"round": rnd, **counts})
    return commit


def matrix_stage(next_stage: str = "fitting") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("matrix", f"matrix:r{int(sc.inv['round'])}", {}, priority=95)
    return StageDef("matrix", on_enter=on_enter, on_complete=lambda sc: next_stage)


MATRIX_STEP = StepDef("matrix", run_matrix, code_step=True)


# ---------- fit and analysis (B8.20, B8.21) ----------

FIT_VERSION = "fit.v1"
ANALYSIS_VERSION = "analysis.v1"
MAX_CLAIMS = 40


def fit_limit(settings: dict | None) -> int:
    """Claims shown to fit and analysis (`fit.max_claims`; 20 in the budget profile, B17.2)."""
    return int((((settings or {}).get("fit") or {}).get("max_claims")) or MAX_CLAIMS)
NEEDS_CLAIM = {"evidence", "inference", "recommendation"}


def basis(h: dict, feature_keys: list[str]) -> dict:
    """What fit and analysis depend on: rerun only when it changes (B8.20)."""
    return {"evidence_version": int(h.get("evidence_version") or 0), "features": sorted(feature_keys)}


def previous_analysis(conn: sqlite3.Connection, investigation_id: int, hypothesis_id: int, rnd: int) -> dict | None:
    row = conn.execute("SELECT * FROM hypothesis_analyses WHERE investigation_id = ? AND hypothesis_id = ? AND "
                       "round < ? ORDER BY round DESC LIMIT 1", (investigation_id, hypothesis_id, rnd)).fetchone()
    return _decode(row) if row else None


def top_claims(conn: sqlite3.Connection, investigation_id: int, hypothesis_id: int, settings: dict | None,
               limit: int = MAX_CLAIMS) -> list[dict]:
    """The hypothesis's active claims by provisional weight (heaviest first), with their effective direction."""
    prov = sorted((p for p in W.provisional(conn, investigation_id, settings) if p["hypothesis_id"] == hypothesis_id),
                  key=lambda p: (-p["weight"], p["claim_id"]))[:limit]
    out = []
    for p in prov:
        c = _decode(conn.execute("SELECT * FROM evidence_claims WHERE id = ?", (p["claim_id"],)).fetchone())
        out.append({**c, "direction": p["direction"], "weight": p["weight"]})
    return out


def claims_block(claims: list[dict]) -> list[str]:
    return [f"C{n}: [{c['direction']}, weight {c['weight']:.2f}, {c.get('verification')}, agreement "
            f"{c.get('agreement')}] {c['claim']} (population: {c.get('population') or 'not stated'}; design: "
            f"{c.get('design') or 'unknown'})" for n, c in enumerate(claims, 1)]


def hypothesis_block(h: dict) -> dict:
    r = h.get("ratings") or {}
    return {"key": h["key"], "title": h["title"], "description": h.get("description"),
            "mechanism": h.get("mechanism"), "kind": h.get("kind"), "direction": h.get("direction"),
            "certainty": h.get("certainty"), "S": r.get("S"), "C": r.get("C"), "downgrades": r.get("downgrades")}


def settle_fit(items, feature_keys: list[str], refs: dict[str, int]) -> tuple[list[dict], dict]:
    """Exactly one row per feature: unknown keys and repeats dropped, missing features `unknown`, `explains` /
    `against` without a backing claim downgraded (to compatible / unknown)."""
    rows: dict[str, dict] = {}
    log = {"dropped": 0, "missing": 0, "downgraded": 0}
    for it in items:
        if it.feature_key not in feature_keys or it.feature_key in rows:
            log["dropped"] += 1
            continue
        ids = [refs[c] for c in it.claim_ids if c in refs]
        fit = it.fit
        if fit in ("explains", "against") and not ids:
            fit = "compatible" if fit == "explains" else "unknown"
            log["downgraded"] += 1
        rows[it.feature_key] = {"feature_key": it.feature_key, "fit": fit, "claim_ids": ids, "reason": it.reason}
    for k in feature_keys:
        if k not in rows:
            rows[k] = {"feature_key": k, "fit": "unknown", "claim_ids": [], "reason": "not rated"}
            log["missing"] += 1
    return [rows[k] for k in feature_keys], log


async def run_fit(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])
    hid = int(tc.payload["hypothesis_id"])
    with tc.db.read() as conn:
        h = _decode(conn.execute("SELECT * FROM hypotheses WHERE id = ?", (hid,)).fetchone())
        found = safety.current_brief(conn, inv["id"])
        claims = top_claims(conn, inv["id"], hid, tc.settings, fit_limit(tc.settings))
    features = found[1].case_features if found else []
    keys = [f.key for f in features]
    rows: list[dict] = []
    log: dict = {}
    if keys:
        refs = {f"C{n}": c["id"] for n, c in enumerate(claims, 1)}
        body = "\n".join([f"Hypothesis: {json.dumps(hypothesis_block(h), ensure_ascii=False)}", "", "Case features:",
                          *[f"- {f.key}: {f.text}" for f in features], "", "Claims:",
                          *(claims_block(claims) or ["(none found in the searched sources)"])])
        ctx = tc.services.with_task(task_id=tc.task["id"], step="fit", investigation_id=inv["id"])
        m = await ensemble.single(ctx, "fit", FitSet, llm.base_system() + "\n\n" + llm.prompt(FIT_VERSION),
                                  llm.material(body, kind="fit"), prompt_version=FIT_VERSION)
        rows, log = settle_fit(m.value.items, keys, refs)  # type: ignore[union-attr]
    fit = M.case_fit([r["fit"] for r in rows], len(keys)) if keys else "unknown"

    def commit(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM feature_fit WHERE investigation_id = ? AND round = ? AND hypothesis_id = ?",
                     (inv["id"], rnd, hid))
        for r in rows:
            conn.execute("INSERT INTO feature_fit (investigation_id, round, feature_key, hypothesis_id, fit, claim_ids, "
                         "reason) VALUES (?, ?, ?, ?, ?, ?, ?)", (inv["id"], rnd, r["feature_key"], hid, r["fit"],
                                                                 json.dumps(r["claim_ids"]), r["reason"][:300]))
        conn.execute("UPDATE hypotheses SET case_fit = ? WHERE id = ?", (fit, hid))
        tc.db.event(inv["id"], "fit", f"r{rnd}:h{hid}", stage="fitting", conn=conn, data={
            "hypothesis_id": hid, "features": len(keys), "claims": len(claims), "case_fit": fit, **log})
    return commit


def statement_problem(s, refs: set[str], segs: set[str]) -> str | None:
    """B8.24 citation rules for an analysis statement (general items are report-only)."""
    bad = [c for c in s.claim_ids if c not in refs] + [x for x in s.seg_ids if x not in segs]
    if bad:
        return f"unknown IDs {', '.join(bad)}"
    if s.kind in NEEDS_CLAIM and not s.claim_ids:
        return f"a {s.kind} statement must cite a claim"
    if s.kind == "case_fact" and not s.seg_ids:
        return "a case_fact statement must cite the person's segments"
    if s.kind == "general":
        return "general statements belong in the report, not the analysis"
    return None


def analysis_validator(refs: set[str], segs: set[str]):
    """Strict on the first answer (one repair), lenient after: failing statements are then dropped by code."""
    seen = {"n": 0}

    def check(a: HypothesisAnalysis) -> None:
        seen["n"] += 1
        if seen["n"] > 1:
            return
        problems = [f"statement {i + 1}: {p}" for i, s in enumerate(a.statements)
                    if (p := statement_problem(s, refs, segs))]
        bad = [c for c in a.strongest_support + a.strongest_contra if c not in refs]
        if bad:
            problems.append(f"unknown claim IDs {', '.join(bad)}")
        if problems:
            raise ValueError("; ".join(problems))
    return check


async def run_analysis(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])
    hid = int(tc.payload["hypothesis_id"])
    with tc.db.read() as conn:
        h = _decode(conn.execute("SELECT * FROM hypotheses WHERE id = ?", (hid,)).fetchone())
        found = safety.current_brief(conn, inv["id"])
        segs = safety.segments(conn, inv["id"])
        claims = top_claims(conn, inv["id"], hid, tc.settings, fit_limit(tc.settings))
        fits = [dict(r) for r in conn.execute("SELECT feature_key, fit FROM feature_fit WHERE investigation_id = ? AND "
                                              "round = ? AND hypothesis_id = ?", (inv["id"], rnd, hid))]
    features = {f.key: f.text for f in (found[1].case_features if found else [])}
    refs = {f"C{n}": c["id"] for n, c in enumerate(claims, 1)}
    seg_ids = {s["seg_id"] for s in segs}
    body = "\n".join([
        f"Hypothesis: {json.dumps(hypothesis_block(h), ensure_ascii=False)}", "", "Case features and fit:",
        *[f"- {f['feature_key']} ({features.get(f['feature_key'], '')}): {f['fit']}" for f in fits], "",
        "Person's segments:", *[f"{s['seg_id']}: {safety.person_text(s)}" for s in segs], "", "Claims:",
        *(claims_block(claims) or ["(none found in the searched sources)"])])
    ctx = tc.services.with_task(task_id=tc.task["id"], step="analysis", investigation_id=inv["id"])
    m = await ensemble.single(ctx, "analysis", HypothesisAnalysis,
                              llm.base_system() + "\n\n" + llm.prompt(ANALYSIS_VERSION),
                              llm.material(body, kind="analysis"), prompt_version=ANALYSIS_VERSION,
                              validate=analysis_validator(set(refs), seg_ids))
    a: HypothesisAnalysis = m.value  # type: ignore[assignment]
    kept, dropped = [], 0
    for s in a.statements:
        if statement_problem(s, set(refs), seg_ids):
            dropped += 1
            continue
        kept.append({"text": s.text, "kind": s.kind, "claim_ids": [refs[c] for c in s.claim_ids],
                     "seg_ids": s.seg_ids})
    data = {"basis": basis(h, list(features)), "statements": kept,
            "strongest_support": [refs[c] for c in a.strongest_support if c in refs],
            "strongest_contra": [refs[c] for c in a.strongest_contra if c in refs],
            "open_questions": a.open_questions, "dropped": dropped}

    def commit(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT OR REPLACE INTO hypothesis_analyses (investigation_id, hypothesis_id, round, data, copied) "
                     "VALUES (?, ?, ?, ?, 0)", (inv["id"], hid, rnd, json.dumps(data)))
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "hypothesis_analysis",
                                           "artifact_key": f"analysis:r{rnd}:h{hid}", "created_at": now()},
                     or_ignore=True)
        tc.db.event(inv["id"], "analysis", f"r{rnd}:h{hid}", stage="analyzing", conn=conn, data={
            "hypothesis_id": hid, "statements": len(kept), "dropped": dropped, "claims": len(claims)})
    return commit


def _active(sc: StageContext) -> list[dict]:
    return [_decode(r) for r in sc.conn.execute("SELECT * FROM hypotheses WHERE investigation_id = ? AND "
                                                "status = 'active' ORDER BY id", (sc.id,))]


def _feature_keys(sc: StageContext) -> list[str]:
    found = safety.current_brief(sc.conn, sc.id)
    return [f.key for f in found[1].case_features] if found else []


def fitting_stage(next_stage: str = "analyzing") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        rnd = int(sc.inv["round"])
        keys = _feature_keys(sc)
        for h in _active(sc):
            prev = previous_analysis(sc.conn, sc.id, h["id"], rnd)
            if prev and prev["data"].get("basis") == basis(h, keys):
                # Unchanged evidence and features: copy fit and analysis forward (copied = 1).
                for r in sc.conn.execute("SELECT * FROM feature_fit WHERE investigation_id = ? AND round = ? AND "
                                         "hypothesis_id = ?", (sc.id, prev["round"], h["id"])).fetchall():
                    sc.conn.execute("INSERT OR REPLACE INTO feature_fit (investigation_id, round, feature_key, "
                                    "hypothesis_id, fit, claim_ids, reason, copied) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                                    (sc.id, rnd, r["feature_key"], h["id"], r["fit"], r["claim_ids"], r["reason"]))
                sc.conn.execute("INSERT OR REPLACE INTO hypothesis_analyses (investigation_id, hypothesis_id, round, "
                                "data, copied) VALUES (?, ?, ?, ?, 1)",
                                (sc.id, h["id"], rnd, json.dumps(prev["data"])))
                continue
            sc.queue("fit", f"fit:r{rnd}:{h['key']}", {"hypothesis_id": h["id"]}, priority=95)
    return StageDef("fitting", on_enter=on_enter, on_complete=lambda sc: next_stage, skip_check_steps=("fit",))


def analyzing_stage(next_stage: str = "gaps") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        rnd = int(sc.inv["round"])
        for h in _active(sc):
            done = sc.conn.execute("SELECT 1 FROM hypothesis_analyses WHERE investigation_id = ? AND hypothesis_id = ? "
                                   "AND round = ?", (sc.id, h["id"], rnd)).fetchone()
            if not done:
                sc.queue("analysis", f"analysis:r{rnd}:{h['key']}", {"hypothesis_id": h["id"]}, priority=95)
    return StageDef("analyzing", on_enter=on_enter, on_complete=lambda sc: next_stage,
                    skip_check_steps=("analysis",))


FIT_STEP = StepDef("fit", run_fit, on_fail="skip")
ANALYSIS_STEP = StepDef("analysis", run_analysis, on_fail="skip")
