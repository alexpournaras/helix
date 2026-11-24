"""Spike X7 (PLAN 1A.11): the paper sentence splitter on >= 200 sentences of real open-access text.

Run from backend/:  .venv/Scripts/python -m eval.spikes.x7_splitter [--review]
Fetches open-access full text (PMC) for papers from three body systems (search terms are vocabulary only),
splits body paragraphs from methods/results-style sections with app.research.segment, and writes
results/x7.json and results/x7_review.txt (one numbered piece per line) for the hand check.
Errors found by hand are recorded in X7_ERRORS below as (pmcid, piece number, kind), so the rate is reproducible.
Pass: >= 97% of pieces are correct sentences.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

from app.literature import jats
from app.literature.sources import europepmc
from app.literature.sources.ncbi import eutil
from app.research.segment import split_paper

OUT = Path(__file__).resolve().parent / "results"
QUERIES = {
    "ent": 'TITLE:"tinnitus" AND OPEN_ACCESS:y AND HAS_FT:y AND PUB_TYPE:"research-article"',
    "neurology": 'TITLE:"migraine" AND OPEN_ACCESS:y AND HAS_FT:y AND PUB_TYPE:"research-article"',
    "skin": 'TITLE:"atopic dermatitis" AND OPEN_ACCESS:y AND HAS_FT:y AND PUB_TYPE:"research-article"',
}
PAPERS_PER_SYSTEM = 2
SENTENCES_PER_PAPER = 40
SECTION_HINT = re.compile(r"method|result|statistic|analys|participant|discussion", re.I)

# Hand-check results of the seg.v1 run on 2026-11-24 (240 pieces): (pmcid, piece number, kind). Both pieces of
# one wrongly split sentence ("IBM SPSS Statistics ver." | "30 (IBM SPSS, …)") count. Fixed in seg.v2.
X7_ERRORS: list[tuple[str, int, str]] = [("PMC13657970", 25, "split"), ("PMC13657970", 26, "split")]


async def _paper_text(client: httpx.AsyncClient, pmcid: str) -> list[str]:
    response = await eutil(client, "efetch.fcgi", {"db": "pmc", "id": pmcid.removeprefix("PMC"), "retmode": "xml"})
    root = jats.article_element(response.content)
    if root is None:
        return []
    sections, _, _ = jats.parse(root)
    paras = []
    for s in sections:
        if s.kind == "body" and SECTION_HINT.search(s.title or ""):
            paras += [p for p in s.text.split("\n\n") if len(p) > 200]
    return paras


async def main() -> None:
    rows = []
    async with httpx.AsyncClient(timeout=90, headers={"User-Agent": "helix-spike"}) as client:
        for system, query in QUERIES.items():
            _, records = await europepmc.search_records(client, europepmc.scoped(query), 10)
            taken = 0
            for rec in records:
                if taken >= PAPERS_PER_SYSTEM or not rec.pmcid:
                    continue
                paras = await _paper_text(client, rec.pmcid)
                pieces: list[str] = []
                for para in paras:
                    pieces += split_paper(para)
                    if len(pieces) >= SENTENCES_PER_PAPER:
                        break
                if len(pieces) < 20:
                    continue
                taken += 1
                rows.append({"system": system, "pmcid": rec.pmcid, "pieces": pieces[:SENTENCES_PER_PAPER]})
    OUT.mkdir(parents=True, exist_ok=True)
    total = sum(len(r["pieces"]) for r in rows)
    errors = len(X7_ERRORS)
    result = {"papers": [{k: r[k] for k in ("system", "pmcid")} | {"pieces": len(r["pieces"])} for r in rows],
              "pieces": total, "errors": errors, "rate": round(1 - errors / max(1, total), 4),
              "pass": total >= 200 and 1 - errors / max(1, total) >= 0.97, "error_list": X7_ERRORS}
    (OUT / "x7.json").write_text(json.dumps({**result, "rows": rows}, indent=2, ensure_ascii=False), "utf-8")
    with (OUT / "x7_review.txt").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(f"## {r['system']} {r['pmcid']}\n")
            for i, piece in enumerate(r["pieces"], 1):
                fh.write(f"{i:>3}  {piece}\n")
    print(json.dumps({k: v for k, v in result.items() if k != "error_list"}))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    asyncio.run(main())
