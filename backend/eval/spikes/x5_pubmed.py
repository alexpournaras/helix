"""Spike X5 (PLAN 1A.0): PubMed efetch with 200 IDs; retraction and expression-of-concern fields.

Run from backend/:  .venv/Scripts/python -m eval.spikes.x5_pubmed
Live, public E-utilities (NCBI_API_KEY optional). Only medical vocabulary is sent. Writes results/x5.json.
Pass: a 200-ID efetch parses (else fall back to 100 per call).
"""

from __future__ import annotations

import asyncio
import json
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import httpx

from app.literature.sources import pubmed
from app.literature.sources.ncbi import EUTILS, TOOL, _key, _throttle

OUT = Path(__file__).resolve().parent / "results" / "x5.json"


async def eutil(client: httpx.AsyncClient, endpoint: str, data: dict, *, post: bool = False) -> httpx.Response:
    params = {**data, "tool": TOOL}
    if _key():
        params["api_key"] = _key()
    for attempt in range(3):
        await _throttle.wait(0.11 if _key() else 0.35)
        r = await (client.post(f"{EUTILS}/{endpoint}", data=params) if post else
                   client.get(f"{EUTILS}/{endpoint}", params=params))
        if r.status_code == 429 or r.status_code >= 500:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(endpoint)


async def esearch(client, term: str, retmax: int) -> tuple[int, list[str]]:
    r = await eutil(client, "esearch.fcgi", {"db": "pubmed", "term": term, "retmax": retmax, "retmode": "json"})
    data = r.json()["esearchresult"]
    return int(data["count"]), data.get("idlist", [])


async def main() -> None:
    results: dict = {}
    async with httpx.AsyncClient(timeout=120, headers={"User-Agent": "helix-spike"}) as client:
        # Hit counts with retmax=0 (C10)
        count, ids = await esearch(client, "tinnitus[mh] AND systematic review[pt]", 0)
        results["retmax0"] = {"count": count, "ids_returned": len(ids)}

        _, normal = await esearch(client, "tinnitus[mh] AND 2020:2025[dp]", 150)
        _, retracted = await esearch(client, "retracted publication[pt]", 30)
        _, concern = await esearch(client, "hasexpressionofconcernin", 20)
        ids = list(dict.fromkeys(normal + retracted + concern))[:200]

        for label, batch in (("efetch_200_post", ids),):
            started = time.monotonic()
            r = await eutil(client, "efetch.fcgi", {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"}, post=True)
            elapsed = time.monotonic() - started
            root = ET.fromstring(r.content)
            articles = root.findall("PubmedArticle")
            parsed, errors = 0, []
            pub_types = Counter()
            corrections = Counter()
            flagged = {"retracted_pt": 0, "retraction_in": 0, "concern_in": 0}
            for node in articles:
                try:
                    art = pubmed.parse(node)
                    parsed += 1
                    pub_types.update(art.publication_types)
                    if "Retracted Publication" in art.publication_types:
                        flagged["retracted_pt"] += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{exc.__class__.__name__}: {exc}")
                for cc in node.findall("MedlineCitation/CommentsCorrectionsList/CommentsCorrections"):
                    ref = cc.get("RefType", "?")
                    corrections[ref] += 1
                kinds = {cc.get("RefType") for cc in node.findall(
                    "MedlineCitation/CommentsCorrectionsList/CommentsCorrections")}
                flagged["retraction_in"] += "RetractionIn" in kinds
                flagged["concern_in"] += "ExpressionOfConcernIn" in kinds
            results[label] = {
                "requested": len(batch), "returned": len(articles), "parsed": parsed, "parse_errors": errors[:5],
                "seconds": round(elapsed, 2), "bytes": len(r.content), "flags": flagged,
                "correction_ref_types": dict(corrections.most_common()),
                "retraction_pub_types": {k: v for k, v in pub_types.items() if "etract" in k or "oncern" in k},
            }
        results["concern_query_ids"] = len(concern)
    ok = results["efetch_200_post"]["parsed"] == results["efetch_200_post"]["requested"]
    results["pass"] = {"efetch_200_parses": ok,
                       "retraction_fields_seen": results["efetch_200_post"]["flags"]["retraction_in"] > 0,
                       "concern_fields_seen": results["efetch_200_post"]["flags"]["concern_in"] > 0}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), "utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
