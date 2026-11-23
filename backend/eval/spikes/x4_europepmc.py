"""Spike X4 (PLAN 1A.0): Europe PMC field allowlist, core fields, correction types, guideline queries.

Run from backend/:  .venv/Scripts/python -m eval.spikes.x4_europepmc
Live, public API, no key. Only medical vocabulary is sent (no case text). Writes eval/spikes/results/x4.json.
Pass: allowlist confirmed; >= 1 guideline or systematic review found for >= 8 of 10 complaints.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import httpx

BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
OUT = Path(__file__).resolve().parent / "results" / "x4.json"
TERM = "tinnitus"

# Field → expected to work (True) or to be unknown (False → Europe PMC answers 0 hits with HTTP 200, C7)
FIELDS = {
    f'TITLE:"{TERM}"': True,
    f'ABSTRACT:"{TERM}"': True,
    f'TITLE_ABS:"{TERM}"': True,
    f'KW:"{TERM}"': True,
    f'MESH:"{TERM}"': True,
    f'TITLE_ABS:"{TERM}" AND PUB_TYPE:"systematic review"': True,
    f'TITLE_ABS:"{TERM}" AND PUB_TYPE:"practice guideline"': True,
    f'TITLE_ABS:"{TERM}" AND SRC:MED': True,
    f'TITLE_ABS:"{TERM}" AND LANG:eng': True,
    f'TITLE_ABS:"{TERM}" AND OPEN_ACCESS:y': True,
    f'TITLE_ABS:"{TERM}" AND HAS_FT:y': True,
    f'TITLE_ABS:"{TERM}" AND PUB_YEAR:[2016 TO 2026]': True,
    f'MESH_HEADING:"{TERM}"': False,
    f'NOT_A_FIELD:"{TERM}"': False,
}

# 10 complaints from different body systems: (complaint, MeSH descriptor name)
COMPLAINTS = [
    ("tinnitus", "Tinnitus"), ("headache", "Headache"), ("chest pain", "Chest Pain"),
    ("abdominal pain", "Abdominal Pain"), ("low back pain", "Low Back Pain"), ("cough", "Cough"),
    ("urticaria", "Urticaria"), ("dysuria", "Dysuria"), ("fatigue", "Fatigue"), ("palpitations", "Palpitations"),
]


async def get(client: httpx.AsyncClient, params: dict) -> tuple[dict, float]:
    for attempt in range(3):
        started = time.monotonic()
        try:
            r = await client.get(BASE, params={**params, "format": "json"})
            if r.status_code == 429 or r.status_code >= 500:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json(), time.monotonic() - started
        except httpx.TimeoutException:
            await asyncio.sleep(2)
    raise RuntimeError(f"Europe PMC failed for {params.get('query')}")


async def count(client, query: str) -> tuple[int, float]:
    data, latency = await get(client, {"query": query, "resultType": "idlist", "pageSize": 1})
    return int(data.get("hitCount") or 0), latency


def guideline_queries(term: str, descriptor: str) -> dict[str, str]:
    scope = f'(TITLE_ABS:"{term}" OR MESH:"{descriptor}")'
    years = "PUB_YEAR:[2016 TO 2026]"
    return {
        "guideline": f'{scope} AND (PUB_TYPE:"practice guideline" OR PUB_TYPE:"guideline") AND {years}',
        "systematic_review": f'{scope} AND PUB_TYPE:"systematic review" AND {years}',
        "sr_diagnosis": f'{scope} AND PUB_TYPE:"systematic review" AND (TITLE_ABS:"diagnosis" OR '
                        f'TITLE_ABS:"evaluation") AND {years}',
    }


async def main() -> None:
    results: dict = {"fields": [], "core": {}, "complaints": [], "latencies": []}
    async with httpx.AsyncClient(timeout=90, headers={"User-Agent": "helix-spike"}) as client:
        # 1. allowlist
        for query, expected in FIELDS.items():
            hits, latency = await count(client, query)
            results["latencies"].append(latency)
            works = hits > 0
            results["fields"].append({"query": query, "hits": hits, "expected_to_work": expected,
                                      "as_expected": works == expected})

        # 2. core results: field presence over 40 records, plus correction types over a larger sample
        data, latency = await get(client, {"query": f'TITLE_ABS:"{TERM}" AND (SRC:MED OR SRC:PMC)',
                                           "resultType": "core", "pageSize": 40})
        results["latencies"].append(latency)
        records = (data.get("resultList") or {}).get("result") or []
        presence = Counter()
        for rec in records:
            for key in ("abstractText", "pubTypeList", "meshHeadingList", "isOpenAccess", "fullTextIdList",
                        "pmcid", "doi", "citedByCount", "commentCorrectionList", "language", "inEPMC", "hasReferences"):
                if rec.get(key) not in (None, "", [], {}):
                    presence[key] += 1
        results["core"] = {
            "records": len(records),
            "with_abstract": presence["abstractText"],
            "open_access": sum(1 for r in records if r.get("isOpenAccess") == "Y"),
            "field_presence": dict(presence),
        }
        corrections = Counter()
        pub_types = Counter()
        for query in ('PUB_TYPE:"retracted publication" AND SRC:MED', 'TITLE_ABS:"expression of concern" AND SRC:MED'):
            data, latency = await get(client, {"query": query, "resultType": "core", "pageSize": 25})
            results["latencies"].append(latency)
            for rec in (data.get("resultList") or {}).get("result") or []:
                for c in ((rec.get("commentCorrectionList") or {}).get("commentCorrection") or []):
                    corrections[c.get("type", "?")] += 1
                for p in ((rec.get("pubTypeList") or {}).get("pubType") or []):
                    pub_types[p] += 1
        results["core"]["correction_types"] = dict(corrections.most_common())
        results["core"]["pub_types_in_retraction_sample"] = dict(pub_types.most_common(10))

        # 3. guideline / systematic review queries per complaint
        for term, descriptor in COMPLAINTS:
            row = {"complaint": term, "descriptor": descriptor}
            for kind, query in guideline_queries(term, descriptor).items():
                hits, latency = await count(client, query)
                results["latencies"].append(latency)
                row[kind] = hits
            row["found"] = row["guideline"] > 0 or row["systematic_review"] > 0
            results["complaints"].append(row)

    lat = sorted(results["latencies"])
    results["latency_summary"] = {"min": round(lat[0], 2), "median": round(lat[len(lat) // 2], 2), "max": round(lat[-1], 2)}
    found = sum(1 for r in results["complaints"] if r["found"])
    results["pass"] = {
        "allowlist_as_expected": all(f["as_expected"] for f in results["fields"]),
        "complaints_with_guideline_or_sr": f"{found}/{len(COMPLAINTS)}",
        "guidelines_pass": found >= 8,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(results, indent=2), "utf-8")
    print(json.dumps({"pass": results["pass"], "core": results["core"], "latency": results["latency_summary"]}, indent=2))
    for f in results["fields"]:
        print(f"  {'ok ' if f['as_expected'] else 'BAD'} {f['hits']:>7}  {f['query']}")
    for r in results["complaints"]:
        print(f"  {r['complaint']:<15} guideline={r['guideline']:<5} sr={r['systematic_review']:<5} sr_dx={r['sr_diagnosis']}")


if __name__ == "__main__":
    asyncio.run(main())
