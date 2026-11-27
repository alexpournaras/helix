"""Spike X3 (PLAN 1A.11): dual claim extraction (1E.1 `read.claims`) on 6 open-access papers from 3 body systems.

    .venv/Scripts/python -m eval.spikes.x3_dual_extraction fetch   # full texts → results/x3/<pmcid>.json (snapshots)
    .venv/Scripts/python -m eval.spikes.x3_dual_extraction run     # live: dual extraction + reconciliation (dev_openai)
    .venv/Scripts/python -m eval.spikes.x3_dual_extraction score   # valid IDs per extractor; reconciliation vs x3_gold.yaml
    ... run/score --profile dev_ollama [--model qwen3:4b --ctx 24576 --pack 8000 --no-think] [--only PMC1,PMC2]

Pass: ≥ 90% valid sentence IDs per extractor (IDs that exist in the paper and were shown to the extractor, counted
over every claim it returned, rejected ones included); reconciliation puts ≥ 80% of the hand-paired claims (the
same finding from the two extractors) in one cluster. The pairs are listed by hand from the review sheet
`results/x3/pairs.md` into eval/spikes/x3_gold.yaml (agent-drafted, like the X9 gold list; see docs/spikes.md).

The case summaries and hypotheses below are synthetic and only frame what the extractor looks for.
Costs (estimate, docs/progress.md): ~0.8–1.5M tokens on dev_openai.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import yaml

from app.research import llm, settings as S, transport
from app.research.db import ResearchDB
from app.research.packing import pack_snapshot
from app.research.prices import PriceTable
from app.research.providers import default_providers
from app.research.ratelimit import Limiters
from app.research.segment import Snapshot, paper_snapshot
from app.research.steps import read as R

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "x3"
GOLD = HERE / "x3_gold.yaml"


def _h(key: str, title: str) -> dict:
    return {"key": key, "title": title}


# (pmcid, system, design, synthetic case summary, hypotheses)
PAPERS = [
    ("PMC9891135", "ent", "cohort",
     {"complaint": "ringing in both ears for six months", "age": "adult", "exposures": ["lives next to a motorway"]},
     [_h("noise_exposure", "Environmental or occupational noise exposure causes or worsens tinnitus"),
      _h("age_related_hearing_loss", "Age-related hearing loss underlies the tinnitus")]),
    ("PMC5615572", "ent", "cohort",
     {"complaint": "constant tinnitus for a year", "age": "adult", "exposures": ["works in a loud factory"]},
     [_h("noise_exposure", "Work-related noise exposure is associated with chronic tinnitus"),
      _h("hearing_loss", "Hearing loss accompanies and explains the tinnitus")]),
    ("PMC7388434", "neurology", "cohort",
     {"complaint": "new headache during a viral infection", "age": "adult"},
     [_h("viral_infection_headache", "Headache is a feature of the acute viral infection"),
      _h("prognosis", "Headache is linked to the course or outcome of the infection")]),
    ("PMC5036209", "neurology", "rct",
     {"complaint": "migraine attacks several times a month", "age": "adult"},
     [_h("migraine_prevention", "A preventive treatment reduces migraine frequency"),
      _h("adverse_effects", "Preventive treatments cause side effects such as sleepiness or weight gain")]),
    ("PMC7016109", "digestive", "rct",
     {"complaint": "upper abdominal discomfort after meals despite treatment", "age": "adult"},
     [_h("gluten_sensitivity", "Non-coeliac gluten or wheat sensitivity causes the dyspeptic symptoms"),
      _h("functional_dyspepsia", "The symptoms are functional dyspepsia")]),
    ("PMC10764899", "digestive", "rct",
     {"complaint": "early fullness and upper abdominal pain for months", "age": "adult"},
     [_h("probiotic_benefit", "A probiotic improves functional dyspepsia symptoms"),
      _h("gut_microbiota", "Changes in gut microbiota are linked to the symptoms")]),
]


# ---------- data ----------

async def fetch() -> None:
    from app.literature.enrich import enrich
    from app.literature.sources import europepmc
    OUT.mkdir(parents=True, exist_ok=True)
    async with transport.client(timeout=90) as client:
        for pmcid, system, *_ in PAPERS:
            path = OUT / f"{pmcid}.json"
            if path.exists() and json.loads(path.read_text("utf-8")).get("coverage") == "full":
                print(f"{pmcid}: kept (full text already saved)")
                continue
            got = await europepmc.fetch(client, f"PMC:{pmcid}")
            if got is None:
                print(f"{pmcid}: not found")
                continue
            article, details = await enrich(client, *got, citations=False)
            snap = paper_snapshot(article.abstract, details)
            path.write_text(json.dumps({"pmcid": pmcid, "system": system, "title": article.title, "year": article.year,
                                        "coverage": snap.coverage, "snapshot": snap.to_dict()}, ensure_ascii=False),
                            "utf-8")
            print(f"{pmcid}: {snap.coverage}, {len(snap.segments)} segments — {article.title[:80]}")


def load(pmcid: str) -> tuple[dict, Snapshot]:
    data = json.loads((OUT / f"{pmcid}.json").read_text("utf-8"))
    return data, Snapshot.from_dict(data["snapshot"])


# ---------- run ----------

def suffix(label: str) -> str:
    return "" if label == "dev_openai" else f".{label}"


def run_label(profile: str, model: str | None) -> str:
    return profile if not model else f"{profile}-{re.sub(r'[^a-z0-9]+', '_', model.lower()).strip('_')}"


def context(profile: str, overlay: dict | None, label: str) -> llm.CallContext:
    st = S.build(profile, overlay)
    db = ResearchDB(OUT / f"x3{suffix(label)}.sqlite3")  # cache: reruns reuse the model answers
    db.init()
    return llm.CallContext(db=db, settings=st, providers=default_providers(st), limiters=Limiters({}),
                           prices=PriceTable())


def summarise(ext: R.Extraction) -> dict:
    """Per extractor: numbered claims with cited and valid ID counts; rejected claims; clusters as claim refs."""
    refs: dict[int, str] = {}
    counter: dict[str, int] = {}
    claims = []
    for label, _ref, pack_no, _pass, ok in ext.checked:
        counter[label] = counter.get(label, 0) + 1
        ref = f"{label}:{counter[label]}"
        refs[id(ok)] = ref
        cited = len(dict.fromkeys(ok.claim.sentence_ids))
        claims.append({"extractor": label, "n": counter[label], "ref": ref, "pack": pack_no, "claim": ok.claim.claim,
                       "sentence_ids": ok.sentence_ids, "cited_ids": cited, "valid_ids": len(ok.sentence_ids),
                       "links": [l.hypothesis_key for l in ok.claim.links]})
    rejected = [{"extractor": label, "reason": why, "cited_ids": len(dict.fromkeys(json.loads(raw).get("sentence_ids") or [])),
                 "claim": json.loads(raw).get("claim")} for raw, why, label in ext.rejected]
    clusters = [[refs[id(c.kept[1])], *[refs[id(m[1])] for m in c.members]] for c in ext.clusters]
    return {"claims": claims, "rejected": rejected, "clusters": clusters, "borderline": ext.borderline}


async def run(profile: str, overlay: dict | None, label: str, only: list[str] | None) -> None:
    import time
    ctx = context(profile, overlay, label)
    cfg = R._cfg(ctx.settings.model_dump(mode="json"))
    path = OUT / f"extraction{suffix(label)}.json"
    results = json.loads(path.read_text("utf-8")) if path.exists() else {}
    for pmcid, system, design, summary, hyps in PAPERS:
        if only and pmcid not in only:
            continue
        _, snap = load(pmcid)
        packs = pack_snapshot(snap, int(cfg["max_pack_tokens"])).packs
        started = time.monotonic()
        ext = await R.extract_claims(ctx, snap=snap, packs=packs, summary=summary, hyps=hyps, design=design,
                                     limit=int(cfg["max_claims_per_pack"]), profile_n=None, dual=True, whole=False)
        results[pmcid] = {"system": system, "design": design, "packs": len(packs),
                          "seconds": round(time.monotonic() - started, 1), **summarise(ext)}
        r = results[pmcid]
        print(f"{pmcid}: {len(packs)} packs, {len(r['claims'])} claims, {len(r['rejected'])} rejected, "
              f"{len(r['clusters'])} clusters, {r['borderline']} borderline, {r['seconds']} s", flush=True)
        path.write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")  # after each paper
    write_sheet(results, label)


def write_sheet(results: dict, label: str) -> None:
    """The review sheet for hand pairing: each extractor's claims side by side, with reconciliation's clusters."""
    lines = ["# X3 claims to pair by hand", "",
             "Pair a claim of m0 with a claim of m1 when both state the same finding (same result, population and",
             "direction; wording may differ). Record pairs in x3_gold.yaml as [\"m0:N\", \"m1:N\"].", ""]
    for pmcid, r in results.items():
        lines += [f"## {pmcid} ({r['system']}, {r['design']})", ""]
        for label in ("m0", "m1"):
            lines.append(f"### {label}")
            lines += [f"- `{c['ref']}` {c['claim']} · {', '.join(c['sentence_ids'])}" for c in r["claims"]
                      if c["extractor"] == label]
            lines.append("")
        lines.append("Reconciliation clusters: " + "; ".join("+".join(c) for c in r["clusters"] if len(c) > 1))
        lines.append("")
    (OUT / f"pairs{suffix(label)}.md").write_text("\n".join(lines), "utf-8")


# ---------- scoring (pure; tested in test_research_x3.py) ----------

def id_stats(results: dict) -> dict:
    """Per extractor over all papers: claims (rejected included), cited and valid IDs, valid share."""
    out: dict[str, dict] = {}
    for r in results.values():
        rows = [(c["extractor"], c["cited_ids"], c["valid_ids"], False) for c in r["claims"]]
        rows += [(x["extractor"], x["cited_ids"], 0, True) for x in r["rejected"]]
        for label, cited, valid, rejected in rows:
            s = out.setdefault(label, {"claims": 0, "rejected": 0, "cited_ids": 0, "valid_ids": 0})
            s["claims"] += 1
            s["rejected"] += rejected
            s["cited_ids"] += cited
            s["valid_ids"] += valid
    for s in out.values():
        s["rate"] = s["valid_ids"] / s["cited_ids"] if s["cited_ids"] else None
    return out


def pair_match(results: dict, gold: dict[str, list[list[str]]]) -> dict:
    """Hand pairs that reconciliation put in one cluster, the ones it missed, and its merges that aren't hand pairs."""
    total = matched = 0
    missed, extra = [], []
    for pmcid, pairs in gold.items():
        r = results.get(pmcid)
        if r is None:
            continue
        known = {f"{c['extractor']}:{c['n']}" for c in r["claims"]}
        cluster_of = {ref: i for i, cl in enumerate(r["clusters"]) for ref in cl}
        wanted = set()
        for a, b in pairs:
            if a not in known or b not in known:
                raise ValueError(f"{pmcid}: unknown claim ref in {a}, {b}")
            total += 1
            wanted.add(frozenset((a, b)))
            if cluster_of.get(a) is not None and cluster_of.get(a) == cluster_of.get(b):
                matched += 1
            else:
                missed.append((pmcid, a, b))
        for cl in r["clusters"]:
            for i, a in enumerate(cl):
                for b in cl[i + 1:]:
                    if frozenset((a, b)) not in wanted:
                        extra.append((pmcid, *sorted((a, b))))
    return {"gold_pairs": total, "matched": matched, "rate": matched / total if total else None,
            "missed": missed, "extra": extra}


def score(label: str) -> None:
    results = json.loads((OUT / f"extraction{suffix(label)}.json").read_text("utf-8"))
    print("Valid sentence IDs per extractor (pass ≥ 90%):")
    for name, s in sorted(id_stats(results).items()):
        rate = f"{s['rate']:.1%}" if s["rate"] is not None else "—"
        print(f"  {name}: {s['valid_ids']}/{s['cited_ids']} IDs = {rate}; {s['claims']} claims, {s['rejected']} rejected")
    gold = yaml.safe_load(GOLD.read_text("utf-8")) if GOLD.exists() else {}
    pairs = ((gold or {}).get("pairs") or {}).get(label) or {}
    if not pairs:
        print(f"No hand pairs for {label!r} yet: pair the claims in results/x3/pairs{suffix(label)}.md into "
              f"x3_gold.yaml under pairs.{label}.")
        return
    m = pair_match(results, pairs)
    rate = f"{m['rate']:.1%}" if m["rate"] is not None else "—"
    print(f"Reconciliation: {m['matched']}/{m['gold_pairs']} hand pairs in one cluster = {rate} (pass ≥ 80%)")
    for p in m["missed"]:
        print("  missed:", *p)
    for p in m["extra"]:
        print("  merged without a hand pair:", *p)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    def opt(name: str, default=None):
        return argv[argv.index(name) + 1] if name in argv else default
    cmd = argv[0] if argv else "score"
    profile = opt("--profile", "dev_openai")
    model = opt("--model")
    label = run_label(profile, model)
    overlay = S.local_model_overlay(model, thinking="--no-think" not in argv, num_ctx=int(opt("--ctx", 32768)),
                                    pack_tokens=int(opt("--pack", 12000))) if model else None
    only = opt("--only", "").split(",") if "--only" in argv else None
    if cmd == "fetch":
        asyncio.run(fetch())
    elif cmd == "run":
        with transport.use_transport(transport.RecordTransport(OUT / f"recordings{suffix(label)}")):
            asyncio.run(run(profile, overlay, label, only))
    else:
        score(label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
