"""screen (PLAN B8.11), select (B8.12) and fetch (B8.13).

screen:b{hash}  batches of `screening.batch_size` candidates, dual (`screen`, A + B; `ensembles.dual_screening`
                false → one): relevance = max, hypothesis keys merged, include if either includes, every vote kept.
                Design from the publication-type map, else the including model's. Score as B8.11. A batch that
                failed is retried once in new batches (the stage stays); candidates still unscreened then are
                `screen_failed`.
select:r{round} (code): eligible = included, relevance ≥ `selection.min_relevance`, not yet selected;
                n = min(`max_fulltext_papers_per_round`, `guards.max_total_papers` − selected). Grounding sources
                relevant to a hypothesis first, then a round-robin quota pass (each hypothesis below
                `min_papers_per_hypothesis` takes its best paper), then the fill pass by score; within
                `full_text_tiebreak` of the best, full text is preferred (recorded in `selected_reason`).
                Unmet quotas are counted. Approval is M2 → fetching.
fetch:{cid}     library first (`find_existing`, tagged `inv:{id}`), else the source's fetch + enrich + save. A
                failed fetch is replaced once by the best unselected eligible paper (the stage stays).
Then → reading (1E).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .. import ensemble, llm, scoring
from ..db import _decode
from ..pipeline import STAY, StageContext, StageDef, StepDef
from ..schemas import ScreeningBatch
from ..sources import sources_for
from . import grounding as G, safety
from .search import SEED_VIA, article_of

SCREEN_VERSION = "screen.v1"
DONE = ("screened", "selected", "fetched", "fetch_failed", "replaced")


def _screen_cfg(settings: dict) -> dict:
    return {"batch_size": 8, "abstract_chars": None, **((settings or {}).get("screening") or {})}


def _select_cfg(settings: dict) -> dict:
    s = {"min_relevance": 2, "min_papers_per_hypothesis": 3, "max_fulltext_papers_per_round": 25,
         "full_text_tiebreak": 0.05, **((settings or {}).get("selection") or {})}
    s["max_total_papers"] = int((((settings or {}).get("guards") or {}).get("max_total_papers")) or 120)
    return s


def _year() -> int:
    return datetime.now(timezone.utc).year


# ---------- screening ----------

def batch_key(ids: list[int], attempt: int = 0) -> str:
    digest = hashlib.sha1(",".join(str(i) for i in sorted(ids)).encode()).hexdigest()[:10]
    return f"screen:b{digest}" + (f":r{attempt}" if attempt else "")


def to_screen(conn: sqlite3.Connection, investigation_id: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM candidates WHERE investigation_id = ? AND purpose = 'research' "
                                          "AND status = 'to_screen' ORDER BY id", (investigation_id,))]


def queue_batches(sc: StageContext, ids: list[int], attempt: int = 0) -> None:
    size = int(_screen_cfg(sc.settings)["batch_size"])
    for start in range(0, len(ids), size):
        batch = ids[start:start + size]
        sc.queue("screen", batch_key(batch, attempt), {"candidate_ids": batch}, priority=60)


def case_summary(brief, inv: dict) -> dict:
    return {"complaint": brief.chief_complaint.text if brief.chief_complaint else "",
            "key_features": [f.text for f in brief.case_features][:12],
            "population": inv.get("special_populations") or ["general"]}


def material(summary: dict, hyps: list[dict], rows: list[dict], chars: int | None) -> str:
    lines = ["Case: " + json.dumps(summary, ensure_ascii=False), "", "Hypotheses:"]
    lines += [f"- {h['key']}: {h['title']} ({h['kind']})" for h in hyps]
    lines += ["", "Papers:"]
    for i, r in enumerate(rows, 1):
        abstract = (r.get("abstract") or "(none)")
        if chars:
            abstract = abstract[:int(chars)]
        lines += [f"P{i}: {r['title']}", f"  Year: {r.get('year') or 'unknown'}; journal: {r.get('journal') or 'unknown'}; "
                  f"publication types: {', '.join(r.get('pub_types') or []) or 'unknown'}",
                  f"  Abstract: {abstract}", ""]
    return llm.material("\n".join(lines).strip(), kind="screen")


def validator(ids: list[str]):
    def check(batch: ScreeningBatch) -> None:
        got = [i.paper_id for i in batch.items]
        if sorted(got) != sorted(ids):
            raise ValueError(f"one item per paper is required: {', '.join(ids)}")
    return check


def population_match(row: dict, populations: list[str]) -> bool:
    """The paper names one of the case's special populations (B8.11 +0.05), by the grounding population terms."""
    text = f"{row.get('title') or ''} {row.get('abstract') or ''}".lower()
    return any(w in text for p in populations if p in G.POP_TERMS for w in G.POP_TERMS[p][0])


def combine(row: dict, members: list[tuple[Any, Any]], *, hyp_keys: set[str], populations: list[str],
            this_year: int) -> dict:
    votes = [{"model": ref.model, "family": ref.family, **item.model_dump(mode="json")} for ref, item in members]
    items = [item for _, item in members]
    decision = ensemble.include_if_any(i.decision for i in items)
    model_design = next((i.design for i in items if i.decision == "include"), items[0].design)
    design = scoring.design_for(row.get("pub_types") or [], model_design)
    relevance = max(i.relevance for i in items)
    keys = [k for k in ensemble.union(i.hypothesis_keys for i in items) if k in hyp_keys]
    total, parts = scoring.score(relevance=relevance, design=design, year=row.get("year"), this_year=this_year,
                                 cited_by=row.get("cited_by"), full_text_likely=bool(row.get("full_text_likely")),
                                 n_queries=len(row.get("query_ids") or []), seed=row.get("found_via") in SEED_VIA,
                                 population_match=population_match(row, populations))
    return {"votes": votes, "relevance": relevance, "hypothesis_keys": keys, "design": design, "decision": decision,
            "reason": "; ".join(dict.fromkeys(i.reason for i in items if i.reason))[:500], "score": total,
            "score_breakdown": parts}


async def run_screen(tc) -> Any:
    inv = tc.investigation
    ids = [int(i) for i in tc.payload["candidate_ids"]]
    with tc.db.read() as conn:
        rows = [_decode(conn.execute("SELECT * FROM candidates WHERE id = ?", (i,)).fetchone()) for i in ids]
        hyps = [_decode(r) for r in conn.execute("SELECT key, title, kind FROM hypotheses WHERE investigation_id = ? "
                                                 "AND status = 'active' ORDER BY id", (inv["id"],))]
        found = safety.current_brief(conn, inv["id"])
    if found is None:
        raise RuntimeError("screening needs a case brief")
    rows = [r for r in rows if r["status"] == "to_screen"]  # a retried batch may overlap one that finished
    if not rows:
        return None
    local = [f"P{i}" for i in range(1, len(rows) + 1)]
    cfg = _screen_cfg(tc.settings)
    system = llm.base_system() + "\n\n" + llm.prompt(SCREEN_VERSION)
    text = material(case_summary(found[1], inv), hyps, rows, cfg.get("abstract_chars"))
    ctx = tc.services.with_task(task_id=tc.task["id"], step="screen", investigation_id=inv["id"])
    dual = bool(((tc.settings or {}).get("ensembles") or {}).get("dual_screening", True))
    if dual:
        result = await ensemble.dual(ctx, "screen", ScreeningBatch, system, text, prompt_version=SCREEN_VERSION,
                                     validate=validator(local))
        members = [(m.model, m.value) for m in result.members if m.ok]
        independence = result.independence
    else:
        m = await ensemble.single(ctx, "screen", ScreeningBatch, system, text, prompt_version=SCREEN_VERSION,
                                  validate=validator(local))
        members, independence = [(m.model, m.value)], "single"
    keys = {h["key"] for h in hyps}
    pops = inv.get("special_populations") or []
    year = _year()
    out = []
    for pid, row in zip(local, rows):
        picked = [(ref, next(i for i in value.items if i.paper_id == pid)) for ref, value in members]
        out.append((row, combine(row, picked, hyp_keys=keys, populations=pops, this_year=year)))

    def commit(conn: sqlite3.Connection) -> None:
        included = 0
        for row, s in out:
            conn.execute("UPDATE screenings SET votes = ?, relevance = ?, hypothesis_keys = ?, design = ?, decision = ?, "
                         "reason = ?, score = ?, score_breakdown = ? WHERE candidate_id = ?",
                         (json.dumps(s["votes"]), s["relevance"], json.dumps(s["hypothesis_keys"]), s["design"],
                          s["decision"], s["reason"], s["score"], json.dumps(s["score_breakdown"]), row["id"]))
            conn.execute("UPDATE candidates SET status = 'screened' WHERE id = ?", (row["id"],))
            included += s["decision"] == "include"
        tc.db.event(inv["id"], "screen", tc.task["key"], stage="screening", conn=conn, data={
            "papers": len(out), "included": included, "independence": independence})
    return commit


def screening_stage(next_stage: str = "selecting") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        queue_batches(sc, to_screen(sc.conn, sc.id))

    def on_complete(sc: StageContext):
        left = to_screen(sc.conn, sc.id)
        if not left:
            return next_stage
        retried = any(t["key"].endswith(":r1") for t in sc.tasks("screen"))
        if not retried:
            queue_batches(sc, left, attempt=1)  # missing items are retried once (B4)
            return STAY
        for cid in left:
            sc.conn.execute("UPDATE candidates SET status = 'screen_failed' WHERE id = ?", (cid,))
        sc.event("screen_failed", data={"candidates": len(left)})
        return next_stage
    return StageDef("screening", on_enter=on_enter, on_complete=on_complete)


# ---------- selection ----------

def eligible(conn: sqlite3.Connection, investigation_id: int, min_relevance: int) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.found_via, c.full_text_likely, s.relevance, s.score, s.hypothesis_keys "
        "FROM candidates c JOIN screenings s ON s.candidate_id = c.id WHERE c.investigation_id = ? AND "
        "c.purpose = 'research' AND c.status = 'screened' AND s.decision = 'include' AND s.relevance >= ? "
        "ORDER BY s.score DESC, c.id", (investigation_id, min_relevance)).fetchall()
    return [_decode(r) for r in rows]


def plan_selection(pool: list[dict], hyp_keys: list[str], have: dict[str, int], n: int, cfg: dict) -> list[tuple[dict, str]]:
    """[(candidate, reason)] in selection order (B8.12)."""
    chosen: list[tuple[dict, str]] = []
    taken: set[int] = set()

    def take(c: dict, reason: str) -> None:
        chosen.append((c, reason))
        taken.add(c["id"])
        for k in c["hypothesis_keys"]:
            have[k] = have.get(k, 0) + 1
    for c in pool:  # grounding sources relevant to a hypothesis are always selected
        if c["found_via"] == "grounding" and c["hypothesis_keys"]:
            take(c, "grounding source")
    quota = int(cfg["min_papers_per_hypothesis"])
    progress = True
    while progress and len(chosen) < n:  # quota pass, round-robin over hypotheses
        progress = False
        for k in hyp_keys:
            if len(chosen) >= n:
                break
            if have.get(k, 0) >= quota:
                continue
            best = next((c for c in pool if c["id"] not in taken and k in c["hypothesis_keys"]), None)
            if best is not None:
                take(best, f"quota:{k}")
                progress = True
    tiebreak = float(cfg["full_text_tiebreak"])
    while len(chosen) < n:  # fill pass by score, full text preferred within the tiebreak
        rest = [c for c in pool if c["id"] not in taken]
        if not rest:
            break
        top = rest[0]
        close = [c for c in rest if (top["score"] or 0) - (c["score"] or 0) <= tiebreak]
        pick = next((c for c in close if c["full_text_likely"]), top) if not top["full_text_likely"] else top
        take(pick, "score (full-text tiebreak)" if pick is not top else "score")
    return chosen


async def run_select(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])
    cfg = _select_cfg(tc.settings)

    def commit(conn: sqlite3.Connection) -> None:
        hyp_keys = [r["key"] for r in conn.execute("SELECT key FROM hypotheses WHERE investigation_id = ? AND "
                                                   "status = 'active' ORDER BY id", (inv["id"],))]
        selected = [_decode(r) for r in conn.execute(
            "SELECT s.hypothesis_keys FROM candidates c JOIN screenings s ON s.candidate_id = c.id WHERE "
            "c.investigation_id = ? AND c.purpose = 'research' AND s.selected_round IS NOT NULL", (inv["id"],))]
        have: dict[str, int] = {}
        for s in selected:
            for k in s["hypothesis_keys"]:
                have[k] = have.get(k, 0) + 1
        n = max(0, min(int(cfg["max_fulltext_papers_per_round"]), cfg["max_total_papers"] - len(selected)))
        chosen = plan_selection(eligible(conn, inv["id"], int(cfg["min_relevance"])), hyp_keys, have, n, cfg)
        for c, reason in chosen:
            conn.execute("UPDATE candidates SET status = 'selected' WHERE id = ?", (c["id"],))
            conn.execute("UPDATE screenings SET selected_round = ?, selected_reason = ? WHERE candidate_id = ?",
                         (rnd, reason, c["id"]))
        unmet = sum(1 for k in hyp_keys if have.get(k, 0) < int(cfg["min_papers_per_hypothesis"]))
        tc.db.event(inv["id"], "select", f"r{rnd}", stage="selecting", conn=conn, data={
            "round": rnd, "selected": len(chosen), "slots": n, "unmet_quotas": unmet,
            "full_text_tiebreaks": sum(1 for _, r in chosen if "tiebreak" in r),
            "grounding_sources": sum(1 for _, r in chosen if r == "grounding source")})
    return commit


def selecting_stage(next_stage: str = "fetching") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("select", f"select:r{int(sc.inv['round'])}", {}, priority=60)

    return StageDef("selecting", on_enter=on_enter, on_complete=lambda sc: next_stage)


# ---------- fetch ----------

async def run_fetch(tc) -> Any:
    inv = tc.investigation
    cid = int(tc.payload["candidate_id"])
    with tc.db.read() as conn:
        row = _decode(conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone())
    fetched = await sources_for(tc.services).fetch(article_of(row), inv["id"])

    def commit(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE candidates SET status = 'fetched', literature_id = ? WHERE id = ?",
                     (fetched.literature_id, cid))
        tc.db.event(inv["id"], "fetch", f"c{cid}", stage="fetching", conn=conn, data={
            "candidate_id": cid, "from_library": fetched.from_library,
            "full_text": bool(fetched.details.full_text) if fetched.details else False})
    return commit


def fetch_failed(conn: sqlite3.Connection, task: dict, error: str) -> None:
    payload = task.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    if payload.get("candidate_id") is not None:
        conn.execute("UPDATE candidates SET status = 'fetch_failed' WHERE id = ?", (payload["candidate_id"],))


def fetching_stage(next_stage: str = "reading") -> StageDef:
    def queue_selected(sc: StageContext) -> None:
        for r in sc.conn.execute("SELECT id FROM candidates WHERE investigation_id = ? AND purpose = 'research' AND "
                                 "status = 'selected' ORDER BY id", (sc.id,)):
            sc.queue("fetch", f"fetch:{r['id']}", {"candidate_id": r["id"]}, priority=70)

    def on_complete(sc: StageContext):
        failed = [r["id"] for r in sc.conn.execute(
            "SELECT id FROM candidates WHERE investigation_id = ? AND purpose = 'research' AND status = 'fetch_failed' "
            "ORDER BY id", (sc.id,))]
        if not failed:
            return next_stage
        cfg = _select_cfg(sc.settings)
        pool = eligible(sc.conn, sc.id, int(cfg["min_relevance"]))
        replaced = 0
        for cid in failed:  # replacement mode (B8.12): the best unselected eligible paper, once per failure
            sc.conn.execute("UPDATE candidates SET status = 'replaced' WHERE id = ?", (cid,))
            if not pool:
                continue
            c = pool.pop(0)
            sc.conn.execute("UPDATE candidates SET status = 'selected' WHERE id = ?", (c["id"],))
            sc.conn.execute("UPDATE screenings SET selected_round = ?, selected_reason = ? WHERE candidate_id = ?",
                            (int(sc.inv["round"]), f"replacement for candidate {cid}", c["id"]))
            replaced += 1
        sc.event("fetch_replacements", data={"failed": len(failed), "replaced": replaced})
        if replaced:
            queue_selected(sc)
            return STAY
        return next_stage
    return StageDef("fetching", on_enter=queue_selected, on_complete=on_complete)


SCREEN_STEP = StepDef("screen", run_screen, on_fail="skip")
SELECT_STEP = StepDef("select", run_select, code_step=True)
FETCH_STEP = StepDef("fetch", run_fetch, on_fail=fetch_failed)
