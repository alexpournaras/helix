"""Spike X8b (PLAN 1A.9): held-out MeSH mapping check, none of these terms were used to write mesh_systems.yaml.

Run from backend/:  .venv/Scripts/python -m eval.spikes.x8b_mesh_heldout
Live E-utilities through app.research.mesh (the real lookup rules). Only medical vocabulary is sent.
EXPECTED was labelled by an agent before the run and needs a human check (PLAN Part G.6): a tuple lists systems
that are each clinically acceptable. Pass: >= 95% of verified complaints have an expected system.
Writes results/x8b.json.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from app.research import mesh
from app.research.db import ResearchDB

OUT = Path(__file__).resolve().parent / "results" / "x8b.json"

EXPECTED: dict[str, str | tuple[str, ...]] = {
    # ENT
    "otitis media": "ent", "sinusitis": ("ent", "respiratory"), "hoarseness": ("ent", "respiratory"),
    "anosmia": ("ent", "neurology"), "earache": "ent",
    # neurology
    "migraine disorders": "neurology", "facial paralysis": "neurology", "dizziness": ("neurology", "ent"),
    "ataxia": "neurology", "dementia": ("neurology", "mental_health"), "restless legs syndrome": "neurology",
    # cardiovascular
    "bradycardia": "cardiovascular", "varicose veins": "cardiovascular", "intermittent claudication": "cardiovascular",
    "hypotension": "cardiovascular",
    # respiratory
    "asthma": "respiratory", "snoring": ("respiratory", "ent"), "pneumonia": ("respiratory", "infection"),
    # digestive
    "vomiting": "digestive", "hematemesis": "digestive", "flatulence": "digestive", "fecal incontinence": "digestive",
    "melena": "digestive",
    # mouth and teeth
    "toothache": "mouth_dental", "xerostomia": "mouth_dental", "oral ulcer": "mouth_dental",
    # musculoskeletal
    "shoulder pain": "musculoskeletal", "gout": ("musculoskeletal", "endocrine_metabolic"),
    "fibromyalgia": ("musculoskeletal", "neurology"),
    # skin
    "acne vulgaris": "skin", "psoriasis": "skin", "hyperhidrosis": "skin", "eczema": "skin",
    # urogenital
    "nocturia": "urogenital", "renal colic": "urogenital", "kidney calculi": "urogenital",
    # female reproductive
    "amenorrhea": "female_reproductive", "menorrhagia": "female_reproductive",
    "dyspareunia": ("female_reproductive", "urogenital"),
    # pregnancy
    "morning sickness": "pregnancy", "pre-eclampsia": "pregnancy", "postpartum hemorrhage": "pregnancy",
    "hyperemesis gravidarum": "pregnancy",
    # eye
    "photophobia": "eye", "conjunctivitis": "eye", "dry eye syndromes": "eye",
    # endocrine and metabolic
    "polydipsia": ("endocrine_metabolic", "general"), "hypoglycemia": "endocrine_metabolic",
    "goiter": "endocrine_metabolic", "obesity": ("endocrine_metabolic", "general"),
    # blood
    "anemia": "hematology", "purpura": ("hematology", "skin"),
    "lymphadenopathy": ("hematology", "immunology_allergy"),
    # immune and allergy
    "anaphylaxis": "immunology_allergy", "food hypersensitivity": "immunology_allergy",
    "angioedema": ("immunology_allergy", "skin", "cardiovascular"),
    # infection
    "lyme disease": "infection", "herpes zoster": ("infection", "skin", "neurology"),
    "influenza, human": ("infection", "respiratory"),
    # cancer
    "breast neoplasms": "oncology",
    # mental health
    "panic disorder": "mental_health", "anorexia nervosa": "mental_health",
    "hallucinations": ("mental_health", "neurology"), "suicidal ideation": "mental_health",
    # children and newborns
    "colic": ("digestive", "general"), "neonatal jaundice": ("congenital_neonatal", "digestive"),
    "failure to thrive": ("general", "congenital_neonatal"), "enuresis": ("urogenital", "mental_health"),
    "febrile seizures": ("neurology", "general"), "croup": ("respiratory", "infection"),
    # injury and poisoning
    "carbon monoxide poisoning": "injury_poisoning", "snake bites": "injury_poisoning", "burns": "injury_poisoning",
    "drug overdose": "injury_poisoning", "sunburn": ("injury_poisoning", "skin"),
    "brain concussion": ("injury_poisoning", "neurology"),
    # rare conditions
    "myasthenia gravis": ("neurology", "immunology_allergy", "musculoskeletal"),
    "sarcoidosis": ("immunology_allergy", "respiratory", "hematology"),
    "porphyrias": ("endocrine_metabolic", "skin", "hematology"), "amyloidosis": "endocrine_metabolic",
    "ehlers-danlos syndrome": ("musculoskeletal", "skin", "congenital_neonatal"),
    # general
    "chills": "general", "malaise": "general",
}


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="helix-x8b-"))
    rows = []
    try:
        db = ResearchDB(tmp / "research.sqlite3")
        db.init()
        for complaint, expected in EXPECTED.items():
            d = await mesh.lookup(complaint, db)
            accepted = (expected,) if isinstance(expected, str) else tuple(expected)
            systems = d.systems if d else []
            rows.append({"complaint": complaint, "expected": list(accepted),
                         "descriptor": {"ui": d.ui, "name": d.name, "tree_numbers": d.tree_numbers} if d else None,
                         "systems": systems, "correct": any(e in systems for e in accepted)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    verified = [r for r in rows if r["descriptor"]]
    correct = sum(r["correct"] for r in verified)
    result = {"rows": rows, "total": len(rows), "verified": len(verified), "correct_of_verified": correct,
              "rate_of_verified": round(correct / max(1, len(verified)), 3),
              "rate_overall": round(correct / len(rows), 3),
              "pass": correct / max(1, len(verified)) >= 0.95,
              "unverified": [r["complaint"] for r in rows if not r["descriptor"]],
              "wrong": [r["complaint"] for r in verified if not r["correct"]]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2), "utf-8")
    for r in rows:
        d = r["descriptor"] or {}
        mark = "ok " if r["correct"] else ("-- " if not d else "BAD")
        print(f"{mark} {r['complaint']:<26} {str(d.get('ui')):<9} {str(d.get('name'))[:28]:<28} "
              f"exp={'|'.join(r['expected']):<44} got={','.join(r['systems'])}  trees={','.join(d.get('tree_numbers', []))}")
    print(f"\nverified {len(verified)}/{len(rows)}; correct {correct}/{len(verified)} = {result['rate_of_verified']:.0%} "
          f"(overall {result['rate_overall']:.0%}) pass={result['pass']}")
    print(f"unverified={result['unverified']}\nwrong={result['wrong']}")


if __name__ == "__main__":
    asyncio.run(main())
