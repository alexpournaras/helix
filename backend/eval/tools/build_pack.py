"""Build a curated complaint pack draft (PLAN B7.2, 1C.3).

    .venv/Scripts/python -m eval.tools.build_pack --complaint tinnitus --term Tinnitus [--profile quality]
        [--out DIR] [--no-source-check]

1. Grounding pass at maximum depth: the real `grounding` stage (search → dual screening → selection → fetch →
   dual extraction → attestation verification) on a synthetic investigation that holds only the complaint.
   It runs in a temporary data folder with its own literature library; nothing touches your data.
2. The strongest model (`pack_draft` role) drafts the pack from the verified and partial guideline items only
   (curated floor and pack items are left out), citing them by `K` number.
3. Code checks the draft: `K` numbers become sources (the papers that attest them), every red-flag rule must pass
   its own examples (validator.check_rule), no doses in management topics, item IDs unique, and every source and
   seed paper is checked live (URL and PubMed esummary). Entries that fail are dropped and listed.
4. Output for human review (Part G.6), never loaded by the app: `knowledge/packs/drafts/<complaint>.yaml`,
   `<complaint>.sources.yaml` (new source entries) and `<complaint>.report.md` (evidence per entry, dropped
   entries, source checks, comparison with the current pack). A human merges it and marks it reviewed.

Live: model calls (grounding + drafting) and literature requests. The case is synthetic (the complaint name only).
Model answers go to the shared eval cache (eval/results/llm_cache.sqlite3), so a stopped build resumes.
Cost (estimate, docs/progress.md): about an X9 run or more on dev_openai (~2–3M tokens: 8 sources, every pack).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from app.research import ensemble, llm, settings as S
from app.research.db import ResearchDB
from app.research.knowledge import loader, validator
from app.research.schemas import PackDraft

DRAFT_VERSION = "pack.draft.v1"
DRAFTS_DIR = loader.PACKS_DIR / "drafts"
# Maximum depth for the grounding pass (B7.2 step 1): more sources, more screening, every pack of every source.
MAX_DEPTH = {"grounding": {"max_sources_per_complaint": 8, "max_screened": 80, "max_results_per_query": 40,
                           "max_packs_per_source": None, "max_per_organisation": 2}}
POPULATION_FACTS = {"infant": {"age_months": 3}, "child": {"age_years": 8}, "pregnancy": {"pregnant": True},
                    "older": {"age_years": 78}, "immunocompromised": {"immunocompromised": True}}
DETAIL_KEYS = ("features", "urgency", "question", "key_features", "typical_population", "why", "indication", "context")


# ---------- 1. grounding pass ----------

async def ground(ctx: llm.CallContext, db: ResearchDB, *, key: str, text: str, descriptor: dict) -> int:
    """Run the grounding stage for one complaint; returns the synthetic investigation's ID."""
    from app.research.runner import Worker
    from app.research.steps import build_pipeline
    complaint = {"key": key, "text": text, "seg_ids": [], "mesh": [descriptor], "mesh_dropped": []}
    inv_id = db.create_investigation(f"Synthetic pack build: {text}.", profile=ctx.settings.profile,
                                     settings_snapshot=ctx.settings.model_dump(mode="json"), synthetic=True,
                                     title=f"build_pack:{key}")
    db.update_investigation(inv_id, stage="grounding", complaints=[complaint], special_populations=[])
    pipeline, _ = build_pipeline(ctx, stop_at="safety")  # grounding → merge → (stop on entering safety)
    worker = Worker(db, pipeline, services=ctx, max_parallel=4)
    try:
        await worker.machine.advance(inv_id)
        await worker.run_until_idle()
    finally:
        await worker.stop()
    return inv_id


@dataclass
class Knowledge:
    items: list[dict]  # {ref: K1, kind, text, attributes, population, trust, papers: [paper keys], quotes}
    papers: dict[str, dict]  # paper key → {pmid, pmcid, doi, title, journal, year, design}


def collect(db: ResearchDB, inv_id: int) -> Knowledge:
    """The guideline items grounding kept (verified or partial; merged-away ones too) with their papers."""
    rows = db.query("SELECT * FROM knowledge_items WHERE investigation_id = ? AND origin = 'guideline' AND "
                    "trust IN ('verified', 'partial') ORDER BY kind, id", (inv_id,))
    papers: dict[str, dict] = {}
    items = []
    for row in rows:
        atts = db.query(
            "SELECT a.sentence_ids, a.quote, a.verdict, c.pmid, c.pmcid, c.doi, c.title, c.journal, c.year, "
            "s.design FROM knowledge_attestations a JOIN paper_reads r ON r.id = a.read_id "
            "JOIN candidates c ON c.id = r.candidate_id LEFT JOIN screenings s ON s.candidate_id = c.id "
            "WHERE a.item_id = ? AND a.verdict IN ('supported', 'partial') ORDER BY a.id", (row["id"],))
        keys = []
        for a in atts:
            pk = paper_key(a)
            if pk is None:
                continue
            papers.setdefault(pk, {k: a[k] for k in ("pmid", "pmcid", "doi", "title", "journal", "year", "design")})
            keys.append(pk)
        if not keys:
            continue
        items.append({"ref": f"K{len(items) + 1}", "kind": row["kind"], "text": row["text"],
                      "attributes": row["attributes"] or {}, "population": row["population"], "trust": row["trust"],
                      "papers": list(dict.fromkeys(keys)), "quotes": [a["quote"] for a in atts]})
    return Knowledge(items, papers)


def paper_key(p: dict) -> str | None:
    if p.get("pmid"):
        return f"pmid_{p['pmid']}"
    if p.get("pmcid"):
        return f"pmc_{str(p['pmcid']).lower().removeprefix('pmc')}"
    if p.get("doi"):
        return "doi_" + re.sub(r"[^a-z0-9]+", "_", str(p["doi"]).lower()).strip("_")
    return None


def paper_source(key: str, p: dict) -> dict:
    """A sources.yaml entry for a paper that attests pack items."""
    if p.get("pmid"):
        url = f"https://pubmed.ncbi.nlm.nih.gov/{p['pmid']}/"
    elif p.get("pmcid"):
        url = f"https://pmc.ncbi.nlm.nih.gov/articles/{p['pmcid']}/"
    else:
        url = f"https://doi.org/{p['doi']}"
    title = (p.get("title") or key).strip()
    return {"id": key, "title": title + (f" ({p['year']})" if p.get("year") else ""),
            "publisher": journal if len(journal := (p.get("journal") or "").strip()[:120]) >= 2 else "PubMed",
            "url": url,
            "kind": "guideline" if p.get("design") == "guideline" else "review"}


# ---------- 2. drafting ----------

def material(complaint: str, k: Knowledge) -> str:
    lines = [f"Complaint: {complaint}", "", "Knowledge items:"]
    for it in k.items:
        details = "; ".join(f"{name}: {', '.join(v) if isinstance(v, list) else v}"
                            for name in DETAIL_KEYS if (v := it["attributes"].get(name)))
        lines.append(f"{it['ref']} [{it['kind']}] {it['text']}" + (f" — {details}" if details else "")
                     + f" (population: {it['population']}; trust: {it['trust']})")
    return llm.material("\n".join(lines), kind="pack_knowledge")


async def draft(ctx: llm.CallContext, complaint: str, k: Knowledge) -> tuple[PackDraft, str]:
    system = llm.base_system() + "\n\n" + llm.prompt(DRAFT_VERSION)
    refs = {it["ref"] for it in k.items}

    def check(d: PackDraft) -> None:
        bad = sorted({r for group in (d.red_flags, d.etiologies, d.history_checklist, d.evaluations,
                                      d.management_topics) for e in group for r in e.knowledge if r not in refs})
        if bad:
            raise ValueError(f"cite only listed items; unknown: {', '.join(bad[:10])}")
    m = await ensemble.single(ctx, "pack_draft", PackDraft, system, material(complaint, k),
                              prompt_version=DRAFT_VERSION, validate=check)
    return m.value, m.model.label()  # type: ignore[return-value]


# ---------- 3. assembly and checks ----------

@dataclass
class Built:
    pack: dict
    sources: list[dict]  # new source entries (papers)
    dropped: list[str] = field(default_factory=list)
    evidence: dict[str, list[str]] = field(default_factory=dict)  # entry id → K refs


def phrase(text: str) -> str:
    """A plain phrase as a whole-word regex (spaces match any run of whitespace)."""
    return r"\s+".join(re.escape(w) for w in text.lower().split())


def assemble(d: PackDraft, k: Knowledge, *, key: str, name: str, mesh_ui: str, entry_terms: list[str],
             built_by: str) -> Built:
    by_ref = {it["ref"]: it for it in k.items}
    out = Built(pack={}, sources=[])
    used: set[str] = set()

    def sources_of(entry_id: str, refs: list[str]) -> list[str]:
        keys = [p for r in refs if r in by_ref for p in by_ref[r]["papers"]]
        out.evidence[entry_id] = [r for r in refs if r in by_ref]
        used.update(keys)
        return list(dict.fromkeys(keys))
    seen: set[str] = set()

    def unique(slug: str) -> str | None:
        if slug in seen:
            out.dropped.append(f"{slug}: duplicate id")
            return None
        seen.add(slug)
        return slug

    complaint_terms = list(dict.fromkeys(t.lower() for t in [name, *entry_terms, *d.synonyms] if t.strip()))
    red_flags = []
    for f in d.red_flags:
        rid = f"{key}.{f.slug}"
        if not unique(f.slug):
            continue
        facts = POPULATION_FACTS.get(f.population, {})
        red_flags.append({
            "id": rid, "area": f.area, "level": f.level, "population": f.population, "title": f.title,
            "fires": {"all": [{"text": [phrase(t) for t in complaint_terms]},
                              {"mention": [phrase(t) for t in f.triggers]}]},
            "unknown": f.unknown_attribute, "rule_out_question": f.rule_out_question,
            "sources": sources_of(rid, f.knowledge),
            "examples": {"fires": [{"text": t, **({"facts": facts} if facts else {})} for t in f.example_fires],
                         "no_fire": [{"text": t} for t in f.example_no_fire],
                         "unknown": [{"text": t} for t in f.example_unknown]}})
    sections: dict[str, list[dict]] = {"etiologies": [], "history_checklist": [], "evaluations": [],
                                       "management_topics": []}
    for e in d.etiologies:
        if unique(e.slug):
            sections["etiologies"].append({"id": e.slug, "name": e.name, "key_features": e.key_features,
                                           "discriminating_questions": e.discriminating_questions,
                                           "typical_evaluations": e.typical_evaluations, "red_flag": e.red_flag,
                                           "sources": sources_of(e.slug, e.knowledge)})
    for h in d.history_checklist:
        if unique(h.slug):
            sections["history_checklist"].append({"id": h.slug, "attribute": h.attribute, "priority": h.priority,
                                                  "question": h.question, "sources": sources_of(h.slug, h.knowledge)})
    for v in d.evaluations:
        if unique(v.slug):
            sections["evaluations"].append({"id": v.slug, "test": v.test, "indication": v.indication,
                                            "sources": sources_of(v.slug, v.knowledge)})
    for t in d.management_topics:
        if unique(t.slug):
            sections["management_topics"].append({"id": t.slug, "topic": t.topic, "context": t.context,
                                                  "sources": sources_of(t.slug, t.knowledge)})
    seeds = [{"pmid": p["pmid"], "kind": "guideline" if p.get("design") == "guideline" else "systematic_review",
              "title": p.get("title") or ""}
             for pk, p in k.papers.items() if pk in used and p.get("pmid")
             and p.get("design") in ("guideline", "systematic_review")]
    out.pack = {"version": 1, "complaint": key, "mesh": [mesh_ui], "built_by": built_by,
                "review": {"status": "unreviewed", "reviewers": [],
                           "notes": "Drafted by build_pack.py from verified guideline items. Needs clinician review."},
                "red_flags": red_flags, **sections,
                "vocabulary": {"mesh_terms": [name], "synonyms": list(d.synonyms)}, "seed_literature": seeds}
    out.sources = [paper_source(pk, k.papers[pk]) for pk in sorted(used)]
    return out


def check(built: Built, known: dict[str, loader.Source]) -> loader.Pack:
    """Drop the entries that fail the knowledge checks (listed in `built.dropped`); returns the valid pack."""
    sources = {**known, **{s["id"]: loader.Source.model_validate(s) for s in built.sources}}
    pack = built.pack
    rules = []
    for raw in pack["red_flags"]:
        if not raw["sources"]:
            built.dropped.append(f"{raw['id']}: no attested source")
            continue
        try:
            rule = loader.Rule.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - report and drop
            built.dropped.append(f"{raw['id']}: invalid rule ({str(exc).splitlines()[0][:160]})")
            continue
        problems = validator.check_rule(rule, sources)
        if problems:
            built.dropped += [f"{raw['id']}: {p.split(': ', 1)[-1]}" for p in problems]
            continue
        rules.append(raw)
    pack["red_flags"] = rules
    models = {"etiologies": loader.Etiology, "history_checklist": loader.HistoryItem,
              "evaluations": loader.Evaluation, "management_topics": loader.ManagementTopic}
    for section, model in models.items():
        kept = []
        for raw in pack[section]:
            if not raw["sources"]:
                built.dropped.append(f"{raw['id']}: no attested source")
                continue
            if section == "management_topics" and validator.DOSE.search(f"{raw['topic']} {raw['context']}"):
                built.dropped.append(f"{raw['id']}: management topics never contain doses")
                continue
            try:
                model.model_validate(raw)
            except Exception as exc:  # noqa: BLE001
                built.dropped.append(f"{raw['id']}: invalid ({str(exc).splitlines()[0][:160]})")
                continue
            kept.append(raw)
        pack[section] = kept
    cited = {s for r in pack["red_flags"] for s in r["sources"]} | \
            {s for sec in models for item in pack[sec] for s in item["sources"]}
    built.sources = [s for s in built.sources if s["id"] in cited]
    pack["seed_literature"] = [p for p in pack["seed_literature"] if f"pmid_{p['pmid']}" in cited]
    return loader.Pack.model_validate(pack)


SourceCheck = Callable[[list[dict], list[str]], Awaitable[dict]]


async def live_source_check(sources: list[dict], pmids: list[str]) -> dict:
    """{"sources": {id: {status, …}}, "papers": {pmid: {status, title}}}: every URL fetched, PMIDs via esummary."""
    import httpx
    from app.literature.sources.ncbi import eutil
    from eval.tools.check_sources import UA, _check_url
    out: dict = {"sources": {}, "papers": {}}
    async with httpx.AsyncClient(timeout=40, follow_redirects=True, headers={"User-Agent": UA}) as client:
        for s in sources:
            out["sources"][s["id"]] = {"url": s["url"], **await _check_url(client, s["url"])}
        if pmids:
            data = (await eutil(client, "esummary.fcgi", {"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})).json()
            result = data.get("result") or {}
            for pmid in pmids:
                rec = result.get(pmid) or {}
                ok = isinstance(rec, dict) and rec.get("title") and "error" not in rec
                out["papers"][pmid] = {"status": "ok" if ok else "missing", "title": (rec.get("title") or "")[:200]}
    return out


# ---------- 4. review files ----------

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


LABELS = {"red_flags": "title", "etiologies": "name", "history_checklist": "question", "evaluations": "test",
          "management_topics": "topic"}
SECTIONS = tuple(LABELS)


def _label(section: str, item: Any) -> str:
    return getattr(item, LABELS[section])


def compare(new: loader.Pack, old: loader.Pack | None, threshold: float = 0.4) -> dict[str, dict[str, list[str]]]:
    """Per section: entries of the current pack with no similar draft entry, and draft entries that are new."""
    out: dict[str, dict[str, list[str]]] = {}
    for section in SECTIONS:
        a = [_label(section, x) for x in getattr(new, section)]
        b = [_label(section, x) for x in getattr(old, section)] if old else []

        def similar(x: str, pool: list[str]) -> bool:
            wx = _words(x)
            return any(len(wx & _words(y)) / max(1, len(wx | _words(y))) >= threshold for y in pool)
        out[section] = {"only_current": [y for y in b if not similar(y, a)], "only_draft": [x for x in a if not similar(x, b)]}
    return out


def report(pack: loader.Pack, built: Built, k: Knowledge, checks: dict | None, diff: dict, meta: dict) -> str:
    by_ref = {it["ref"]: it for it in k.items}
    lines = [f"# Pack draft: {pack.complaint}", "",
             f"Built {meta['date']} by `build_pack.py` (profile `{meta['profile']}`, drafted by `{meta['model']}`, "
             f"prompt `{DRAFT_VERSION}`). **Unreviewed**: a clinician reviews every entry before it is merged.", "",
             f"Grounding: {len(k.papers)} papers, {len(k.items)} verified/partial items. Draft: "
             + ", ".join(f"{len(getattr(pack, s))} {s}" for s in SECTIONS) + ".", ""]
    lines += ["## Dropped by the checks", ""] + ([f"- {d}" for d in built.dropped] or ["- none"]) + [""]
    lines += ["## Sources", ""]
    if checks is None:
        lines.append("- Not checked live (`--no-source-check`): run it before merging.")
    else:
        for s in built.sources:
            st = checks["sources"].get(s["id"], {}).get("status", "not checked")
            lines.append(f"- `{s['id']}` {st} — {s['title']} ({s['url']})")
        for pmid, v in checks["papers"].items():
            lines.append(f"- seed PMID {pmid}: {v['status']}")
    lines += ["", "## Compared with the current pack", ""]
    for section, d in diff.items():
        lines.append(f"- **{section}**: in the current pack only: {', '.join(d['only_current']) or '—'}; "
                     f"new in the draft: {', '.join(d['only_draft']) or '—'}")
    lines += ["", "## Evidence per entry", ""]
    for section in SECTIONS:
        for item in getattr(pack, section):
            lines.append(f"### {section}: `{item.id}` — {_label(section, item)}")
            for ref in built.evidence.get(item.id, []):
                it = by_ref[ref]
                lines.append(f"- {ref} [{it['kind']}, {it['trust']}] {it['text']}")
                lines += [f"  > {q[:500]}" for q in it["quotes"][:2]]
            lines.append("")
    return "\n".join(lines)


def write(out_dir: Path, pack: loader.Pack, built: Built, text: str) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    key = pack.complaint
    header = (f"# DRAFT pack: {key} (build_pack.py, {date.today().isoformat()}). Unreviewed: see {key}.report.md.\n"
              "# Not loaded by the app. A human reviews it, merges it into knowledge/packs/ and sources.yaml,\n"
              "# runs eval/tools/check_sources.py and records the review (Part G.6).\n")
    paths = [out_dir / f"{key}.yaml", out_dir / f"{key}.sources.yaml", out_dir / f"{key}.report.md"]
    data = pack.model_dump(mode="json", exclude_defaults=False)
    paths[0].write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120), "utf-8")
    paths[1].write_text("# New sources cited by the draft (merge into sources.yaml).\n"
                        + yaml.safe_dump({"sources": built.sources}, sort_keys=False, allow_unicode=True, width=120),
                        "utf-8")
    paths[2].write_text(text, "utf-8")
    return paths


# ---------- the whole build ----------

async def build(ctx: llm.CallContext, db: ResearchDB, *, key: str, term: str, descriptor: dict,
                out_dir: Path = DRAFTS_DIR, source_check: SourceCheck | None = live_source_check,
                current: loader.Pack | None = None) -> dict:
    inv_id = await ground(ctx, db, key=key, text=term, descriptor=descriptor)
    k = collect(db, inv_id)
    if not k.items:
        raise RuntimeError(f"Grounding found no verified guideline items for {term!r}: nothing to draft "
                           f"(see grounding_runs: {[r['status'] for r in db.query('SELECT status FROM grounding_runs')]}).")
    d, model = await draft(ctx, term, k)
    stamp = date.today().isoformat()
    built = assemble(d, k, key=key, name=descriptor.get("name") or term, mesh_ui=descriptor["ui"],
                     entry_terms=[], built_by=f"build_pack.py {stamp}: grounding ({len(k.papers)} papers) + {model}")
    pack = check(built, loader.load_sources())
    checks = None
    if source_check is not None:
        checks = await source_check(built.sources, [p.pmid for p in pack.seed_literature if p.pmid])
        bad = [sid for sid, v in checks["sources"].items() if v.get("status") != "ok"]
        bad += [f"pmid {p}" for p, v in checks["papers"].items() if v.get("status") != "ok"]
        built.dropped += [f"source {b}: did not resolve (fix or remove before merging)" for b in bad]
    if current is None:
        current = loader.load_packs().get(key)
    text = report(pack, built, k, checks, compare(pack, current),
                  {"date": stamp, "profile": ctx.settings.profile, "model": model})
    paths = write(Path(out_dir), pack, built, text)
    return {"investigation": inv_id, "items": len(k.items), "papers": len(k.papers), "dropped": built.dropped,
            "paths": [str(p) for p in paths],
            "counts": {s: len(getattr(pack, s)) for s in SECTIONS}}


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--complaint", required=True, help="pack key, e.g. tinnitus")
    parser.add_argument("--term", required=True, help="the complaint as MeSH knows it, e.g. Tinnitus")
    parser.add_argument("--profile", default="quality", help="quality (strongest models, default) or dev_openai")
    parser.add_argument("--out", default=str(DRAFTS_DIR))
    parser.add_argument("--no-source-check", action="store_true")
    args = parser.parse_args(argv)
    from app.literature.db import LiteratureDB
    from app.research import mesh, preflight
    from app.research.prices import PriceTable
    from app.research.providers import default_providers
    from app.research.ratelimit import Limiters
    from eval.run import local_overrides, shared_cache
    settings = S.build(args.profile, S.deep_merge(local_overrides(), MAX_DEPTH))
    if args.profile.startswith("dev"):  # drafting is a one-off quality task: the dev model at high effort
        settings = S.build(args.profile, S.deep_merge(local_overrides(), {
            **MAX_DEPTH, "roles": {"pack_draft": {"members": [{"model": S.DEV_OPENAI, "effort": "high"}]}}}))
    tmp = Path(tempfile.mkdtemp(prefix=f"helix-pack-{args.complaint}-"))
    db = ResearchDB(tmp / "research.sqlite3")
    db.init()
    providers = default_providers(settings)
    pf = preflight.run(settings, providers, preflight.ProbeStore(tmp / "probes.json"), synthetic=True,
                       require_probes=False)
    if not pf.ok:
        print("Pre-flight failed:", *pf.errors, sep="\n  ")
        return 1
    found = await mesh.lookup(args.term, db)
    if found is None:
        print(f"No MeSH descriptor for {args.term!r}.")
        return 1
    descriptor = {"ui": found.ui, "name": found.name, "tree_numbers": found.tree_numbers, "systems": []}
    ctx = llm.CallContext(db=db, settings=settings, providers=providers, limiters=Limiters({}), prices=PriceTable(),
                          literature=LiteratureDB(tmp / "literature.db"),
                          cache=shared_cache())  # a stopped build resumes from the answers it already paid for
    result = await build(ctx, db, key=args.complaint, term=args.term, descriptor=descriptor, out_dir=Path(args.out),
                         source_check=None if args.no_source_check else live_source_check)
    usage = db.usage(result["investigation"])
    print(json.dumps({**result, "tokens": usage.get("tokens"), "cost": usage.get("cost")}, indent=1))
    print(f"Data folder (kept for inspection): {tmp}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.exit(asyncio.run(main()))
