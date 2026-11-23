"""Spike X8 (PLAN 1A.0): 50 complaints across all systems → MeSH descriptors → Helix body systems.

Run from backend/:  .venv/Scripts/python -m eval.spikes.x8_mesh
Live E-utilities (db=mesh). Only medical vocabulary is sent. Writes results/x8.json.
The expected system for each complaint is the hand check (reviewer: please confirm the EXPECTED column).
Pass: >= 95% of complaints have their expected system among the mapped systems.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import yaml

from app.literature.sources.ncbi import EUTILS, TOOL, _key, _throttle

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "results" / "x8.json"
MAPPING = ROOT.parent.parent / "app" / "research" / "knowledge" / "mesh_systems.yaml"

# complaint → expected Helix system, or a tuple of systems that are each clinically correct (hand-labelled
# before the mapping table was written; ambiguous symptoms accept any of their systems)
EXPECTED = {
    "tinnitus": "ent", "hearing loss": "ent", "vertigo": "ent", "sore throat": "ent", "epistaxis": "ent",
    "headache": "neurology", "seizures": "neurology", "paresthesia": "neurology", "tremor": "neurology",
    "memory loss": ("neurology", "mental_health"), "syncope": ("neurology", "cardiovascular"),
    "chest pain": "cardiovascular", "palpitations": "cardiovascular", "edema": ("cardiovascular", "general"),
    "hypertension": "cardiovascular",
    "cough": "respiratory", "dyspnea": "respiratory", "hemoptysis": "respiratory", "wheezing": "respiratory",
    "abdominal pain": "digestive", "nausea": "digestive", "diarrhea": "digestive", "constipation": "digestive",
    "jaundice": "digestive", "dysphagia": "digestive", "heartburn": "digestive",
    "low back pain": "musculoskeletal", "arthralgia": "musculoskeletal", "neck pain": "musculoskeletal",
    "muscle cramp": "musculoskeletal",
    "pruritus": "skin", "urticaria": "skin", "exanthema": "skin", "alopecia": "skin",
    "dysuria": "urogenital", "hematuria": "urogenital", "urinary incontinence": "urogenital",
    "erectile dysfunction": "urogenital",
    "dysmenorrhea": "female_reproductive", "vaginal discharge": "female_reproductive",
    "eye pain": "eye", "diplopia": "eye", "blurred vision": "eye",
    "fatigue": "general", "fever": "general", "weight loss": "general",
    "depression": "mental_health", "anxiety": "mental_health", "insomnia": ("mental_health", "neurology"),
    "polyuria": ("endocrine_metabolic", "urogenital"),
}


async def eutil(client: httpx.AsyncClient, endpoint: str, params: dict) -> httpx.Response:
    query = {**params, "tool": TOOL}
    if _key():
        query["api_key"] = _key()
    for attempt in range(4):
        await _throttle.wait(0.11 if _key() else 0.4)
        r = await client.get(f"{EUTILS}/{endpoint}", params=query)
        if r.status_code == 429 or r.status_code >= 500:
            await asyncio.sleep(2 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(endpoint)


async def lookup(client: httpx.AsyncClient, term: str) -> dict | None:
    """Descriptor for a term: exact heading first, then a free search (entry-term fallback)."""
    for query in (f'"{term}"[MeSH Terms]', term):
        r = await eutil(client, "esearch.fcgi", {"db": "mesh", "term": query, "retmode": "json", "retmax": 5})
        uids = r.json()["esearchresult"].get("idlist", [])
        if not uids:
            continue
        s = await eutil(client, "esummary.fcgi", {"db": "mesh", "id": ",".join(uids), "retmode": "json"})
        data = s.json()["result"]
        records = [data[u] for u in data.get("uids", [])]
        # descriptors only (UI starts with D), prefer one whose terms include the query
        descriptors = [r for r in records if str(r.get("ds_meshui", "")).startswith("D")]
        if not descriptors:
            continue
        term_l = term.casefold()
        named = [d for d in descriptors if any(t.casefold() == term_l for t in d.get("ds_meshterms", []))]
        if not named and not query.endswith("[MeSH Terms]"):
            continue  # a free-search hit must list the term among its entry terms, else it stays unverified
        best = (named or descriptors)[0]
        trees = [link.get("treenum") for link in best.get("ds_idxlinks", []) if link.get("treenum")]
        return {"ui": best.get("ds_meshui"), "name": (best.get("ds_meshterms") or [None])[0],
                "entry_terms": best.get("ds_meshterms", [])[:8], "tree_numbers": trees,
                "matched_by": "heading" if query.endswith("[MeSH Terms]") else "search"}
    return None


def map_systems(trees: list[str], table: dict) -> list[str]:
    systems: list[str] = []
    prefixes = sorted(table["prefixes"].items(), key=lambda kv: -len(kv[0]))
    for tree in trees:
        for prefix, system in prefixes:
            if tree == prefix or tree.startswith(prefix + "."):
                if system and system not in systems:
                    systems.append(system)
                break
    return systems


async def main() -> None:
    table = yaml.safe_load(MAPPING.read_text("utf-8")) if MAPPING.exists() else {"prefixes": {}}
    rows = []
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "helix-spike"}) as client:
        for complaint, expected in EXPECTED.items():
            hit = await lookup(client, complaint)
            systems = map_systems(hit["tree_numbers"], table) if hit else []
            accepted = (expected,) if isinstance(expected, str) else tuple(expected)
            rows.append({"complaint": complaint, "expected": list(accepted), "descriptor": hit, "systems": systems,
                         "correct": any(e in systems for e in accepted)})
    correct = sum(r["correct"] for r in rows)
    result = {"rows": rows, "correct": correct, "total": len(rows), "rate": round(correct / len(rows), 3),
              "pass": correct / len(rows) >= 0.95, "unverified": [r["complaint"] for r in rows if not r["descriptor"]]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), "utf-8")
    for r in rows:
        d = r["descriptor"] or {}
        mark = "ok " if r["correct"] else "BAD"
        print(f"{mark} {r['complaint']:<22} {str(d.get('ui')):<9} {str(d.get('name'))[:26]:<26} "
              f"exp={'|'.join(r['expected']):<20} got={','.join(r['systems'])}  trees={','.join(d.get('tree_numbers', []))}")
    print(f"\n{correct}/{len(rows)} = {result['rate']:.0%}  pass={result['pass']}  unverified={result['unverified']}")


if __name__ == "__main__":
    asyncio.run(main())
