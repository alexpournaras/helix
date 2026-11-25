"""brief_update (PLAN B8.5): after answers (or a follow-up), the model rewrites the brief from every segment; the
intake code checks run again (segment IDs, brief check, unknowns, MeSH, systems, populations); then `safety`.

- A fact of the previous brief may disappear only when `removed_features` names it with a contradicting segment
  (checked; one repair call). A fact counts as kept when its path or its wording survives.
- An unknown answered by a question with the same key is dropped even if the model keeps it.
- New complaints and populations reach grounding through the safety rule (B4 step 3).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .. import brief as B, ensemble, llm

from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import BriefUpdate, CaseBrief
from ..segment import UserSegment
from . import intake, safety

GENERATE_VERSION = "brief_update.generate.v1"


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def load_segments(conn: sqlite3.Connection, investigation_id: int) -> list[UserSegment]:
    return [UserSegment(r["seg_id"], r["origin"], r["text"], r.get("ref_id"), r.get("text_en"))
            for r in safety.segments(conn, investigation_id)]


def removal_validator(previous: CaseBrief, valid: set[str], need_translation: list[str]):
    prev = list(B.iter_facts(previous.model_dump(mode="json")))

    def check(update: BriefUpdate) -> None:
        got = {t.seg_id for t in update.translations}
        missing_tr = [s for s in need_translation if s not in got]
        if missing_tr:
            raise ValueError(f"translations missing for {', '.join(missing_tr[:10])}")
        new = dict(B.iter_facts(update.brief.model_dump(mode="json")))
        texts = {_norm(f["text"]) for f in new.values()}
        removed = {r.key: r for r in update.removed_features}
        for path, fact in prev:
            if path in new or _norm(fact["text"]) in texts:
                continue
            r = removed.get(path)
            if r is None or not r.seg_ids or not set(r.seg_ids) <= valid:
                raise ValueError(f"{path} was dropped without a contradicting segment in removed_features")
        if not update.complaints:
            raise ValueError("the update needs at least one complaint")
    return check


def update_material(previous: CaseBrief, segments: list[UserSegment], new_ids: list[str]) -> str:
    facts = [{"path": p, **f} for p, f in B.iter_facts(previous.model_dump(mode="json"))]
    body = {"previous_brief": previous.model_dump(mode="json", exclude_none=True), "fact_paths": facts,
            "segments": [{"seg_id": s.seg_id, "text": s.text, **({"english": s.text_en} if s.text_en else {})}
                         for s in segments],
            "new_segments": new_ids}
    return llm.material(json.dumps(body, ensure_ascii=False, indent=1), kind="brief_update")


async def run(tc) -> Any:
    inv = tc.investigation
    with tc.db.read() as conn:
        found = safety.current_brief(conn, inv["id"])
        segments = load_segments(conn, inv["id"])
        answered = {r["key"] for r in conn.execute(
            "SELECT key FROM clarifying_questions WHERE investigation_id = ? AND status = 'answered'", (inv["id"],))}
    if found is None:
        raise RuntimeError("brief_update needs a brief")
    prev_version, previous = found
    cited = {s for _, f in B.iter_facts(previous.model_dump(mode="json")) for s in f["seg_ids"]}
    new_ids = [s.seg_id for s in segments if s.origin in ("answer", "followup") and s.seg_id not in cited]
    lang = (inv.get("problem_lang") or "en").lower()
    need_tr = [s.seg_id for s in segments if s.seg_id in new_ids and lang != "en" and not s.text_en]
    valid = {s.seg_id for s in segments}
    ctx = tc.services.with_task(task_id=tc.task["id"], step="brief_update", investigation_id=inv["id"])
    member = await ensemble.single(
        ctx, "brief_update", BriefUpdate, llm.base_system() + "\n\n" + llm.prompt(GENERATE_VERSION),
        update_material(previous, segments, new_ids), prompt_version=GENERATE_VERSION,
        validate=removal_validator(previous, valid, need_tr))
    update: BriefUpdate = member.value  # type: ignore[assignment]
    english = {s.seg_id: s.text_en for s in segments if s.text_en}
    english.update({t.seg_id: t.text_en for t in update.translations})
    for s in segments:
        s.text_en = english.get(s.seg_id, s.text_en)
    out = await intake.checked(ctx, segments, english, update.brief, update.complaints, update.proposed_systems,
                               result=update, model_label=member.model.label())
    data = out.brief.model_dump(mode="json")
    data["unknowns"] = [u for u in data["unknowns"] if u["key"] not in answered]
    brief = CaseBrief.model_validate(data)
    removed = [r.model_dump(mode="json") for r in update.removed_features]

    def commit(conn: sqlite3.Connection) -> None:
        for s in segments:
            if s.seg_id in english and s.origin != "problem":
                conn.execute("UPDATE user_segments SET text_en = ? WHERE investigation_id = ? AND seg_id = ?",
                             (english[s.seg_id], inv["id"], s.seg_id))
        version = intake.save_brief(tc.db, conn, inv["id"], brief, source=f"brief_update:v{prev_version + 1}",
                                    check={**(out.check or {}), "removed_features": removed},
                                    prompt_version=f"{GENERATE_VERSION}+{intake.CHECK_VERSION}")
        tc.db.update_investigation(inv["id"], conn=conn, complaints=out.complaints, systems=out.systems,
                                   special_populations=out.populations)
        tc.db.event(inv["id"], "brief_update", f"brief v{version}", stage="brief_update", conn=conn, data={
            **(out.stats or {}), "brief_version": version, "new_segments": len(new_ids),
            "removed_features": len(removed), "check": (out.check or {}).get("counts")})
    return commit


def stage() -> StageDef:
    def on_enter(sc: StageContext) -> None:
        last = sc.conn.execute("SELECT id FROM events WHERE investigation_id = ? AND kind = 'questions_closed' "
                               "ORDER BY id DESC LIMIT 1", (sc.id,)).fetchone()
        sc.queue("brief_update", f"brief_update:e{last['id'] if last else 0}", priority=30)

    def on_complete(sc: StageContext):
        return "safety"
    return StageDef("brief_update", on_enter=on_enter, on_complete=on_complete)


STEP = StepDef("brief_update", run)
