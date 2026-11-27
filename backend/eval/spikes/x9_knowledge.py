"""Spike X9 (PLAN 1A.11): knowledge extraction on 6 guidelines from 3 body systems.

    .venv/Scripts/python -m eval.spikes.x9_knowledge fetch     # full texts → results/x9/<pmcid>.json (snapshots)
    .venv/Scripts/python -m eval.spikes.x9_knowledge find      # print red-flag-like sentences with IDs (for the gold list)
    .venv/Scripts/python -m eval.spikes.x9_knowledge run       # live: grounding extraction + verification (profile dev_openai)
    .venv/Scripts/python -m eval.spikes.x9_knowledge score     # recall vs eval/spikes/x9_gold.yaml + attestation review sheet
    ... run --profile dev_ollama / score --profile dev_ollama  # another profile: its own cache, results and matches
    ... run --profile dev_ollama --model qwen3:4b --ctx 24576 --pack 10000 [--no-think] [--only PMC1,PMC2]
        # another local model in every role; score with the same --profile/--model

Pass: ≥ 90% of hand-listed red flags extracted; ≥ 95% of red-flag attestations supported (judged by hand from the
review sheet `results/x9/attestations.md`, recorded in x9_gold.yaml under `attestation_review`).
The gold red flags are listed by hand from each guideline's text (agent-drafted, like the floor; see docs/spikes.md).
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
from app.research.steps import grounding as G

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "x9"
GOLD = HERE / "x9_gold.yaml"
GUIDELINES = [  # (pmcid, system, complaint text used in the prompt)
    ("PMC6734476", "neurology", "Headache"),
    ("PMC13405573", "neurology", "Headache"),
    ("PMC8870422", "musculoskeletal", "Back pain (children and adolescents)"),
    ("PMC11364714", "ent_vestibular", "Acute vertigo or dizziness"),
    ("PMC9380508", "digestive", "Dyspepsia (upper abdominal pain or discomfort)"),
    ("PMC8831363", "digestive", "Dyspepsia (upper abdominal pain or discomfort)"),
]
FIND = re.compile(r"red.?flag|alarm|warning sign|urgent|emergenc|refer|serious|sinister|malignan|cancer|"
                  r"cauda equina|fracture|infection|weight loss|bleed|dysphag|vomit|thunderclap|papilloedema|"
                  r"papilledema|neurolog|fever|new.onset|older than|aged? (?:over|above)|> ?\d\d years", re.I)


async def fetch() -> None:
    from app.literature.enrich import enrich
    from app.literature.sources import europepmc
    OUT.mkdir(parents=True, exist_ok=True)
    async with transport.client(timeout=90) as client:
        for pmcid, system, _ in GUIDELINES:
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
            (OUT / f"{pmcid}.json").write_text(json.dumps({
                "pmcid": pmcid, "system": system, "title": article.title, "year": article.year,
                "coverage": snap.coverage, "snapshot": snap.to_dict()}, ensure_ascii=False), "utf-8")
            print(f"{pmcid}: {snap.coverage}, {len(snap.segments)} segments — {article.title[:80]}")


def load(pmcid: str) -> tuple[dict, Snapshot]:
    data = json.loads((OUT / f"{pmcid}.json").read_text("utf-8"))
    return data, Snapshot.from_dict(data["snapshot"])


def find(only: str | None = None) -> None:
    for pmcid, _, _ in GUIDELINES:
        if only and pmcid != only:
            continue
        data, snap = load(pmcid)
        print(f"\n===== {pmcid} {data['title'][:90]}")
        for s in snap.segments:
            if FIND.search(s.text):
                print(f"[{s.seg_id}] {s.text[:400]}")


def suffix(label: str) -> str:
    """dev_openai keeps the original file names; another run label gets its own cache, results and sheet."""
    return "" if label == "dev_openai" else f".{label}"


def match_key(label: str) -> str:
    return "matched_by" if label == "dev_openai" else f"matched_by_{label}"


def run_label(profile: str, model: str | None) -> str:
    """dev_openai, dev_ollama, or dev_ollama-qwen3_4b for another local model."""
    return profile if not model else f"{profile}-{re.sub(r'[^a-z0-9]+', '_', model.lower()).strip('_')}"


def context(profile: str = "dev_openai", overlay: dict | None = None, label: str | None = None) -> llm.CallContext:
    st = S.build(profile, overlay)
    db = ResearchDB(OUT / f"x9{suffix(label or profile)}.sqlite3")  # cache: reruns reuse the model answers
    db.init()
    return llm.CallContext(db=db, settings=st, providers=default_providers(st), limiters=Limiters({}),
                           prices=PriceTable())


async def run(profile: str = "dev_openai", overlay: dict | None = None, label: str | None = None,
              only: list[str] | None = None) -> None:
    import time
    label = label or profile
    ctx = context(profile, overlay, label)
    cfg = G._cfg(ctx.settings.model_dump(mode="json"))
    results = {}
    for pmcid, system, complaint in GUIDELINES:
        if only and pmcid not in only:
            continue
        data, snap = load(pmcid)
        packs = pack_snapshot(snap, int(cfg["pack_tokens"])).packs  # every pack (no cap): X9 measures recall
        started = time.monotonic()
        read = G.Read(G.Cand(None, [], [], 0), None, snap, packs, data["coverage"])  # type: ignore[arg-type]
        items, dropped = await G.extract(ctx, {"text": complaint}, "general", [read], dual=True)
        await G.verify_items(ctx, {"text": complaint}, items, [read])
        results[pmcid] = {"system": system, "packs": len(packs), "dropped": dropped,
                          "seconds": round(time.monotonic() - started, 1), "items": [
            {"kind": i.kind, "text": i.text, "population": i.population, "attributes": i.attributes,
             "attestations": [{k: a.get(k) for k in ("sentence_ids", "quote", "model", "verdict")}
                              for a in i.attestations]} for i in items]}
        flags = [i for i in items if i.kind == "red_flag"]
        print(f"{pmcid}: {len(packs)} packs, {len(items)} items, {len(flags)} red flags, {dropped} dropped, "
              f"{results[pmcid]['seconds']} s", flush=True)
        (OUT / f"extraction{suffix(label)}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                                              "utf-8")  # after each guideline: a stop keeps the rest


def score(profile: str = "dev_openai") -> None:  # `profile` is the run label (see run_label)
    gold = yaml.safe_load(GOLD.read_text("utf-8"))
    res = json.loads((OUT / f"extraction{suffix(profile)}.json").read_text("utf-8"))
    key = match_key(profile)
    total = found = 0
    lines = ["# X9 red-flag attestations (judge each: supports / partial / no)", ""]
    for pmcid, g in gold["guidelines"].items():
        if pmcid not in res:
            print(f"\n== {pmcid}: not in the results (run stopped?)")
            continue
        r = res[pmcid]
        flags = [i for i in r["items"] if i["kind"] == "red_flag"]
        print(f"\n== {pmcid} ({r['system']}): {len(g['red_flags'])} gold, {len(flags)} extracted red flags")
        for gf in g["red_flags"]:
            total += 1
            hit = gf.get(key)  # filled in by hand after reading the list below
            ok = bool(hit)
            found += ok
            print(f"  [{'x' if ok else ' '}] {gf['flag']}" + (f"  ← {hit}" if hit else ""))
        print("  extracted:")
        for i, f in enumerate(flags, 1):
            print(f"   {i:2}. {f['text']} ({f['attributes'].get('urgency')}, {f['population']})")
        lines += [f"## {pmcid}", ""]
        for i, f in enumerate(flags, 1):
            for a in f["attestations"]:
                lines.append(f"- **{i}. {f['text']}** — verdict `{a['verdict']}` · {', '.join(a['sentence_ids'])}  ")
                lines.append(f"  > {a['quote'][:600]}")
        lines.append("")
    (OUT / f"attestations{suffix(profile)}.md").write_text("\n".join(lines), "utf-8")
    if total:
        print(f"\nRecall: {found}/{total} = {found / total:.0%} (pass ≥ 90%)")
    review = gold.get("attestation_review")
    if review:
        ok, n = review["supported"], review["judged"]
        print(f"Attestations supported (hand review): {ok}/{n} = {ok / n:.0%} (pass ≥ 95%)")


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console can't print ≥
    cmd = argv[0] if argv else "score"
    def opt(name: str, default=None):
        return argv[argv.index(name) + 1] if name in argv else default
    profile = opt("--profile", "dev_openai")
    model = opt("--model")  # another local model in every role (with --profile dev_ollama)
    label = run_label(profile, model)
    overlay = S.local_model_overlay(model, thinking="--no-think" not in argv, num_ctx=int(opt("--ctx", 32768)),
                                    pack_tokens=int(opt("--pack", 12000))) if model else None
    only = opt("--only", "").split(",") if "--only" in argv else None
    if cmd == "fetch":
        asyncio.run(fetch())
    elif cmd == "find":
        find(argv[1] if len(argv) > 1 else None)
    elif cmd == "run":
        with transport.use_transport(transport.RecordTransport(OUT / f"recordings{suffix(label)}")):
            asyncio.run(run(profile, overlay, label, only))
    else:
        score(label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
