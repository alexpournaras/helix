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

Reuse (`grounding.reuse`, PLAN B17.6, task 1R.2): before searching, the task looks up a cached grounding for the
same complaint (MeSH descriptor, else its text) × population key × versions (prompts, query builder, the settings
and models that shape the result). A hit copies the queries, sources, items and attestations without any model
call (the library fetch is local-first); a miss runs the steps above and caches an `ok` result, or a `gap` when
every search succeeded. At most `grounding.max_new_complaints` complaints are grounded fresh per investigation
(cached ones don't count); any further complaint gets the gap "not grounded: token budget".

ground_merge:k{k+1} (code + grounding.merge judge for borderline pairs): curated floor and pack items plus the
guideline items → B7.4 merge → duplicates superseded (attestations move to the kept item) → knowledge
version k+1. Then back to `safety` (the new knowledge may raise the level).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.literature.models import Article

from .. import brief as B, ensemble, llm, scoring, verify as V
from ..db import _decode, now
from ..knowledge import loader, merge as M
from ..packing import pack_snapshot
from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import KnowledgeExtraction, MergeGroups, ScreeningBatch
from ..segment import Snapshot, block_of, paper_snapshot, part_of, quote
from ..sources import Fetched, sources_for

SCREEN_VERSION = "grounding.screen.v1"
EXTRACT_VERSION = "grounding.extract.v3"
VERIFY_VERSION = "grounding.verify.v2"
MERGE_VERSION = "grounding.merge.v2"
QUERY_VERSION = "grounding.queries.v1"  # build_queries (code); bump when the queries change (cache key, B17.6)
BUDGET_GAP = "not grounded: token budget"
SCREEN_BATCH = 8
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

    async def get(c: Cand):
        try:
            return await src.fetch(c.article, inv["id"])
        except Exception as exc:  # noqa: BLE001 — one paper failing is skipped (B10)
            return exc
    # Fetches run in parallel; results are kept in selection order.
    for c, fetched in zip(chosen, await asyncio.gather(*[get(c) for c in chosen])):
        if isinstance(fetched, Exception):
            failures.append(f"{c.local_id}: {fetched.__class__.__name__}")
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
        return raw["condition"], {"features": raw.get("features") or [], "urgency": raw["urgency"],
                                  "question": raw.get("question") or ""}, raw.get("population") or "all"
    pop = raw.get("population") or "all"
    if kind == "differential":
        return raw["condition"], {"key_features": raw.get("key_features") or [],
                                  "typical_population": raw.get("typical_population") or ""}, pop
    if kind == "history_item":
        return raw["attribute"], {"why": raw.get("why") or "", "question": raw.get("question") or ""}, pop
    if kind == "evaluation":
        return raw["test"], {"indication": raw.get("indication") or ""}, pop
    return raw["topic"], {"context": raw.get("context") or ""}, pop


EXAM_WORDS = re.compile(r"\b(exam\w*|imaging|scan\w*|test\w*|audiolog\w*|audiogram\w*|measure\w*|investigation\w*)\b",
                        re.I)
GENERIC_HISTORY = {"history", "clinical history", "targeted history", "medical history", "take history"}


def history_kind(kind: str, text: str) -> str | None:
    """History items are questions for the person: an examination or test becomes an evaluation, and a generic
    "take a history" is dropped (None). Other kinds pass through."""
    if kind != "history_item":
        return kind
    if " ".join(re.findall(r"[a-z]+", text.lower())) in GENERIC_HISTORY:
        return None
    return "evaluation" if EXAM_WORDS.search(text) else kind


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
    jobs = [(ri, read, pack) for ri, read in enumerate(reads) for pack in read.packs]

    async def one(pack) -> list:
        material = extraction_material(complaint, population, pack)
        if dual:
            result = await ensemble.dual(ctx, "grounding.extract", KnowledgeExtraction, system, material,
                                         prompt_version=EXTRACT_VERSION)
            return [(m.model, m.value) for m in result.members if m.ok]
        m = await ensemble.single(ctx, "grounding.extract", KnowledgeExtraction, system, material,
                                  prompt_version=EXTRACT_VERSION)
        return [(m.model, m.value)]

    # Packs run in parallel (the provider limiter caps concurrency); results are combined in document order.
    results = await asyncio.gather(*[one(pack) for _, _, pack in jobs])
    for (ri, read, pack), outs in zip(jobs, results):
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
                    kind = history_kind(kind, text)
                    if kind is None:
                        continue  # "take a clinical history": not a question for the person
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
        # The urgency is the extractor's classification, not a guideline claim: it isn't verified (verify.v2).
        if item.population != "all":
            text += f" — population: {item.population}"
        for att in item.attestations:
            n += 1
            att["pair"] = f"X{n}"
            triggers = {"red_flag": ["red_flag"], "evaluation": ["recommendation"],
                        "management_topic": ["recommendation"]}.get(item.kind, [])
            pairs.append(V.Pair(f"X{n}", text, att["quote"], _context(reads[att["read"]].snapshot,
                                                                      att["sentence_ids"]),
                                att["model"], att["family"], triggers=triggers, seed=text))
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


# ---------- reuse (PLAN B17.6) ----------

def _reuse(settings: dict) -> dict:
    return {"enabled": False, "max_age_days": 180, "gap_max_age_days": 30,
            **(((settings or {}).get("grounding") or {}).get("reuse") or {})}


def cache_key(complaint: dict, population: str, settings: dict) -> tuple[str, str, dict]:
    """(key, complaint key, versions): everything that shapes a grounding result, hashed."""
    descriptor = (complaint.get("mesh") or [None])[0]
    complaint_key = descriptor["ui"] if descriptor else "text:" + _slug(complaint.get("text") or complaint["key"])
    cfg = _cfg(settings)
    roles = (settings or {}).get("roles") or {}
    ens = (settings or {}).get("ensembles") or {}
    versions = {
        "prompts": [SCREEN_VERSION, EXTRACT_VERSION, VERIFY_VERSION, MERGE_VERSION, QUERY_VERSION],
        "grounding": {k: cfg.get(k) for k in ("years", "kinds", "max_per_organisation", "max_results_per_query",
                                              "max_screened", "max_sources_per_complaint", "pack_tokens",
                                              "max_packs_per_source")},
        "ensembles": {k: ens.get(k) for k in ("dual_screening", "dual_knowledge_extraction")},
        "models": {r: roles.get(r) for r in ("grounding.screen", "grounding.extract", "grounding.verify")},
        "languages": ((settings or {}).get("filters") or {}).get("languages"),
    }
    digest = hashlib.sha256(json.dumps([complaint_key, population, versions], sort_keys=True,
                                       default=str).encode("utf-8")).hexdigest()
    return digest, complaint_key, versions


def cache_store(tc):
    """Eval runs share one cache file across cases (`services.cache`); otherwise the research database."""
    return getattr(tc.services, "cache", None) or tc.db


def cacheable(queries: list[dict], failures: list[str], gap: str | None) -> bool:
    """Cache only a complete result: every search succeeded and every selected source was read."""
    return all(q.get("status") == "done" for q in queries) and not failures and gap != BUDGET_GAP


def cache_payload(queries: list[dict], reads: list["Read"], items: list["Extracted"]) -> dict:
    return {
        "queries": [{k: q.get(k) for k in ("source", "query", "query_type", "hit_count", "status")} for q in queries],
        "sources": [{"article": r.cand.article.model_dump(mode="json"), "sources": r.cand.sources,
                     "rank": r.cand.rank, "votes": r.cand.votes, "relevance": r.cand.relevance,
                     "design": r.cand.design, "decision": r.cand.decision, "reason": r.cand.reason,
                     "snapshot": r.snapshot.to_dict(), "coverage": r.coverage,
                     "packs": [{"no": p.no, "seg_ids": p.seg_ids, "tokens": p.tokens, "oversize": p.oversize}
                               for p in r.packs]} for r in reads],
        "items": [{"kind": i.kind, "text": i.text, "attributes": i.attributes, "population": i.population,
                   "attestations": [{k: a.get(k) for k in ("read", "sentence_ids", "quote", "model", "family",
                                                           "verdict", "votes")} for a in i.attestations]}
                  for i in items],
    }


async def from_cache(tc, hit: dict) -> tuple[list[dict], list[Cand], list["Read"], list["Extracted"]]:
    """Rebuild a cached grounding for this investigation: sources fetched library-first (no model calls)."""
    src = sources_for(tc.services)
    payload = hit["payload"]
    cands, reads = [], []
    for n, s in enumerate(payload["sources"], 1):
        article = Article.model_validate(s["article"])
        c = Cand(article=article, sources=s["sources"], queries=[], rank=s["rank"], votes=s["votes"],
                 relevance=s["relevance"], design=s["design"], decision=s["decision"], reason=s["reason"],
                 selected=True, local_id=f"P{n}")
        try:
            fetched = await src.fetch(article, tc.investigation["id"])
        except Exception:  # noqa: BLE001 — the cached snapshot still carries the text; the library link is optional
            fetched = None
        cands.append(c)
        reads.append(Read(c, fetched, Snapshot.from_dict(s["snapshot"]),
                          [SimpleNamespace(**p) for p in s["packs"]], s["coverage"]))
    items = [Extracted(kind=i["kind"], text=i["text"], attributes=i["attributes"], population=i["population"],
                       attestations=[dict(a) for a in i["attestations"]]) for i in payload["items"]]
    queries = [dict(q) for q in payload["queries"]]
    return queries, cands, reads, items


def claim_new_slot(db, investigation_id: int, complaint_key: str, limit: int | None) -> bool:
    """At most `limit` complaints grounded fresh per investigation (B17.5 item 10); claimed atomically, because
    grounding tasks run in parallel. A complaint that already holds a slot keeps it (retries)."""
    if not limit:
        return True
    with db.tx() as conn:
        held = [r["message"] for r in conn.execute(
            "SELECT message FROM events WHERE investigation_id = ? AND kind = 'grounding_new'", (investigation_id,))]
        if complaint_key in held:
            return True
        if len(held) >= int(limit):
            return False
        db.event(investigation_id, "grounding_new", complaint_key, stage="grounding", conn=conn)
        return True


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
    reuse = _reuse(tc.settings)
    ckey, complaint_key, versions = cache_key(complaint, population, tc.settings)
    if reuse["enabled"]:
        hit = await asyncio.to_thread(cache_store(tc).knowledge_cache_get, ckey, reuse["max_age_days"],
                                      reuse["gap_max_age_days"])
        if hit is not None:
            queries, cands, reads, items = await from_cache(tc, hit)
            gap = hit["gap_reason"] if hit["status"] == "gap" else None

            def commit_reused(conn: sqlite3.Connection) -> None:
                write_run(tc.db, conn, inv, key, population, descriptor, queries, cands, reads, items, gap, kv_next,
                          {"reused": True}, reused_from=hit["id"])
            return commit_reused
    if not await asyncio.to_thread(claim_new_slot, tc.db, inv["id"], key, cfg.get("max_new_complaints")):
        def commit_budget_gap(conn: sqlite3.Connection) -> None:
            write_run(tc.db, conn, inv, key, population, descriptor, [], [], [], [], BUDGET_GAP, kv_next,
                      {"broad_case": True})
            tc.db.event(inv["id"], "broad_case", key, level="warning", stage="grounding", conn=conn,
                        data={"note": "More complaints than one investigation grounds; consider a separate "
                                      "investigation for this one."})
        return commit_budget_gap
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

    if reuse["enabled"] and cacheable(queries, failures, gap):
        await asyncio.to_thread(cache_store(tc).knowledge_cache_put, ckey, complaint_key=complaint_key,
                                mesh_ui=descriptor["ui"] if descriptor else None, population=population,
                                versions=versions, status="gap" if gap else "ok", gap_reason=gap,
                                payload=cache_payload(queries, reads, items))

    def commit(conn: sqlite3.Connection) -> None:
        write_run(tc.db, conn, inv, key, population, descriptor, queries, cands, reads, items, gap, kv_next,
                  {"failures": len(failures), "dropped_attestations": dropped})
    return commit


def write_run(db, conn, inv, key, population, descriptor, queries, cands, reads, items, gap, kv_next, extra,
              reused_from: int | None = None) -> None:
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
        "knowledge_version": kv_next, "reused_from": reused_from})
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


def kind_lists(items: list[M.Item]) -> dict[str, list[M.Item]]:
    """Items per kind, for kinds that have at least one guideline item (curated-only kinds need no merge)."""
    out: dict[str, list[M.Item]] = {}
    for item in items:
        out.setdefault(item.kind, []).append(item)
    return {k: v for k, v in out.items() if any(i.origin != "curated" for i in v) and len(v) > 1}


def group_material(items: list[M.Item]) -> str:
    lines = [f"X{n}: [{i.origin}; population {i.population or 'all'}] {i.text}" for n, i in enumerate(items, 1)]
    return llm.material("\n".join(lines), kind="merge")


def _groups_validator(n: int):
    def check(result: MergeGroups) -> None:
        seen: set[str] = set()
        for g in result.groups:
            for x in [g.keep, *g.same]:
                if not re.fullmatch(r"X\d+", x) or not 1 <= int(x[1:]) <= n:
                    raise ValueError(f"unknown item {x}")
                if x in seen:
                    raise ValueError(f"{x} appears in more than one group")
                seen.add(x)
    return check


def same_pairs(items: list[M.Item], result: MergeGroups) -> set[frozenset]:
    """Every pair inside a judged group (same kind and population only)."""
    out: set[frozenset] = set()
    for g in result.groups:
        members = [items[int(x[1:]) - 1] for x in [g.keep, *g.same]]
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if (a.population or "all") == (b.population or "all"):
                    out.add(frozenset((a.key, b.key)))
    return out


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
    lists = kind_lists(pool)
    same: set[frozenset] = set()
    if lists:
        ctx = tc.services.with_task(task_id=tc.task["id"], step="ground_merge", investigation_id=inv["id"])
        system = llm.base_system() + "\n\n" + llm.prompt(MERGE_VERSION)

        async def judge(items: list[M.Item]) -> set[frozenset]:
            m = await ensemble.single(ctx, "grounding.merge", MergeGroups, system, group_material(items),
                                      prompt_version=MERGE_VERSION, validate=_groups_validator(len(items)))
            return same_pairs(items, m.value)  # type: ignore[arg-type]

        for found in await asyncio.gather(*[judge(items) for items in lists.values()]):
            same |= found
    pairs = sorted(same, key=sorted)
    result = M.merge(pool, same=same)
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
    return [(c["key"], pop) for c in (inv.get("complaints") or [])[:int(cfg["max_complaints"])]]  # chief first


def stage() -> StageDef:
    def on_enter(sc: StageContext) -> None:
        pending = []
        for key, pop in wanted(sc.inv, sc.settings):
            if not sc.conn.execute("SELECT 1 FROM grounding_runs WHERE investigation_id = ? AND complaint_key = ? "
                                   "AND population = ?", (sc.id, key, pop)).fetchone():
                pending.append((key, pop))
        for n, (key, pop) in enumerate(pending):  # in complaint order, so new-grounding slots go to the chief first
            sc.queue("ground", f"ground:{key}:{pop}", {"complaint": key, "population": pop}, priority=20 + n)
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
