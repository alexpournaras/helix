"""queries (PLAN B8.7): one task per active hypothesis and round, then the `searching` stage (1D.3).

queries:r{round}:{hypothesis key}
  1. one model call (`queries`) with the hypothesis, the complaints' MeSH descriptors and entry terms, the
     knowledge vocabulary (the hypothesis's differential item and the matching packs' MeSH terms and synonyms)
     and population terms → QuerySet;
  2. code checks (one repair through the validator): field allowlists per source, Europe PMC publication types,
     balanced quotes, parentheses and brackets, round 1 needs a guideline and a review query;
     code drops sources not searched in M1, duplicates, repeats of queries already run, and queries past
     `search.max_queries_per_hypothesis`;
  3. hit counts (Europe PMC scoped to PubMed + PMC, PubMed retmax 0): 0 hits, more than
     `search.max_hits_per_query`, or a query the source rejects → refine (`queries`, refine prompt), up to
     `search.max_refinements` times. Still 0 hits or rejected → `dropped`; still too many → kept (the search
     takes the top `results_per_query`).
Rows go to `research_queries` (status `ready` or `dropped`); a `search_log` note per round.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from typing import Any

from .. import ensemble, llm
from ..db import _decode, now
from ..knowledge import loader
from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import QueryRefinement, QuerySet, SearchQuery
from ..sources import sources_for
from . import grounding as G, safety

GENERATE_VERSION = "queries.generate.v1"
REFINE_VERSION = "queries.refine.v1"
M1_SOURCES = ("europepmc", "pubmed")  # OpenAlex and Semantic Scholar are M2
EPMC_FIELDS = {"TITLE_ABS", "TITLE", "ABSTRACT", "KW", "MESH", "PUB_TYPE", "LANG", "OPEN_ACCESS", "HAS_FT",
               "PUB_YEAR"}
EPMC_PUB_TYPES = {"review", "systematic review", "meta-analysis", "practice guideline", "guideline",
                  "randomized controlled trial"}
PUBMED_TAGS = {"tiab", "ti", "ab", "mh", "majr", "pt", "la", "dp", "sh"}
REVIEW_TYPES = {"systematic_review", "review"}
SYNTAX = {
    "europepmc": 'TITLE_ABS:"…", TITLE:, ABSTRACT:, KW:, MESH:"…", PUB_TYPE:"review" | "systematic review" | '
                 '"meta-analysis" | "practice guideline" | "guideline" | "randomized controlled trial", LANG:eng, '
                 'OPEN_ACCESS:y, HAS_FT:y, PUB_YEAR:[2015 TO 2026], AND/OR/NOT, parentheses. No SRC:.',
    "pubmed": '"…"[tiab], [ti], [ab], "…"[mh], [majr], [pt], [la], [dp], [sh], AND/OR/NOT, parentheses.',
}
COUNT_CONCURRENCY = 4  # Europe PMC allows 4 concurrent requests (B8.8)


def _cfg(settings: dict) -> dict:
    return {"sources": list(M1_SOURCES), "max_queries_per_hypothesis": 12, "max_hits_per_query": 50000,
            "max_refinements": 3, **((settings or {}).get("search") or {})}


def sources(cfg: dict) -> list[str]:
    return [s for s in cfg["sources"] if s in M1_SOURCES]


# ---------- checks ----------

def _unquoted(query: str) -> str:
    return re.sub(r'"[^"]*"', '""', query)


def check_query(source: str, query: str) -> str | None:
    """The problem with a query (field allowlist, publication types, balance), or None."""
    if query.count('"') % 2:
        return "unbalanced quotes"
    bare = _unquoted(query)
    for open_, close in ("()", "[]"):
        depth = 0
        for ch in bare:
            depth += (ch == open_) - (ch == close)
            if depth < 0:
                break
        if depth != 0:
            return f"unbalanced {open_}{close}"
    if source == "europepmc":
        bad = sorted({f for f in re.findall(r"\b([A-Za-z_]+):", bare) if f not in EPMC_FIELDS})
        if bad:
            return f"Europe PMC fields not allowed: {', '.join(bad)}"
        for value in re.findall(r'PUB_TYPE:\s*(?:"([^"]*)"|([A-Za-z-]+))', query):
            pt = (value[0] or value[1]).strip().lower()
            if pt not in EPMC_PUB_TYPES:
                return f"Europe PMC publication type not allowed: {pt}"
    elif source == "pubmed":
        bad = sorted({t for t in re.findall(r"\[([^\]]+)\]", bare) if t.strip().lower() not in PUBMED_TAGS})
        if bad:
            return f"PubMed tags not allowed: {', '.join(bad)}"
    return None


def norm(query: str) -> str:
    return " ".join(query.casefold().split())


def validator(allowed: list[str], required: set[str]):
    def check(qs: QuerySet) -> None:
        problems = [f"query {i + 1} ({q.source}): {p}" for i, q in enumerate(qs.queries)
                    if q.source in allowed and (p := check_query(q.source, q.query))]
        if problems:
            raise ValueError("; ".join(problems))
        kinds = {q.query_type for q in qs.queries if q.source in allowed}
        if "guideline" in required and "guideline" not in kinds:
            raise ValueError("this round needs a guideline query")
        if "review" in required and not kinds & REVIEW_TYPES:
            raise ValueError("this round needs a review or systematic_review query")
    return check


def select(proposed: list[SearchQuery], *, allowed: list[str], earlier: set[tuple[str, str]],
           limit: int) -> tuple[list[SearchQuery], dict[str, int]]:
    """(kept, drop counts): other sources, duplicates and repeats are dropped; at most `limit` are kept."""
    kept: list[SearchQuery] = []
    seen = set(earlier)
    dropped = {"source": 0, "duplicate": 0, "over_limit": 0}
    for q in proposed:
        if q.source not in allowed:
            dropped["source"] += 1
            continue
        k = (q.source, norm(q.query))
        if k in seen:
            dropped["duplicate"] += 1
            continue
        if len(kept) >= limit:
            dropped["over_limit"] += 1
            continue
        seen.add(k)
        kept.append(q)
    return kept, dropped


# ---------- material ----------

def vocabulary(db, inv: dict, hyp: dict, item: dict | None) -> dict:
    complaints = []
    for c in inv.get("complaints") or []:
        mesh = [{"descriptor": m.get("name"), "entry_terms": G.entry_terms(db, m["ui"])[:6]}
                for m in c.get("mesh") or [] if m.get("ui")]
        complaints.append({"text": c.get("text"), "mesh": mesh})
    packs = loader.packs_for(safety.mesh_uis(inv))
    vocab = {"mesh_terms": list(dict.fromkeys(t for p in packs for t in p.vocabulary.mesh_terms)),
             "synonyms": list(dict.fromkeys(t for p in packs for t in p.vocabulary.synonyms))}
    if item is not None:
        vocab["differential_item"] = {"text": item["text"],
                                      "key_features": (item.get("attributes") or {}).get("key_features") or []}
    pops = [p for p in inv.get("special_populations") or [] if p in G.POP_TERMS]
    return {"complaints": complaints, "knowledge_vocabulary": vocab,
            "population_terms": {p: G.POP_TERMS[p][0] for p in pops}}


def material(hyp: dict, vocab: dict, *, allowed: list[str], limit: int, required: set[str],
             earlier: list[str]) -> str:
    body = {"hypothesis": {k: hyp.get(k) for k in ("title", "description", "mechanism", "discriminators", "kind")},
            **vocab, "sources": allowed, "syntax": {s: SYNTAX[s] for s in allowed}, "max_queries": limit,
            "required_query_types": sorted(required), "queries_already_run": earlier[-40:]}
    return llm.material(json.dumps(body, ensure_ascii=False, indent=1), kind="queries")


def refine_material(hyp: dict, source: str, query: str, problem: str, attempts: list[str]) -> str:
    body = {"hypothesis": hyp.get("title"), "source": source, "syntax": SYNTAX[source], "query": query,
            "problem": problem, "earlier_attempts": attempts}
    return llm.material(json.dumps(body, ensure_ascii=False, indent=1), kind="queries_refine")


# ---------- hit counts and refinement ----------

async def settle(ctx, src, hyp: dict, q: SearchQuery, cfg: dict, gate: asyncio.Semaphore) -> dict:
    """Count hits and refine until the count is usable. Returns the row values for research_queries."""
    query, attempts, origin = q.query, [q.query], "model"
    max_hits, max_ref = int(cfg["max_hits_per_query"]), int(cfg["max_refinements"])
    refine_system = llm.base_system() + "\n\n" + llm.prompt(REFINE_VERSION)
    for n in range(max_ref + 1):
        problem, hits = None, None
        try:
            async with gate:
                hits = await src.count(q.source, query)
        except ValueError as exc:  # the source rejected the query
            problem = f"rejected by {q.source}: {str(exc)[:200]}"
        except Exception as exc:  # noqa: BLE001 — a network failure: the search step retries it
            return {"query": query, "hit_count": None, "attempts": n, "status": "ready", "origin": origin,
                    "error": f"count failed: {exc.__class__.__name__}"}
        if problem is None:
            if hits == 0:
                problem = "0 hits"
            elif hits > max_hits:
                problem = f"{hits} hits, more than {max_hits}"
            else:
                return {"query": query, "hit_count": hits, "attempts": n, "status": "ready", "origin": origin,
                        "error": None}
        if n == max_ref:
            if hits and hits > max_hits:
                return {"query": query, "hit_count": hits, "attempts": n, "status": "ready", "origin": origin,
                        "error": "too many hits after refinement"}
            return {"query": query, "hit_count": hits, "attempts": n, "status": "dropped", "origin": origin,
                    "error": problem}

        def check(r: QueryRefinement) -> None:
            bad = check_query(q.source, r.query)
            if bad:
                raise ValueError(bad)
            if norm(r.query) in {norm(a) for a in attempts}:
                raise ValueError("the refinement repeats an earlier attempt")
        member = await ensemble.single(ctx, "queries", QueryRefinement, refine_system,
                                       refine_material(hyp, q.source, query, problem, attempts),
                                       prompt_version=REFINE_VERSION, validate=check)
        query = member.value.query  # type: ignore[union-attr]
        attempts.append(query)
        origin = "refined"
    raise AssertionError("unreachable")


# ---------- the step ----------

async def run(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])
    hyp_id = int(tc.payload["hypothesis_id"])
    with tc.db.read() as conn:
        hyp = _decode(conn.execute("SELECT * FROM hypotheses WHERE id = ?", (hyp_id,)).fetchone())
        item = (_decode(conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (hyp["item_id"],)).fetchone())
                if hyp.get("item_id") else None)
        earlier_rows = [dict(r) for r in conn.execute(
            "SELECT source, query FROM research_queries WHERE investigation_id = ? AND purpose = 'research'",
            (inv["id"],))]
    cfg = _cfg(tc.settings)
    allowed = sources(cfg)
    if not allowed:
        raise RuntimeError("no search source is enabled")
    limit = int(cfg["max_queries_per_hypothesis"])
    required = {"guideline", "review"} if rnd == 1 else set()
    earlier = {(r["source"], norm(r["query"])) for r in earlier_rows}
    ctx = tc.services.with_task(task_id=tc.task["id"], step="queries", investigation_id=inv["id"])
    member = await ensemble.single(
        ctx, "queries", QuerySet, llm.base_system() + "\n\n" + llm.prompt(GENERATE_VERSION),
        material(hyp, vocabulary(tc.db, inv, hyp, item), allowed=allowed, limit=limit, required=required,
                 earlier=[f"{r['source']}: {r['query']}" for r in earlier_rows]),
        prompt_version=GENERATE_VERSION, validate=validator(allowed, required))
    proposed = member.value.queries  # type: ignore[union-attr]
    kept, dropped = select(proposed, allowed=allowed, earlier=earlier, limit=limit)
    src = sources_for(tc.services)
    gate = asyncio.Semaphore(COUNT_CONCURRENCY)
    settled = await asyncio.gather(*[settle(ctx, src, hyp, q, cfg, gate) for q in kept])

    def commit(conn: sqlite3.Connection) -> None:
        # Other hypotheses' tasks may have committed the same query meanwhile: check again here.
        taken = {(r["source"], norm(r["query"])) for r in conn.execute(
            "SELECT source, query FROM research_queries WHERE investigation_id = ? AND purpose = 'research'",
            (inv["id"],))}
        counts = {"ready": 0, "dropped": 0, "refined": 0, "repeat": 0}
        for q, s in zip(kept, settled):
            status, error = s["status"], s["error"]
            if (q.source, norm(s["query"])) in taken:
                status, error = "dropped", "repeat of a query already run"
                counts["repeat"] += 1
            taken.add((q.source, norm(s["query"])))
            counts[status] += 1
            counts["refined"] += s["origin"] == "refined"
            tc.db.insert(conn, "research_queries", {
                "investigation_id": inv["id"], "hypothesis_id": hyp_id, "round": rnd, "purpose": "research",
                "source": q.source, "query": s["query"], "query_type": q.query_type, "rationale": q.rationale,
                "origin": s["origin"], "hit_count": s["hit_count"], "attempts": s["attempts"], "status": status,
                "error": error})
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "search_log",
                                           "artifact_key": f"search:r{rnd}", "created_at": now()}, or_ignore=True)
        tc.db.event(inv["id"], "queries", f"r{rnd}:{hyp['key']}", stage="querying", conn=conn, data={
            "round": rnd, "hypothesis_id": hyp_id, "proposed": len(proposed), "kept": len(kept),
            **{f"dropped_{k}": v for k, v in dropped.items()}, **counts})
    return commit


def stage(next_stage: str = "searching") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        rnd = int(sc.inv["round"])
        # Round 1: every hypothesis. Later rounds (B17.5 item 9): only the hypotheses added for this round; the
        # gaps step's follow-up queries are already saved as `ready`.
        sql = "SELECT id, key FROM hypotheses WHERE investigation_id = ? AND status = 'active'"
        params: list = [sc.id]
        if rnd > 1 and (sc.settings.get("budget") or {}).get("targets"):
            sql += " AND round_added = ?"
            params.append(rnd)
        for h in sc.conn.execute(sql + " ORDER BY id", params):
            sc.queue("queries", f"queries:r{rnd}:{h['key']}", {"hypothesis_id": h["id"]}, priority=40)

    def on_complete(sc: StageContext):
        return next_stage
    # One hypothesis's queries failing is skipped (B10); more than half skipped fails the investigation.
    return StageDef("querying", on_enter=on_enter, on_complete=on_complete, skip_check_steps=("queries",))


STEP = StepDef("queries", run, on_fail="skip")
