"""gaps (PLAN B8.22): code-computed gaps, the gaps panel, merging, novelty and the round decision.

gaps:r{round}
  computed  unmet quotas, hypotheses supported only by weak links, features unknown for every hypothesis, abandoned
            queries, uncovered features, differential items excluded on weak grounds (no case segment cited),
            grounding gaps, contradiction pairs (opposite directions on one hypothesis from different studies);
  panel     `gaps` (A + B; `ensembles.gaps_panel` off → one) → GapReport each;
  merge     claim IDs must exist; a new hypothesis cites ≥ 1 claim, at most `loop.max_new_hypotheses_per_round`,
            those both members proposed first; concepts already covered by a hypothesis or knowledge item are
            dropped; queries go through the B8.7 checks (allowlists, sources, no repeats);
  decision  novelty = rating changes + accepted concepts + accepted hypotheses (+ snowball candidates, M2);
            new_work = accepted queries + accepted hypotheses (+ snowball, M2);
            continue = round < first + max_rounds − 1 and not acquisition_stopped and novelty ≥ min_novelty and
            new_work. M1 is reporting only: the result is recorded and the run goes on to synthesis.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import claims as CL, ensemble, llm, weights as W
from ..db import _decode
from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import GapReport
from . import queries as Q, safety
from .screen import case_summary

GAPS_VERSION = "gaps.v1"
MAX_CLAIMS = 60
M1_REPORTING_ONLY = True


# ---------- computed ----------

def computed(conn: sqlite3.Connection, inv: dict, settings: dict | None) -> dict:
    iid, rnd = inv["id"], int(inv["round"])
    hyps = [_decode(r) for r in conn.execute("SELECT * FROM hypotheses WHERE investigation_id = ? AND status = 'active' "
                                             "ORDER BY id", (iid,))]
    quota = int((((settings or {}).get("selection") or {}).get("min_papers_per_hypothesis")) or 3)
    have: dict[str, int] = {}
    for r in conn.execute("SELECT s.hypothesis_keys FROM screenings s JOIN candidates c ON c.id = s.candidate_id WHERE "
                          "c.investigation_id = ? AND s.selected_round IS NOT NULL", (iid,)):
        for k in json.loads(r["hypothesis_keys"] or "[]"):
            have[k] = have.get(k, 0) + 1
    prov = W.provisional(conn, iid, settings)
    weak_only = []
    for h in hyps:
        sup = [p for p in prov if p["hypothesis_id"] == h["id"] and p["direction"] == "supports"]
        if sup and all(p["strength"] == "weak" for p in sup):
            weak_only.append(h["id"])
    fits: dict[str, set[str]] = {}
    for r in conn.execute("SELECT feature_key, fit FROM feature_fit WHERE investigation_id = ? AND round = ?",
                          (iid, rnd)):
        fits.setdefault(r["feature_key"], set()).add(r["fit"])
    study_of = {r["read_id"]: r["study_id"] for r in conn.execute(
        "SELECT m.read_id, m.study_id FROM study_members m JOIN studies s ON s.id = m.study_id WHERE "
        "s.investigation_id = ?", (iid,))}
    reads = {r["id"]: r["read_id"] for r in conn.execute("SELECT id, read_id FROM evidence_claims WHERE "
                                                         "investigation_id = ? AND status = 'active'", (iid,))}
    pairs = []
    for h in hyps:
        sup = sorted((p for p in prov if p["hypothesis_id"] == h["id"] and p["direction"] == "supports"),
                     key=lambda p: -p["weight"])
        con = sorted((p for p in prov if p["hypothesis_id"] == h["id"] and p["direction"] == "contradicts"),
                     key=lambda p: -p["weight"])
        for s in sup[:3]:
            for c in con[:3]:
                rs, rc = reads.get(s["claim_id"]), reads.get(c["claim_id"])
                if study_of.get(rs, -(rs or 0)) != study_of.get(rc, -(rc or 0)):
                    pairs.append({"hypothesis_id": h["id"], "supports": s["claim_id"], "contradicts": c["claim_id"]})
    return {
        "unmet_quotas": [h["id"] for h in hyps if have.get(h["key"], 0) < quota],
        "weak_only": weak_only,
        "features_unknown_everywhere": sorted(k for k, v in fits.items() if v == {"unknown"}),
        "abandoned_queries": [r["id"] for r in conn.execute(
            "SELECT id FROM research_queries WHERE investigation_id = ? AND purpose = 'research' AND status IN "
            "('dropped', 'failed')", (iid,))],
        "uncovered_features": [r["feature_key"] for r in conn.execute(
            "SELECT feature_key FROM plan_uncovered WHERE investigation_id = ?", (iid,))],
        "weak_exclusions": [r["item_id"] for r in conn.execute(
            "SELECT item_id FROM knowledge_coverage WHERE investigation_id = ? AND decision = 'excluded' AND "
            "(seg_ids IS NULL OR seg_ids = '[]')", (iid,))],
        "grounding_gaps": [r["complaint_key"] for r in conn.execute(
            "SELECT complaint_key FROM grounding_runs WHERE investigation_id = ? AND status IN ('gap', 'failed')",
            (iid,))],
        "contradiction_pairs": pairs[:10],
    }


# ---------- merge ----------

def merge(reports: list[GapReport], *, refs: dict[str, int], hyp_keys: set[str], covered: list[str],
          allowed_sources: list[str], earlier_queries: set[tuple[str, str]], max_new: int) -> dict:
    """Merge the panel's reports (B8.22). Returns accepted items (claim refs → IDs) and drop counts."""
    dropped = {"bad_ids": 0, "hypothesis_without_claim": 0, "hypothesis_over_limit": 0, "concept_covered": 0,
               "query_checks": 0, "query_repeat": 0, "query_unknown_hypothesis": 0}

    def ids(refs_in: list[str]) -> list[int] | None:
        if any(r not in refs for r in refs_in):
            return None
        return [refs[r] for r in refs_in]
    contradictions, concepts, missing, hyps, queries = [], [], [], [], []
    seen_terms: set[str] = set()
    proposals: dict[str, dict] = {}
    for n, rep in enumerate(reports):
        for c in rep.contradictions:
            got = ids(c.claim_ids)
            if got is None:
                dropped["bad_ids"] += 1
            elif sorted(got) not in [sorted(x["claim_ids"]) for x in contradictions]:
                contradictions.append({"claim_ids": got, "note": c.note})
        for c in rep.new_concepts:
            got = ids(c.claim_ids)
            term = " ".join(CL.tokens(c.term))
            if got is None:
                dropped["bad_ids"] += 1
            elif not term or term in seen_terms or any(CL.jaccard(c.term, x) >= 0.5 or term in " ".join(CL.tokens(x))
                                                       for x in covered):
                dropped["concept_covered"] += 1
            else:
                seen_terms.add(term)
                concepts.append({"term": c.term, "reason": c.reason, "claim_ids": got})
        for m in rep.missing_information:
            if all(CL.jaccard(m.text, x["text"]) < 0.5 for x in missing):
                missing.append({"text": m.text, "reason": m.reason})
        for h in rep.new_hypotheses:
            got = ids(h.claim_ids)
            if got is None:
                dropped["bad_ids"] += 1
                continue
            if not got:
                dropped["hypothesis_without_claim"] += 1
                continue
            if h.key in hyp_keys or any(CL.jaccard(h.title, x) >= 0.5 for x in covered):
                dropped["concept_covered"] += 1
                continue
            match = next((k for k, p in proposals.items() if k == h.key or CL.jaccard(p["title"], h.title) >= 0.5), None)
            if match is None:
                proposals[h.key] = {"key": h.key, "title": h.title, "description": h.description, "claim_ids": got,
                                    "members": {n}}
            else:
                proposals[match]["members"].add(n)
                proposals[match]["claim_ids"] = sorted(set(proposals[match]["claim_ids"]) | set(got))
    ranked = sorted(proposals.values(), key=lambda p: (-len(p["members"]), -len(p["claim_ids"]), p["key"]))
    for p in ranked:
        if len(hyps) >= max_new:
            dropped["hypothesis_over_limit"] += 1
            continue
        hyps.append({"key": p["key"], "title": p["title"], "description": p["description"],
                     "claim_ids": p["claim_ids"], "proposed_by": len(p["members"])})
    new_keys = {h["key"] for h in hyps}
    taken = set(earlier_queries)
    for rep in reports:
        for q in rep.follow_up_queries:
            if q.hypothesis_key not in hyp_keys | new_keys:
                dropped["query_unknown_hypothesis"] += 1
                continue
            if q.source not in allowed_sources or Q.check_query(q.source, q.query):
                dropped["query_checks"] += 1
                continue
            k = (q.source, Q.norm(q.query))
            if k in taken:
                dropped["query_repeat"] += 1
                continue
            taken.add(k)
            queries.append(q.model_dump(mode="json"))
    return {"contradictions": contradictions, "new_concepts": concepts, "missing_information": missing,
            "new_hypotheses": hyps, "follow_up_queries": queries, "dropped": dropped}


def decide(*, rnd: int, first: int, max_rounds: int, acquisition_stopped: bool, novelty: int, min_novelty: int,
           new_work: int) -> bool:
    return rnd < first + max_rounds - 1 and not acquisition_stopped and novelty >= min_novelty and new_work > 0


# ---------- the step ----------

def _loop(settings: dict | None) -> dict:
    return {"max_rounds": 1, "min_novelty": 1, "max_new_hypotheses_per_round": 4,
            **((settings or {}).get("loop") or {})}


async def run(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])
    settings = tc.settings or {}
    with tc.db.read() as conn:
        comp = computed(conn, inv, settings)
        hyps = [_decode(r) for r in conn.execute("SELECT * FROM hypotheses WHERE investigation_id = ? AND "
                                                 "status = 'active' ORDER BY id", (inv["id"],))]
        prov = W.provisional(conn, inv["id"], settings)
        best: dict[int, float] = {}
        for p in prov:
            best[p["claim_id"]] = max(best.get(p["claim_id"], 0), p["weight"])
        top = [cid for cid, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))][:MAX_CLAIMS]
        claims = [_decode(conn.execute("SELECT id, claim, population, design FROM evidence_claims WHERE id = ?",
                                       (c,)).fetchone()) for c in top]
        items = [r["text"] for r in conn.execute("SELECT text FROM knowledge_items WHERE investigation_id = ? AND "
                                                 "status = 'active'", (inv["id"],))]
        earlier = {(r["source"], Q.norm(r["query"])) for r in conn.execute(
            "SELECT source, query FROM research_queries WHERE investigation_id = ?", (inv["id"],))}
        changed = conn.execute("SELECT data FROM events WHERE investigation_id = ? AND kind = 'matrix' ORDER BY id "
                               "DESC LIMIT 1", (inv["id"],)).fetchone()
        found = safety.current_brief(conn, inv["id"])
    refs = {f"C{n}": c["id"] for n, c in enumerate(claims, 1)}
    hid_key = {h["id"]: h["key"] for h in hyps}
    body = {
        "case": case_summary(found[1], inv) if found else {},
        "hypotheses": [{"key": h["key"], "title": h["title"], "kind": h.get("kind"), "direction": h.get("direction"),
                        "certainty": h.get("certainty"), "case_fit": h.get("case_fit")} for h in hyps],
        "computed_gaps": {**comp, "unmet_quotas": [hid_key.get(i) for i in comp["unmet_quotas"]],
                          "weak_only": [hid_key.get(i) for i in comp["weak_only"]]},
        "claims": [f"C{n}: {c['claim']} (population: {c.get('population') or 'not stated'}; design: "
                   f"{c.get('design') or 'unknown'})" for n, c in enumerate(claims, 1)],
    }
    material = llm.material(json.dumps(body, ensure_ascii=False, indent=1), kind="gaps")
    system = llm.base_system() + "\n\n" + llm.prompt(GAPS_VERSION)
    ctx = tc.services.with_task(task_id=tc.task["id"], step="gaps", investigation_id=inv["id"])
    if ((settings.get("ensembles") or {}).get("gaps_panel", True)):
        result = await ensemble.panel(ctx, "gaps", GapReport, system, material, prompt_version=GAPS_VERSION)
        reports, independence = result.values, result.independence
    else:
        m = await ensemble.single(ctx, "gaps", GapReport, system, material, prompt_version=GAPS_VERSION)
        reports, independence = [m.value], "single"
    loop = _loop(settings)
    merged = merge(reports, refs=refs, hyp_keys={h["key"] for h in hyps}, covered=[h["title"] for h in hyps] + items,
                   allowed_sources=Q.sources(Q._cfg(settings)), earlier_queries=earlier,
                   max_new=int(loop["max_new_hypotheses_per_round"]))
    rating_changes = int(((_decode(changed)["data"] or {}).get("changed") or 0)) if changed else 0
    novelty = {"rating_changes": rating_changes, "concepts": len(merged["new_concepts"]),
               "hypotheses": len(merged["new_hypotheses"]), "snowball": 0}
    novelty["total"] = sum(novelty.values())
    new_work = len(merged["follow_up_queries"]) + len(merged["new_hypotheses"])
    first = 1  # the segment's first round (M2 follow-ups start later)
    would = decide(rnd=rnd, first=first, max_rounds=int(loop["max_rounds"]),
                   acquisition_stopped=bool(tc.payload.get("acquisition_stopped")), novelty=novelty["total"],
                   min_novelty=int(loop["min_novelty"]), new_work=new_work)
    decision = "stop" if (M1_REPORTING_ONLY or not would) else "continue"
    reason = ("M1 reports gaps only; the loop would " + ("continue" if would else "stop")) if M1_REPORTING_ONLY \
        else ("new work and novelty remain" if would else "no new work, no novelty, or rounds used up")

    def commit(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT OR REPLACE INTO gap_reports (investigation_id, round, computed, panel, novelty, decision, "
                     "reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (inv["id"], rnd, json.dumps(comp), json.dumps({
                         "members": [r.model_dump(mode="json") for r in reports], "independence": independence,
                         "merged": merged}), json.dumps({**novelty, "new_work": new_work, "would_continue": would}),
                      decision, reason))
        tc.db.event(inv["id"], "gaps", f"r{rnd}", stage="gaps", conn=conn, data={
            "round": rnd, "decision": decision, "would_continue": would, "novelty": novelty["total"],
            "new_work": new_work, "contradiction_pairs": len(comp["contradiction_pairs"]),
            "accepted_hypotheses": len(merged["new_hypotheses"]), "accepted_queries": len(merged["follow_up_queries"]),
            **{f"dropped_{k}": v for k, v in merged["dropped"].items()}})
    return commit


def stage(next_stage: str = "synthesis") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("gaps", f"gaps:r{int(sc.inv['round'])}", {"acquisition_stopped": bool(sc.guards.acquisition_stop)},
                 priority=95)
    # M1 is reporting only: whatever the decision, the run goes on to synthesis (B4: stop → synthesis).
    return StageDef("gaps", on_enter=on_enter, on_complete=lambda sc: next_stage)


STEP = StepDef("gaps", run, on_fail="skip")
