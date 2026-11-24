"""Record the MeSH E-utilities responses used by test_research_mesh.py (PLAN 1A.9).

Run from backend/:  .venv/Scripts/python -m eval.record_mesh
Live, but only MeSH vocabulary terms are sent (no case text). Recordings hold response bodies only, keyed
without the API key, so they're safe to commit. Writes test_fixtures/mesh/.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from app.research import mesh, transport
from app.research.db import ResearchDB

FIXTURES = Path(__file__).resolve().parent.parent / "test_fixtures" / "mesh"

# 20 terms: heading matches across systems, an entry term, a descriptor UI, and terms that must stay unverified.
TERMS = [
    "tinnitus", "headache", "cephalalgia", "chest pain", "low back pain", "cough", "dyspnea", "abdominal pain",
    "urticaria", "dysuria", "fatigue", "fever", "depression", "vertigo", "hearing loss", "pruritus", "D014012",
    "palpitations", "blurred vision", "zzqx flarbitis",
]


async def main() -> None:
    if FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    tmp = Path(tempfile.mkdtemp(prefix="helix-mesh-record-"))
    try:
        db = ResearchDB(tmp / "research.sqlite3")
        db.init()
        with transport.use_transport(transport.RecordTransport(FIXTURES)):
            for term in TERMS:
                d = await mesh.lookup(term, db)
                print(f"{term:<18} -> {d.ui + ' ' + d.name if d else 'unverified':<34} {','.join(d.systems) if d else ''}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{len(list(FIXTURES.glob('*.json')))} recordings in {FIXTURES}")


if __name__ == "__main__":
    asyncio.run(main())
