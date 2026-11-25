"""grounding (PLAN B8.3a, B7.3, B7.4): one task per complaint × population key, then `ground_merge`.

ground:{complaint}:{population}
  1. queries (code): the complaint's verified MeSH descriptor and entry terms, population terms, guideline and
     systematic-review filters, language and date limits; Europe PMC and PubMed;
  2. search, dedup, prefilter (retracted, empty title, language, year, no abstract unless a guideline);
  3. dual screening (grounding.screen): relevance = max, include if either includes;
  4. selection (code): guidelines → systematic reviews → narrative reviews (relevance 3 only), newest first,
     ≤ max_per_organisation per first author, ≤ max_sources_per_complaint; no guideline and no systematic
     review → a gap;
  5. fetch (library first) and snapshot; a paper another complaint already read is reused;
  6. dual knowledge extraction per pack (grounding.extract); IDs outside the pack shown are dropped and an
     item left without IDs is rejected; quotes are assembled by code;
  7. verification of each attestation (grounding.verify: panel → escalate → adjudicate); `unsupported`
     rejects it; an item without a supported or partial attestation is rejected.
  Everything is written in one commit with knowledge version k+1 (visible after the merge).

ground_merge:k{k+1} (code + grounding.merge judge for borderline pairs): curated floor and pack items plus the
guideline items → B7.4 merge → duplicates superseded (attestations move to the kept item) → knowledge
version k+1. Then back to `safety` (the new knowledge may raise the level).
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.literature.models import Article

from .. import brief as B, ensemble, llm, scoring, verify as V
from ..db import _decode, now
from ..knowledge import loader, merge as M
from ..packing import pack_snapshot
from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import KnowledgeExtraction, MergeJudgement, ScreeningBatch
from ..segment import Snapshot, block_of, paper_snapshot, part_of, quote
from ..sources import Fetched, sources_for

SCREEN_VERSION = "grounding.screen.v1"
EXTRACT_VERSION = "grounding.extract.v1"
VERIFY_VERSION = "grounding.verify.v1"
MERGE_VERSION = "grounding.merge.v1"
SCREEN_BATCH = 8
MAX_JUDGE_PAIRS = 40
POP_TERMS = {
    "infant": (["infant", "newborn"], ['"infant"[mh]']),
    "child": (["child", "children", "paediatric", "pediatric"], ['"child"[mh]', '"adolescent"[mh]']),
    "pregnancy": (["pregnancy", "pregnant"], ['"pregnancy"[mh]']),
    "older": (["older adults", "elderly", "aged"], ['"aged"[mh]']),
    "immunocompromised": (["immunocompromised", "immunosuppressed"], ['"immunocompromised host"[mh]']),
}
ENGLISH = {"", "en", "eng", "english"}


def _cfg(settings: dict) -> dict:
    return {"max_sources_per_complaint": 6, "years": 10, "kinds": ["guideline", "systematic_review",
            "narrative_review"], "max_per_organisation": 2, "max_results_per_query": 25, "max_screened": 40,
            "pack_tokens": 32000, "max_packs_per_source": None, "max_complaints": 5, "enabled": True,
            **((settings or {}).get("grounding") or {})}


def _this_year() -> int:
    return datetime.now(timezone.utc).year


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50] or "item"


# ---------- 1. queries ----------

def build_queries(*, name: str | None, entry_terms: list[str], text: str, population: str, years: int,
                  this_year: int) -> list[dict]:
    """[{source, query, query_type}] for one complaint (B8.3a step 1)."""
    terms = [name] + [t for t in entry_terms if t and t.lower() != name.lower()][:3] if name else [text]
    title_abs = " OR ".join(f'TITLE_ABS:"{t}"' for t in terms)
    if name:
        title_abs += f' OR MESH:"{name}"'
    pop_epmc, pop_pubmed = "", ""
    pops = [p for p in population.split("+") if p in POP_TERMS]
    if pops:
        words = [w for p in pops for w in POP_TERMS[p][0]]
        pop_epmc = " AND (" + " OR ".join(f'TITLE_ABS:"{w}"' for w in words) + ")"
        heads = [h for p in pops for h in POP_TERMS[p][1]] + [f'"{w}"[tiab]' for p in pops for w in POP_TERMS[p][0]]
        pop_pubmed = " AND (" + " OR ".join(heads) + ")"
    limits_epmc = f" AND LANG:eng AND PUB_YEAR:[{this_year - years} TO {this_year}]"
    limits_pubmed = f' AND english[la] AND ("{this_year - years}"[dp] : "3000"[dp])'
    subject = f'"{name}"[mh]' if name else f'"{text}"[tiab]'
    return [
        {"source": "europepmc", "query_type": "guideline",
         "query": f'({title_abs}) AND (PUB_TYPE:"practice guideline" OR PUB_TYPE:"guideline"){pop_epmc}{limits_epmc}'},
        {"source": "europepmc", "query_type": "systematic_review",
         "query": f'({title_abs}) AND PUB_TYPE:"systematic review" AND (TITLE_ABS:diagnosis OR '
                  f'TITLE_ABS:evaluation OR TITLE_ABS:assessment OR TITLE_ABS:management){pop_epmc}{limits_epmc}'},
        {"source": "pubmed", "query_type": "guideline",
         "query": f'{subject} AND (guideline[pt] OR practice guideline[pt]){pop_pubmed}{limits_pubmed}'},
        {"source": "pubmed", "query_type": "systematic_review",
         "query": f'{subject} AND systematic review[pt] AND diagnosis[sh]{pop_pubmed}{limits_pubmed}'},
    ]


# ---------- 2. candidates ----------

def title_norm(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (title or "").casefold()))


@dataclass
class Cand:
    article: Article
    sources: list[str]
    queries: list[int]
    rank: int
    prefilter: str | None = None
    votes: list[dict] = field(default_factory=list)
    relevance: int = 0
    design: str = "other"
    decision: str = "exclude"
    reason: str = ""
    selected: bool = False
    local_id: str = ""

    @property
    def group(self) -> str:
        """Organisation or author group, for the per-group cap: the first author, else the journal."""
        first = (self.article.authors or [""])[0]
        return title_norm(first) or title_norm(self.article.journal) or self.local_id


def dedupe(hits: list[tuple[int, int, Article]]) -> list[Cand]:
    """Match by PMID, then PMCID, DOI, title + year (B8.8); merge sources and queries; keep the best rank."""
    out: list[Cand] = []
    index: dict[tuple[str, str], Cand] = {}
    for qi, rank, a in hits:
        keys = [("pmid", a.pmid), ("pmcid", a.pmcid), ("doi", a.doi),
                ("title", f"{title_norm(a.title)}|{a.year}" if a.title else None)]
        found = next((index[(k, v)] for k, v in keys if v and (k, v) in index), None)
        if found is None:
            found = Cand(a, [a.source], [qi], rank)
            out.append(found)
        else:
            if a.source not in found.sources:
                found.sources.append(a.source)
            if qi not in found.queries:
                found.queries.append(qi)
            found.rank = min(found.rank, rank)
            if not found.article.abstract and a.abstract:
                found.article = a  # the record with an abstract is more useful for screening
        for k, v in keys:
            if v:
                index.setdefault((k, v), found)
    return out


def prefilter(c: Cand, *, years: int, this_year: int) -> str | None:
    a = c.article
    if a.retracted:
        return "retracted"
    if not (a.title or "").strip():
        return "empty title"
    if (a.language or "").strip().lower() not in ENGLISH:
        return "language"
    if a.year is not None and a.year < this_year - years:
        return "year"
    if not (a.abstract or "").strip() and scoring.design_from_pub_types(a.publication_types) != "guideline":
        return "no abstract"
    return None


# ---------- 3. screening ----------

def screen_material(complaint: dict, population: str, batch: list[Cand]) -> str:
    lines = [f"Complaint: {complaint['text']}", f"Population: {population}", "", "Papers:"]
    for c in batch:
        a = c.article
        lines += [f"{c.local_id}: {a.title}", f"  Year: {a.year or 'unknown'}; journal: {a.journal or 'unknown'}; "
                  f"publication types: {', '.join(a.publication_types) or 'unknown'}",
                  f"  Abstract: {a.abstract or '(none)'}", ""]
    return llm.material("\n".join(lines).strip(), kind="grounding_screen")


def _screen_validator(ids: list[str]):
    def check(batch: ScreeningBatch) -> None:
        got = [i.paper_id for i in batch.items]
        if sorted(got) != sorted(ids):
            raise ValueError(f"one item per paper is required: {', '.join(ids)}")
    return check


async def screen(ctx: llm.CallContext, complaint: dict, population: str, cands: list[Cand], dual: bool) -> None:
    system = llm.base_system() + "\n\n" + llm.prompt(SCREEN_VERSION)
    for start in range(0, len(cands), SCREEN_BATCH):
        batch = cands[start:start + SCREEN_BATCH]
        material = screen_material(complaint, population, batch)
        check = _screen_validator([c.local_id for c in batch])
        if dual:
            result = await ensemble.dual(ctx, "grounding.screen", ScreeningBatch, system, material,
                                         prompt_version=SCREEN_VERSION, validate=check)
            members = [(m.model, m.value) for m in result.members if m.ok]
        else:
            m = await ensemble.single(ctx, "grounding.screen", ScreeningBatch, system, material,
                                      prompt_version=SCREEN_VERSION, validate=check)
            members = [(m.model, m.value)]
        for c in batch:
            items = [(ref, next(i for i in value.items if i.paper_id == c.local_id)) for ref, value in members]
            c.votes = [{"model": ref.model, "family": ref.family, **i.model_dump(mode="json")} for ref, i in items]
            c.relevance = max(i.relevance for _, i in items)
            c.decision = ensemble.include_if_any(i.decision for _, i in items)
            model_design = next((i.design for _, i in items if i.decision == "include"), items[0][1].design)
            c.design = scoring.design_for(c.article.publication_types, model_design)
            c.reason = "; ".join(dict.fromkeys(i.reason for _, i in items if i.reason))[:500]


# ---------- 4. selection ----------

def select(cands: list[Cand], cfg: dict) -> tuple[list[Cand], str | None]:
    """(selected, gap reason or None)."""
    kinds = list(cfg["kinds"])
    eligible = [c for c in cands if c.prefilter is None and c.decision == "include" and c.relevance >= 2
                and c.design in kinds and (c.design != "narrative_review" or c.relevance >= 3)]
    eligible.sort(key=lambda c: (kinds.index(c.design), -(c.article.year or 0), -c.relevance, c.rank))
    chosen: list[Cand] = []
    per_group: dict[str, int] = {}
    for c in eligible:
        if len(chosen) >= int(cfg["max_sources_per_complaint"]):
            break
        if per_group.get(c.group, 0) >= int(cfg["max_per_organisation"]):
            continue
        per_group[c.group] = per_group.get(c.group, 0) + 1
        c.selected = True
        chosen.append(c)
    if not any(c.design in ("guideline", "systematic_review") for c in chosen):
        return chosen, "No clinical guideline or systematic review was found in the searched sources."
    return chosen, None


# ---------- 5. reading ----------

@dataclass
class Read:
    cand: Cand
    fetched: Fetched | None
    snapshot: Snapshot
    packs: list
    coverage: str
    existing_read_id: int | None = None


def existing_reads(db, investigation_id: int) -> dict[int, dict]:
    rows = db.query("SELECT id, literature_id, segments, packs, scope FROM paper_reads WHERE investigation_id = ? "
                    "AND literature_id IS NOT NULL AND segments IS NOT NULL", (investigation_id,))
    return {r["literature_id"]: r for r in rows}


async def read_sources(tc, chosen: list[Cand], cfg: dict) -> tuple[list[Read], list[str]]:
    inv = tc.investigation
    src = sources_for(tc.services)
    reused = existing_reads(tc.db, inv["id"])
    reads, failures = [], []
    for c in chosen:
        try:
            fetched = await src.fetch(c.article, inv["id"])
        except Exception as exc:  # noqa: BLE001 — one paper failing is skipped (B10)
            failures.append(f"{c.local_id}: {exc.__class__.__name__}")
            continue
        old = reused.get(fetched.literature_id) if fetched.literature_id is not None else None
        if old is not None:
            snap = Snapshot.from_dict(old["segments"])
        else:
            snap = paper_snapshot(fetched.article.abstract or c.article.abstract, fetched.details)
        result = pack_snapshot(snap, int(cfg["pack_tokens"]), cfg.get("max_packs_per_source"))
        reads.append(Read(c, fetched, snap, result.packs, result.coverage, old["id"] if old else None))
    return reads, failures


# ---------- 6. extraction ----------

KINDS = {"red_flags": "red_flag", "differential": "differential", "history_items": "history_item",
         "evaluations": "evaluation", "management_topics": "management_topic"}


def _item_fields(kind: str, raw: dict) -> tuple[str, dict, str]:
    """(text, attributes, population) for one extracted item."""
    if kind == "red_flag":
        return raw["condition"], {"features": raw.get("features") or [], "urgency": raw["urgency"]}, raw.get("population") or "all"
    if kind == "differential":
        return raw["condition"], {"key_features": raw.get("key_features") or [],
                                  "typical_population": raw.get("typical_population") or ""}, "all"
    if kind == "history_item":
        return raw["attribute"], {"why": raw.get("why") or ""}, "all"
    if kind == "evaluation":
        return raw["test"], {"indication": raw.get("indication") or ""}, "all"
    return raw["topic"], {"context": raw.get("context") or ""}, "all"


@dataclass
class Extracted:
    kind: str
    text: str
    attributes: dict
    population: str
    attestations: list[dict] = field(default_factory=list)  # {read, sentence_ids, quote, model, family, pair}


def extraction_material(complaint: dict, population: str, pack) -> str:
    return llm.material(f"Complaint: {complaint['text']}\nPopulation: {population}\n\n{pack.text}",
                        kind="grounding_extract")


async def extract(ctx, complaint: dict, population: str, reads: list[Read], dual: bool) -> tuple[list[Extracted], int]:
    """Items grouped by (kind, normalised text, population). Returns (items, attestations dropped)."""
    system = llm.base_system() + "\n\n" + llm.prompt(EXTRACT_VERSION)
    items: dict[tuple[str, str, str], Extracted] = {}
    dropped = 0
    for ri, read in enumerate(reads):
        for pack in read.packs:
            material = extraction_material(complaint, population, pack)
            if dual:
                result = await ensemble.dual(ctx, "grounding.extract", KnowledgeExtraction, system, material,
                                             prompt_version=EXTRACT_VERSION)
                outs = [(m.model, m.value) for m in result.members if m.ok]
            else:
                m = await ensemble.single(ctx, "grounding.extract", KnowledgeExtraction, system, material,
                                          prompt_version=EXTRACT_VERSION)
                outs = [(m.model, m.value)]
            allowed = set(pack.seg_ids) | {block_of(s) for s in pack.seg_ids if part_of(s).startswith("T")}
            for ref, value in outs:
                data = value.model_dump(mode="json")
                for field_name, kind in KINDS.items():
                    for raw in data[field_name]:
                        ids = [s for s in dict.fromkeys(raw.get("sentence_ids") or []) if s in allowed][:4]
                        if not ids:
                            dropped += 1  # no ID from the pack shown: the attestation can't stand
                            continue
                        text, attrs, pop = _item_fields(kind, raw)
                        key = (kind, M.normalise(text), pop)
                        item = items.setdefault(key, Extracted(kind, text, attrs, pop))
                        if any(a["read"] == ri and set(a["sentence_ids"]) == set(ids) for a in item.attestations):
                            continue
                        item.attestations.append({"read": ri, "sentence_ids": ids, "quote": quote(read.snapshot, ids),
                                                  "model": ref.model, "family": ref.family})
    return list(items.values()), dropped


# ---------- 7. verification ----------

def _context(snapshot: Snapshot, ids: list[str], limit: int = 4000) -> str:
    """The paragraphs (or table) of the cited IDs, with their neighbours (± 1 block in the same part)."""
    blocks = []
    for s in snapshot.segments:
        b = block_of(s.seg_id)
        if b not in blocks:
            blocks.append(b)
    wanted: set[str] = set()
    for sid in ids:
        b = block_of(sid)
        if b in blocks:
            i = blocks.index(b)
            for j in (i - 1, i, i + 1):
                if 0 <= j < len(blocks) and part_of(blocks[j]) == part_of(b):
                    wanted.add(blocks[j])
    text = " ".join(s.text for s in snapshot.segments if block_of(s.seg_id) in wanted)
    return text[:limit]


def verify_material(complaint: dict, pairs: list[V.Pair], with_context: bool) -> str:
    lines = [f"Complaint: {complaint['text']}", ""]
    for p in pairs:
        lines += [f"{p.pair_id}: {p.text}", f"  Quote: {p.quote}"]
        if with_context:
            lines.append(f"  Context: {p.context}")
        lines.append("")
    return llm.material("\n".join(lines).strip(), kind="grounding_verify")


async def verify_items(ctx, complaint: dict, items: list[Extracted], reads: list[Read]) -> None:
    pairs: list[V.Pair] = []
    n = 0
    for item in items:
        label = item.kind.replace("_", " ")
        extra = item.attributes.get("features") or item.attributes.get("key_features") or []
        text = f"[{label}] {item.text}" + (f" (features: {', '.join(extra)})" if extra else "")
        if item.kind == "red_flag":
            text += f" — urgency: {item.attributes['urgency']}"
        if item.population != "all":
            text += f" — population: {item.population}"
        for att in item.attestations:
            n += 1
            att["pair"] = f"X{n}"
            pairs.append(V.Pair(f"X{n}", text, att["quote"], _context(reads[att["read"]].snapshot,
                                                                      att["sentence_ids"]),
                                att["model"], att["family"]))
    if not pairs:
        return
    system = llm.base_system() + "\n\n" + llm.prompt(VERIFY_VERSION)
    await V.verify(ctx, "grounding.verify", pairs, system=system, prompt_version=VERIFY_VERSION,
                   render=lambda ps, wc: verify_material(complaint, ps, wc))
    by_id = {p.pair_id: p for p in pairs}
    for item in items:
        for att in item.attestations:
            p = by_id[att["pair"]]
            att["verdict"], att["votes"] = p.verdict, p.votes


def item_trust(item: Extracted) -> tuple[str, str]:
    verdicts = {a.get("verdict") for a in item.attestations}
    if "supported" in verdicts:
        return "verified", "active"
    if "partial" in verdicts:
        return "partial", "active"
    return "proposed", "rejected"


# ---------- the task ----------

def complaint_of(inv: dict, key: str) -> dict | None:
    return next((c for c in inv.get("complaints") or [] if c["key"] == key), None)


def entry_terms(db, ui: str) -> list[str]:
    row = db.one("SELECT entry_terms FROM mesh_cache WHERE descriptor_ui = ? AND entry_terms IS NOT NULL LIMIT 1",
                 (ui,))
    if not row:
        return []
    value = row["entry_terms"]
    return value if isinstance(value, list) else json.loads(value or "[]")


async def run(tc) -> Any:
    inv = tc.investigation
    key, population = tc.payload["complaint"], tc.payload["population"]
    complaint = complaint_of(inv, key)
    cfg = _cfg(tc.settings)
    kv_next = int(inv["knowledge_version"]) + 1
    this_year = _this_year()
    ctx = tc.services.with_task(task_id=tc.task["id"], step="ground", investigation_id=inv["id"])
    ens = (tc.settings or {}).get("ensembles") or {}
    if complaint is None:
        raise RuntimeError(f"complaint {key} not found")
    descriptor = (complaint.get("mesh") or [None])[0]
    queries = build_queries(name=descriptor["name"] if descriptor else None,
                            entry_terms=entry_terms(tc.db, descriptor["ui"]) if descriptor else [],
                            text=complaint["text"], population=population, years=int(cfg["years"]),
                            this_year=this_year)
    src = sources_for(tc.services)
    hits: list[tuple[int, int, Article]] = []

    async def one(qi: int, q: dict) -> None:
        try:
            total, articles = await src.search(q["source"], q["query"], int(cfg["max_results_per_query"]))
            q.update(hit_count=total, status="done")
            hits.extend((qi, rank, a) for rank, a in enumerate(articles))
        except Exception as exc:  # noqa: BLE001 — a failed query is recorded; all failing → a gap
            q.update(hit_count=None, status="failed", error=exc.__class__.__name__)
    await asyncio.gather(*[one(i, q) for i, q in enumerate(queries)])
    hits.sort(key=lambda h: (h[1], h[0]))  # round-robin by rank across queries
    cands = dedupe(hits)
    for i, c in enumerate(cands, 1):
        c.local_id = f"P{i}"
        c.prefilter = prefilter(c, years=int(cfg["years"]), this_year=this_year)
    to_screen = [c for c in cands if c.prefilter is None][:int(cfg["max_screened"])]
    for c in [c for c in cands if c.prefilter is None][int(cfg["max_screened"]):]:
        c.prefilter = "cap"
    await screen(ctx, complaint, population, to_screen, bool(ens.get("dual_screening", True)))
    chosen, gap = select(cands, cfg)
    if all(q.get("status") == "failed" for q in queries):
        gap = "Every grounding search failed."
    reads, failures, items, dropped = [], [], [], 0
    if gap is None:
        reads, failures = await read_sources(tc, chosen, cfg)
        if not reads:
            gap = "None of the selected guidance could be fetched."
    if gap is None:
        items, dropped = await extract(ctx, complaint, population, reads,
                                       bool(ens.get("dual_knowledge_extraction", True)))
        await verify_items(ctx, complaint, items, reads)

    def commit(conn: sqlite3.Connection) -> None:
        write_run(tc.db, conn, inv, key, population, descriptor, queries, cands, reads, items, gap, kv_next,
                  {"failures": len(failures), "dropped_attestations": dropped})
    return commit


def write_run(db, conn, inv, key, population, descriptor, queries, cands, reads, items, gap, kv_next, extra) -> None:
    inv_id, rnd = inv["id"], inv["round"]
    query_ids = []
    for q in queries:
        query_ids.append(db.insert(conn, "research_queries", {
            "investigation_id": inv_id, "round": rnd, "purpose": "grounding", "source": q["source"],
            "query": q["query"], "query_type": q["query_type"], "origin": "code", "hit_count": q.get("hit_count"),
            "attempts": 1, "status": q.get("status", "done"), "error": q.get("error")}))
    cand_ids: dict[str, int] = {}
    for c in cands:
        a = c.article
        db.insert(conn, "candidates", {
            "investigation_id": inv_id, "round_found": rnd, "purpose": "grounding", "sources": c.sources,
            "source": a.source, "source_id": a.source_id, "pmid": a.pmid, "pmcid": a.pmcid, "doi": a.doi,
            "title_norm": title_norm(a.title), "title": a.title, "abstract": a.abstract, "authors": a.authors,
            "journal": a.journal, "year": a.year, "pub_types": a.publication_types, "language": a.language,
            "open_access": a.open_access, "full_text_likely": a.full_text_likely, "retracted": a.retracted,
            "concern_flag": a.concern, "first_rank": c.rank, "query_ids": [query_ids[i] for i in c.queries],
            "found_via": "grounding", "status": "selected" if c.selected else "screened"}, or_ignore=True)
        cid = conn.execute("SELECT id FROM candidates WHERE investigation_id = ? AND purpose = 'grounding' AND "
                           "source = ? AND source_id = ?", (inv_id, a.source, a.source_id)).fetchone()["id"]
        cand_ids[c.local_id] = cid
        db.insert(conn, "screenings", {
            "candidate_id": cid, "prefilter_reason": c.prefilter, "votes": c.votes,
            "relevance": c.relevance if c.votes else None, "design": c.design if c.votes else None,
            "decision": c.decision if c.votes else None, "reason": c.reason or None,
            "selected_round": rnd if c.selected else None,
            "selected_reason": f"grounding:{key}" if c.selected else None}, or_ignore=True)
    read_ids: list[int] = []
    for r in reads:
        if r.existing_read_id is None and r.fetched and r.fetched.literature_id is not None:
            # Another complaint's task may have read the same paper meanwhile (tasks run in parallel).
            row = conn.execute("SELECT id FROM paper_reads WHERE investigation_id = ? AND literature_id = ? AND "
                               "segments IS NOT NULL ORDER BY id LIMIT 1", (inv_id, r.fetched.literature_id)).fetchone()
            r.existing_read_id = row["id"] if row else None
        if r.existing_read_id is not None:
            read_ids.append(r.existing_read_id)
            continue
        read_ids.append(db.insert(conn, "paper_reads", {
            "investigation_id": inv_id, "candidate_id": cand_ids[r.cand.local_id],
            "literature_id": r.fetched.literature_id if r.fetched else None, "round": rnd, "purpose": "grounding",
            "scope": r.coverage, "fulltext_source": (r.fetched.details.full_text_from if r.fetched else "") or None,
            "parser": "jats" if r.snapshot.has_body else None, "skipped_sections": r.snapshot.skipped_sections,
            "segments": r.snapshot.to_dict(), "segments_hash": r.snapshot.hash,
            "packs": [{"no": p.no, "seg_ids": p.seg_ids, "tokens": p.tokens, "oversize": p.oversize} for p in r.packs],
            "status": "read"}))
        if r.fetched and r.fetched.literature_id:
            conn.execute("UPDATE candidates SET literature_id = ? WHERE id = ?",
                         (r.fetched.literature_id, cand_ids[r.cand.local_id]))
    counts = {"verified": 0, "partial": 0, "rejected": 0}
    for item in items:
        trust, status = item_trust(item)
        counts["rejected" if status == "rejected" else trust] += 1
        item_id = db.insert(conn, "knowledge_items", {
            "investigation_id": inv_id, "version": kv_next, "kind": item.kind,
            "key": f"{key}.{item.kind}.{_slug(item.text)}", "text": item.text,
            "attributes": {**item.attributes, "complaint": key, "grounding_population": population},
            "population": item.population, "origin": "guideline", "trust": trust, "status": status})
        for att in item.attestations:
            r = reads[att["read"]]
            db.insert(conn, "knowledge_attestations", {
                "item_id": item_id, "read_id": read_ids[att["read"]],
                "literature_id": r.fetched.literature_id if r.fetched else None,
                "sentence_ids": att["sentence_ids"], "quote": att["quote"], "verdict": att.get("verdict"),
                "votes": att.get("votes") or []})
    selected = [cand_ids[c.local_id] for c in cands if c.selected]
    db.insert(conn, "grounding_runs", {
        "investigation_id": inv_id, "complaint_key": key, "mesh_ui": descriptor["ui"] if descriptor else None,
        "population": population, "queries": query_ids, "candidate_ids": list(cand_ids.values()),
        "selected_ids": selected, "status": "gap" if gap else "ok", "gap_reason": gap,
        "knowledge_version": kv_next})
    db.event(inv_id, "grounding", "gap" if gap else "ok", stage="grounding", conn=conn, data={
        "complaint": key, "population": population, "queries": len(queries),
        "failed_queries": sum(q.get("status") == "failed" for q in queries), "candidates": len(cands),
        "screened": sum(1 for c in cands if c.votes), "selected": len(selected), "reads": len(reads),
        "items": counts, **extra})


def on_fail(conn: sqlite3.Connection, task: dict, error: str) -> None:
    """B10: a failed grounding task is a gap; planning relies on the floor, packs and proposed items."""
    payload = task.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    inv = conn.execute("SELECT knowledge_version FROM investigations WHERE id = ?",
                       (task["investigation_id"],)).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO grounding_runs (investigation_id, complaint_key, population, status, gap_reason, "
        "knowledge_version) VALUES (?, ?, ?, 'failed', ?, ?)",
        (task["investigation_id"], payload.get("complaint"), payload.get("population", "general"),
         "Grounding failed for this complaint.", int(inv["knowledge_version"]) + 1))


# ---------- merge ----------

def _db_item(row: dict, attestations: list[dict]) -> M.Item:
    return M.Item(row["kind"], f"db:{row['id']}", row["text"], row["origin"], population=row.get("population"),
                  attributes={k: v for k, v in (row.get("attributes") or {}).items()
                              if k not in ("complaint", "grounding_population", "merged_from", "disagreement")},
                  pack_ref=row.get("pack_ref"),
                  attestations=[M.Attestation(a.get("literature_id"), a.get("sentence_ids") or [],
                                              a.get("quote") or "", a.get("verdict") or "unsupported")
                                for a in attestations])


def borderline_pairs(items: list[M.Item]) -> list[tuple[M.Item, M.Item]]:
    out = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.kind != b.kind or (a.population or "all") != (b.population or "all"):
                continue
            na, nb = M.normalise(a.text), M.normalise(b.text)
            if na != nb and M._jaccard(na, nb) >= M.BORDERLINE_JACCARD and not (a.origin == b.origin == "curated"):
                out.append((a, b))
    return out[:MAX_JUDGE_PAIRS]


def _pairs_validator(n: int):
    def check(judgement: MergeJudgement) -> None:
        got = [p.pair for p in judgement.pairs]
        if sorted(got) != sorted(f"X{i}" for i in range(1, n + 1)):
            raise ValueError("one entry per pair is required")
    return check


async def run_merge(tc) -> Any:
    inv = tc.investigation
    kv_next = int(inv["knowledge_version"]) + 1
    with tc.db.read() as conn:
        rows = [_decode(r) for r in conn.execute(
            "SELECT * FROM knowledge_items WHERE investigation_id = ? AND status = 'active' AND version <= ? "
            "ORDER BY id", (inv["id"], kv_next))]
        atts: dict[int, list[dict]] = {}
        for r in conn.execute("SELECT * FROM knowledge_attestations WHERE item_id IN (SELECT id FROM knowledge_items "
                              "WHERE investigation_id = ?)", (inv["id"],)):
            a = _decode(r)
            atts.setdefault(a["item_id"], []).append(a)
    stored_keys = {r["key"] for r in rows if r["origin"] == "curated"}
    mesh = [m["ui"] for c in inv.get("complaints") or [] for m in c.get("mesh") or []]
    curated = [i for i in M.from_floor_and_packs(loader.floor(), loader.packs_for(mesh)) if i.key not in stored_keys]
    pool = [_db_item(r, atts.get(r["id"], [])) for r in rows] + curated
    pairs = borderline_pairs(pool)
    same: set[frozenset] = set()
    if pairs:
        ctx = tc.services.with_task(task_id=tc.task["id"], step="ground_merge", investigation_id=inv["id"])
        body = "\n".join(f"X{n}: ({a.kind}) {a.text}  <->  {b.text}" for n, (a, b) in enumerate(pairs, 1))
        m = await ensemble.single(ctx, "grounding.merge", MergeJudgement,
                                  llm.base_system() + "\n\n" + llm.prompt(MERGE_VERSION),
                                  llm.material(body, kind="merge"), prompt_version=MERGE_VERSION,
                                  validate=_pairs_validator(len(pairs)))
        for p in m.value.pairs:  # type: ignore[union-attr]
            if p.same:
                a, b = pairs[int(p.pair[1:]) - 1]
                same.add(frozenset((a.key, b.key)))
    result = M.merge(pool, judge=lambda a, b: frozenset((a.key, b.key)) in same)
    curated_by_key = {i.key: i for i in curated}

    def commit(conn: sqlite3.Connection) -> None:
        ids: dict[str, int] = {f"db:{r['id']}": r["id"] for r in rows}
        for item in result.items:
            if item.key in curated_by_key and item.key not in ids:
                ids[item.key] = tc.db.insert(conn, "knowledge_items", {
                    "investigation_id": inv["id"], "version": kv_next, "kind": item.kind, "key": item.key,
                    "text": item.text, "attributes": {**item.attributes, "sources": item.sources},
                    "population": item.population, "origin": "curated", "pack_ref": item.pack_ref,
                    "trust": "curated", "status": "active"})
        merged_away = 0
        for kept, other in result.merges:
            kept_id, other_id = ids.get(kept), ids.get(other)
            if other.startswith("db:") and other_id is not None:
                conn.execute("UPDATE knowledge_items SET status = 'superseded' WHERE id = ?", (other_id,))
                merged_away += 1
                if kept_id is not None:
                    conn.execute("UPDATE knowledge_attestations SET item_id = ? WHERE item_id = ?", (kept_id, other_id))
            elif other in curated_by_key:
                tc.db.insert(conn, "knowledge_items", {
                    "investigation_id": inv["id"], "version": kv_next, "kind": curated_by_key[other].kind,
                    "key": other, "text": curated_by_key[other].text, "origin": "curated", "trust": "curated",
                    "pack_ref": curated_by_key[other].pack_ref, "population": curated_by_key[other].population,
                    "attributes": {"merged_into": kept}, "status": "superseded"})
        for item in result.items:
            row_id = ids.get(item.key)
            if row_id is None or not (item.merged_from or item.disagreement):
                continue
            attrs = _decode(conn.execute("SELECT attributes FROM knowledge_items WHERE id = ?",
                                         (row_id,)).fetchone())["attributes"] or {}
            attrs.update({"merged_from": item.merged_from, "disagreement": item.disagreement})
            conn.execute("UPDATE knowledge_items SET attributes = ? WHERE id = ?", (json.dumps(attrs), row_id))
        tc.db.update_investigation(inv["id"], conn=conn, knowledge_version=kv_next)
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "research_plan",
                                           "artifact_key": f"knowledge:k{kv_next}", "created_at": now()},
                     or_ignore=True)
        tc.db.event(inv["id"], "knowledge", f"k{kv_next}", stage="grounding", conn=conn, data={
            "items": len(result.items), "merges": len(result.merges), "superseded": merged_away,
            "disagreements": len(result.disagreements), "judged_pairs": len(pairs), "curated_added": len(curated)})
    return commit


# ---------- stage ----------

def wanted(inv: dict, settings: dict) -> list[tuple[str, str]]:
    """(complaint, population key) pairs that grounding covers (at most max_complaints complaints)."""
    cfg = _cfg(settings)
    pop = B.population_key(inv.get("special_populations") or [])
    return [(c["key"], pop) for c in (inv.get("complaints") or [])[:int(cfg["max_complaints"])]]


def stage() -> StageDef:
    def on_enter(sc: StageContext) -> None:
        pending = []
        for key, pop in wanted(sc.inv, sc.settings):
            if not sc.conn.execute("SELECT 1 FROM grounding_runs WHERE investigation_id = ? AND complaint_key = ? "
                                   "AND population = ?", (sc.id, key, pop)).fetchone():
                pending.append((key, pop))
        for key, pop in pending:
            sc.queue("ground", f"ground:{key}:{pop}", {"complaint": key, "population": pop}, priority=20)
        if pending:
            return
        kv = int(sc.inv["knowledge_version"])
        if sc.conn.execute("SELECT 1 FROM grounding_runs WHERE investigation_id = ? AND knowledge_version > ?",
                           (sc.id, kv)).fetchone():
            sc.queue("ground_merge", f"ground_merge:k{kv + 1}", priority=20)

    def on_complete(sc: StageContext):
        return "safety"
    return StageDef("grounding", on_enter=on_enter, on_complete=on_complete)


STEP = StepDef("ground", run, on_fail=on_fail)
MERGE_STEP = StepDef("ground_merge", run_merge, code_step=True)
