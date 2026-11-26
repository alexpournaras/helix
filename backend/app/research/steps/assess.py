"""matrix (PLAN B8.19, code); fit and analysis (B8.20–B8.21) follow in 1E.5.

matrix:r{round}  rates every active hypothesis from the active claims' effective links (`matrix.rate`): direction,
                 certainty, the units and downgrades behind them (`hypotheses.ratings`). A hypothesis whose rating
                 changed gets `evidence_version + 1`, so fit and analysis rerun only for it. An `evidence_matrix` note.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import matrix as M
from ..db import _decode, now
from ..pipeline import StageContext, StageDef, StepDef


async def run_matrix(tc) -> Any:
    inv = tc.investigation
    rnd = int(inv["round"])

    def commit(conn: sqlite3.Connection) -> None:
        data = M.load(conn, inv["id"], tc.settings)
        counts: dict[str, int] = {"changed": 0}
        for h in [_decode(r) for r in conn.execute("SELECT * FROM hypotheses WHERE investigation_id = ? AND "
                                                   "status = 'active' ORDER BY id", (inv["id"],))]:
            rating = M.rate(data.items.get(h["id"], []), data, tc.settings)
            counts[rating["direction"]] = counts.get(rating["direction"], 0) + 1
            if (h.get("ratings") or None) != rating:
                counts["changed"] += 1
                conn.execute("UPDATE hypotheses SET direction = ?, certainty = ?, ratings = ?, "
                             "evidence_version = evidence_version + 1 WHERE id = ?",
                             (rating["direction"], rating["certainty"], json.dumps(rating), h["id"]))
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "evidence_matrix",
                                           "artifact_key": f"matrix:r{rnd}", "created_at": now()}, or_ignore=True)
        tc.db.event(inv["id"], "matrix", f"r{rnd}", stage="matrix", conn=conn, data={"round": rnd, **counts})
    return commit


def matrix_stage(next_stage: str = "fitting") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("matrix", f"matrix:r{int(sc.inv['round'])}", {}, priority=95)
    return StageDef("matrix", on_enter=on_enter, on_complete=lambda sc: next_stage)


MATRIX_STEP = StepDef("matrix", run_matrix, code_step=True)
