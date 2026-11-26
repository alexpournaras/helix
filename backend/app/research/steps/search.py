"""search, seed (PLAN B8.8) and prefilter (B8.10).

search:{query_id}  one task per `ready` research query of the round: Europe PMC `search_records` (scoped to PubMed
                   and PMC) or PubMed esearch + fetch_many, top `search.results_per_query`; hits become candidates.
seed:r{round}      round 1: the matching packs' seed papers (`found_via = seed`) and the grounding sources selected
                   for this investigation (`found_via = grounding`, already in the library).
Candidates are deduplicated within the investigation by PMID, then PMCID, DOI, OpenAlex ID, Semantic Scholar ID,
then normalised title + year; a match merges sources, queries, flags and the best rank.
Every search failing (and no seed) fails the investigation; otherwise → prefiltering.

prefilter:r{round} (code): first match wins — language, year, excluded publication type, retracted, no abstract
(unless a guideline, seed or grounding source), empty title. The rest is capped at what's left of
`guards.max_screened`, filled round-robin by rank (seeds and grounding sources first).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.literature.models import Article

from .. import scoring
from ..db import _decode
from ..knowledge import loader
from ..pipeline import Fail, StageContext, StageDef, StepDef
from ..sources import sources_for
from . import grounding as G, safety

SEED_VIA = ("seed", "grounding")


# ---------- candidates ----------

def article_of(row: dict) -> Article:
    """The Article a candidate row stands for (to fetch it)."""
    return Article(source=row["source"], source_id=row["source_id"], pmid=row.get("pmid"), pmcid=row.get("pmcid"),
                   doi=row.get("doi"), title=row.get("title") or "", abstract=row.get("abstract") or "",
                   authors=row.get("authors") or [], journal=row.get("journal") or "", year=row.get("year"),
                   publication_types=row.get("pub_types") or [], language=row.get("language") or "",
                   open_access=None if row.get("open_access") is None else bool(row["open_access"]),
                   retracted=bool(row.get("retracted")), concern=bool(row.get("concern_flag")))


def find_candidate(conn: sqlite3.Connection, investigation_id: int, a: Article, *, openalex_id: str | None = None,
                   s2_id: str | None = None) -> dict | None:
    keys = [("pmid", a.pmid), ("pmcid", a.pmcid), ("doi", a.doi), ("openalex_id", openalex_id), ("s2_id", s2_id)]
    for col, value in keys:
        if value:
            row = conn.execute(f"SELECT * FROM candidates WHERE investigation_id = ? AND purpose = 'research' "
                               f"AND {col} = ? ORDER BY id LIMIT 1", (investigation_id, value)).fetchone()
            if row:
                return _decode(row)
    tn = G.title_norm(a.title)
    if tn:
        row = conn.execute("SELECT * FROM candidates WHERE investigation_id = ? AND purpose = 'research' AND "
                           "title_norm = ? AND year IS ? ORDER BY id LIMIT 1", (investigation_id, tn, a.year)).fetchone()
        if row:
            return _decode(row)
    return None


def upsert(db, conn: sqlite3.Connection, investigation_id: int, rnd: int, a: Article, *, query_id: int | None,
           rank: int, found_via: str = "search", literature_id: int | None = None) -> tuple[int, bool]:
    """(candidate id, new?)."""
    old = find_candidate(conn, investigation_id, a)
    if old is None:
        cid = db.insert(conn, "candidates", {
            "investigation_id": investigation_id, "round_found": rnd, "purpose": "research", "sources": [a.source],
            "source": a.source, "source_id": a.source_id, "pmid": a.pmid, "pmcid": a.pmcid, "doi": a.doi,
            "title_norm": G.title_norm(a.title) or None, "title": a.title, "abstract": a.abstract,
            "authors": a.authors, "journal": a.journal, "year": a.year, "pub_types": a.publication_types,
            "language": a.language, "open_access": a.open_access, "full_text_likely": a.full_text_likely,
            "retracted": a.retracted, "concern_flag": a.concern, "first_rank": rank,
            "query_ids": [query_id] if query_id is not None else [], "found_via": found_via,
            "literature_id": literature_id})
        return cid, True
    sources = list(dict.fromkeys([*old["sources"], a.source]))
    qids = list(dict.fromkeys([*old["query_ids"], *([query_id] if query_id is not None else [])]))
    via = old["found_via"] if old["found_via"] in SEED_VIA or found_via not in SEED_VIA else found_via
    conn.execute(
        "UPDATE candidates SET sources = ?, query_ids = ?, first_rank = MIN(COALESCE(first_rank, ?), ?), "
        "pmid = COALESCE(pmid, ?), pmcid = COALESCE(pmcid, ?), doi = COALESCE(doi, ?), "
        "abstract = CASE WHEN abstract = '' THEN ? ELSE abstract END, "
        "pub_types = CASE WHEN pub_types = '[]' THEN ? ELSE pub_types END, "
        "retracted = MAX(retracted, ?), concern_flag = MAX(concern_flag, ?), "
        "full_text_likely = MAX(COALESCE(full_text_likely, 0), ?), found_via = ?, "
        "literature_id = COALESCE(literature_id, ?) WHERE id = ?",
        (json.dumps(sources), json.dumps(qids), rank, rank, a.pmid, a.pmcid, a.doi, a.abstract,
         json.dumps(a.publication_types), int(a.retracted), int(a.concern), int(a.full_text_likely), via,
         literature_id, old["id"]))
    return old["id"], False


# ---------- search ----------

def _results(settings: dict) -> int:
    return int(((settings or {}).get("search") or {}).get("results_per_query") or 100)


async def run_search(tc) -> Any:
    inv = tc.investigation
    qid = int(tc.payload["query_id"])
    with tc.db.read() as conn:
        q = _decode(conn.execute("SELECT * FROM research_queries WHERE id = ?", (qid,)).fetchone())
    total, found = await sources_for(tc.services).search(q["source"], q["query"], _results(tc.settings))

    def commit(conn: sqlite3.Connection) -> None:
        new = 0
        for rank, a in enumerate(found, 1):
            _, added = upsert(tc.db, conn, inv["id"], int(inv["round"]), a, query_id=qid, rank=rank)
            new += added
        conn.execute("UPDATE research_queries SET status = 'done', hit_count = COALESCE(hit_count, ?), error = NULL "
                     "WHERE id = ?", (total, qid))
        tc.db.event(inv["id"], "search", f"q{qid}", stage="searching", conn=conn,
                    data={"query_id": qid, "source": q["source"], "hits": total, "results": len(found), "new": new})
    return commit


def search_failed(conn: sqlite3.Connection, task: dict, error: str) -> None:
    payload = task.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    qid = payload.get("query_id")
    if qid is not None:
        conn.execute("UPDATE research_queries SET status = 'failed', error = ? WHERE id = ?", (error[:300], qid))


async def run_seed(tc) -> Any:
    """Pack seed papers (by PMID through PubMed, by DOI through Europe PMC) and the grounding sources."""
    inv = tc.investigation
    packs = loader.packs_for(safety.mesh_uis(inv))
    pmids = list(dict.fromkeys(s.pmid for p in packs for s in p.seed_literature if s.pmid))
    dois = list(dict.fromkeys(s.doi.lower() for p in packs for s in p.seed_literature if s.doi and not s.pmid))
    src = sources_for(tc.services)
    seeds: list[Article] = []
    if pmids:
        _, got = await src.search("pubmed", " OR ".join(f"{p}[pmid]" for p in pmids), len(pmids))
        seeds += got
    for doi in dois:
        _, got = await src.search("europepmc", f'DOI:"{doi}"', 1)
        seeds += got[:1]
    with tc.db.read() as conn:
        grounding = [_decode(r) for r in conn.execute(
            "SELECT * FROM candidates WHERE investigation_id = ? AND purpose = 'grounding' AND status = 'selected' "
            "ORDER BY id", (inv["id"],))]

    def commit(conn: sqlite3.Connection) -> None:
        rnd = int(inv["round"])
        n_seed = n_ground = 0
        for a in seeds:
            upsert(tc.db, conn, inv["id"], rnd, a, query_id=None, rank=0, found_via="seed")
            n_seed += 1
        for g in grounding:
            upsert(tc.db, conn, inv["id"], rnd, article_of(g), query_id=None, rank=0, found_via="grounding",
                   literature_id=g.get("literature_id"))
            n_ground += 1
        tc.db.event(inv["id"], "seed", f"r{rnd}", stage="searching", conn=conn,
                    data={"round": rnd, "pack_seeds": n_seed, "grounding_sources": n_ground,
                          "pack_seeds_listed": len(pmids) + len(dois)})
    return commit


def searching_stage(next_stage: str = "prefiltering") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        rnd = int(sc.inv["round"])
        for q in sc.conn.execute("SELECT id FROM research_queries WHERE investigation_id = ? AND purpose = 'research' "
                                 "AND round = ? AND status = 'ready' ORDER BY id", (sc.id, rnd)):
            sc.queue("search", f"search:{q['id']}", {"query_id": q["id"]}, priority=50)
        if rnd == 1:
            sc.queue("seed", f"seed:r{rnd}", {}, priority=50)

    def on_complete(sc: StageContext):
        searches = sc.tasks("search")
        if searches and all(t["status"] != "done" for t in searches):
            n = sc.conn.execute("SELECT COUNT(*) AS n FROM candidates WHERE investigation_id = ? AND "
                                "purpose = 'research'", (sc.id,)).fetchone()["n"]
            if not n:
                return Fail("Every literature search failed.")
        return next_stage
    return StageDef("searching", on_enter=on_enter, on_complete=on_complete)


# ---------- prefilter ----------

def _filters(settings: dict) -> dict:
    return {"languages": ["eng", "en"], "year_from": None, "year_to": None, "exclude_pub_types": [],
            **((settings or {}).get("filters") or {})}


def exclusion(row: dict, f: dict) -> str | None:
    """B8.10, first match wins."""
    lang = (row.get("language") or "").strip().lower()
    if lang and lang not in {x.lower() for x in f["languages"]} | {"english"}:
        return "language"
    year = row.get("year")
    if year is not None and ((f["year_from"] and year < int(f["year_from"])) or
                             (f["year_to"] and year > int(f["year_to"]))):
        return "year"
    excluded = {p.lower() for p in f["exclude_pub_types"]}
    if any(p.lower() in excluded for p in row.get("pub_types") or []):
        return "publication type"
    if row.get("retracted"):
        return "retracted"
    exempt = row.get("found_via") in SEED_VIA or scoring.design_from_pub_types(row.get("pub_types") or []) == "guideline"
    if not (row.get("abstract") or "").strip() and not exempt:
        return "no abstract"
    if not (row.get("title") or "").strip():
        return "empty title"
    return None


async def run_prefilter(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])
    f = _filters(tc.settings)
    cap = int((((tc.settings or {}).get("guards") or {}).get("max_screened")) or 6000)

    def commit(conn: sqlite3.Connection) -> None:
        rows = [_decode(r) for r in conn.execute(
            "SELECT * FROM candidates WHERE investigation_id = ? AND purpose = 'research' AND status = 'new' "
            "ORDER BY id", (inv["id"],))]
        screened = conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE investigation_id = ? AND purpose = 'research' AND status NOT IN "
            "('new', 'excluded', 'capped')", (inv["id"],)).fetchone()["n"]  # every paper that took a place
        room = max(0, cap - screened)
        counts: dict[str, int] = {}
        passing = []
        for r in rows:
            reason = exclusion(r, f)
            tc.db.insert(conn, "screenings", {"candidate_id": r["id"], "prefilter_reason": reason}, or_ignore=True)
            if reason:
                counts[reason] = counts.get(reason, 0) + 1
                conn.execute("UPDATE candidates SET status = 'excluded' WHERE id = ?", (r["id"],))
            else:
                passing.append(r)
        # Round-robin by rank: every query's first hit, then every second hit… (seeds and grounding rank 0).
        passing.sort(key=lambda r: (r["first_rank"] if r["first_rank"] is not None else 10 ** 6, r["id"]))
        for i, r in enumerate(passing):
            status = "to_screen" if i < room else "capped"
            conn.execute("UPDATE candidates SET status = ? WHERE id = ?", (status, r["id"]))
            if status == "capped":
                conn.execute("UPDATE screenings SET prefilter_reason = 'cap' WHERE candidate_id = ?", (r["id"],))
        tc.db.event(inv["id"], "prefilter", f"r{rnd}", stage="prefiltering", conn=conn, data={
            "round": rnd, "candidates": len(rows), "to_screen": min(len(passing), room),
            "capped": max(0, len(passing) - room), **{f"excluded_{k.replace(' ', '_')}": v for k, v in counts.items()}})
    return commit


def prefilter_stage(next_stage: str = "screening") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("prefilter", f"prefilter:r{int(sc.inv['round'])}", {}, priority=50)

    return StageDef("prefiltering", on_enter=on_enter, on_complete=lambda sc: next_stage)


SEARCH_STEP = StepDef("search", run_search, on_fail=search_failed)
SEED_STEP = StepDef("seed", run_seed, on_fail="skip")
PREFILTER_STEP = StepDef("prefilter", run_prefilter, code_step=True)
