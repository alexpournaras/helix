"""intake (PLAN B8.1): generate (family A) → code checks → brief check (family B) → MeSH → saves.

Not accepted (not health, not human, prescription or dose only) → wait `problem_rewrite` with a fixed message
(no model text is shown). A rewrite bumps `problem_version`, replaces the problem segments and re-runs intake.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .. import brief as B, ensemble, llm, mesh
from ..db import now
from ..pipeline import STAY, StageContext, StageDef, StepDef, Wait
from ..schemas import BriefCheck, CaseBrief, Complaint, IntakeResult
from ..segment import UserSegment, user_segments

GENERATE_VERSION = "intake.generate.v1"
CHECK_VERSION = "intake.check.v1"

REWRITE_MESSAGES = {
    "not_health": "Helix investigates human health problems. Please describe the health problem you want "
                  "researched: what you notice, since when, and anything that makes it better or worse.",
    "not_human": "Helix only investigates health problems in people. For an animal, please ask a vet.",
    "prescription_or_dose": "Helix can't choose a medicine or a dose. Please describe the health problem "
                            "itself, and Helix will research the evidence about it.",
}


def segment_lines(segments: list[UserSegment], english: dict[str, str] | None = None) -> str:
    lines = []
    for s in segments:
        lines.append(f"{s.seg_id}: {s.text}")
        if english and english.get(s.seg_id) and english[s.seg_id] != s.text:
            lines.append(f"{s.seg_id} (English): {english[s.seg_id]}")
    return "\n".join(lines)


def accepted(result: IntakeResult) -> bool:
    return result.is_health_problem and result.is_human and result.not_accepted_reason is None


def validator(seg_ids: list[str]):
    """Structural checks that warrant one repair call. Invalid segment IDs are handled by code instead."""
    known = set(seg_ids)

    def check(result: IntakeResult) -> None:
        if not accepted(result):
            if result.not_accepted_reason is None:
                raise ValueError("not_accepted_reason is required when the text isn't accepted")
            return
        if result.language.lower() != "en":
            got = {t.seg_id for t in result.translations}
            missing = [s for s in seg_ids if s not in got]
            if missing:
                raise ValueError(f"translations missing for {', '.join(missing[:10])}")
        unknown = sorted({t.seg_id for t in result.translations} - known)
        if unknown:
            raise ValueError(f"translations for unknown segments {', '.join(unknown[:10])}")
        if not result.complaints:
            raise ValueError("an accepted problem needs at least one complaint")
    return check


def clean_complaints(complaints: list[Complaint], valid: set[str]) -> tuple[list[Complaint], int]:
    """Valid segment IDs only; a complaint without one is dropped; keys made unique."""
    out, dropped, keys = [], 0, set()
    for c in complaints:
        segs = [s for s in dict.fromkeys(c.seg_ids) if s in valid]
        if not segs:
            dropped += 1
            continue
        key, n = c.key, 2
        while key in keys:
            key = f"{c.key}_{n}"
            n += 1
        keys.add(key)
        out.append(c.model_copy(update={"key": key, "seg_ids": segs}))
    return out, dropped


def check_material(brief: CaseBrief, segments: list[UserSegment], english: dict[str, str]) -> tuple[str, list[str]]:
    data = brief.model_dump(mode="json")
    facts = [{"path": p, "text": f["text"], "seg_ids": f["seg_ids"]} for p, f in B.iter_facts(data)]
    body = "Segments:\n" + segment_lines(segments, english) + "\n\nFacts:\n" + json.dumps(facts, ensure_ascii=False,
                                                                                          indent=1)
    return llm.material(body, kind="brief_check"), [f["path"] for f in facts]


@dataclass
class IntakeOutcome:
    segments: list[UserSegment]
    result: IntakeResult
    brief: CaseBrief | None = None
    complaints: list[dict] | None = None
    systems: list[str] | None = None
    populations: list[str] | None = None
    check: dict | None = None
    stats: dict | None = None


async def generate(ctx: llm.CallContext, problem_text: str) -> IntakeOutcome:
    """Everything but the saving, so brief_update (1C.4) and the eval can reuse it."""
    segments = user_segments(problem_text)
    seg_ids = [s.seg_id for s in segments]
    system = llm.base_system() + "\n\n" + llm.prompt(GENERATE_VERSION)
    user = llm.material(segment_lines(segments), kind="problem")
    member = await ensemble.single(ctx, "intake", IntakeResult, system, user, prompt_version=GENERATE_VERSION,
                                   validate=validator(seg_ids))
    result: IntakeResult = member.value  # type: ignore[assignment]
    english = {t.seg_id: t.text_en for t in result.translations}
    for s in segments:
        s.text_en = english.get(s.seg_id)
    if not accepted(result):
        return IntakeOutcome(segments, result, stats={"segments": len(segments)})
    return await checked(ctx, segments, english, result.brief, result.complaints, result.proposed_systems,
                         result=result, model_label=member.model.label())


async def checked(ctx: llm.CallContext, segments: list[UserSegment], english: dict[str, str], brief: CaseBrief,
                  raw_complaints: list[Complaint], proposed_systems: list[str], *, result,
                  model_label: str) -> IntakeOutcome:
    """The code checks shared by intake and brief_update: segment IDs, brief check, unknowns, MeSH, systems,
    populations."""
    valid = {s.seg_id for s in segments}
    brief, bad_ids, removed = B.clean_segments(brief, valid)
    complaints, complaints_dropped = clean_complaints(raw_complaints, valid)

    check: dict = {"model": model_label, "counts": {}, "paths": 0}
    paths = B.fact_paths(brief)
    if paths:
        material, paths = check_material(brief, segments, english)
        checker = await ensemble.single(
            ctx, "brief_check", BriefCheck, llm.base_system() + "\n\n" + llm.prompt(CHECK_VERSION), material,
            prompt_version=CHECK_VERSION, validate=lambda c: B.check_covers(c, paths))
        brief, counts = B.apply_check(brief, checker.value)  # type: ignore[arg-type]
        check = {"model": checker.model.label(), "counts": counts, "paths": len(paths),
                 "items": [i.model_dump(mode="json") for i in checker.value.items]}  # type: ignore[union-attr]
    brief = B.dedupe_unknowns(B.ensure_unknowns(brief))

    saved, descriptors = [], []
    for c in complaints:
        verified, dropped = await mesh.verify(c.mesh_candidates, ctx.db)
        descriptors += verified
        saved.append({"key": c.key, "text": c.text, "seg_ids": c.seg_ids,
                      "mesh": [{"ui": d.ui, "name": d.name, "tree_numbers": d.tree_numbers, "systems": d.systems}
                               for d in verified],
                      "mesh_dropped": dropped})
    systems = mesh.union_systems(descriptors, proposed_systems)
    stats = {"segments": len(segments), "invalid_seg_ids": bad_ids, "facts_removed_uncited": removed,
             "complaints": len(saved), "complaints_dropped": complaints_dropped,
             "mesh_verified": len(descriptors), "mesh_dropped": sum(len(c["mesh_dropped"]) for c in saved),
             "unknowns": len(brief.unknowns)}
    return IntakeOutcome(segments, result, brief, saved, systems, B.populations(brief), check, stats)


# ---------- saving ----------

def save_segments(conn: sqlite3.Connection, investigation_id: int, segments: list[UserSegment]) -> None:
    conn.execute("DELETE FROM user_segments WHERE investigation_id = ? AND origin = 'problem'", (investigation_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO user_segments (investigation_id, seg_id, origin, ref_id, text, text_en) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(investigation_id, s.seg_id, s.origin, s.ref_id, s.text, s.text_en) for s in segments])


def save_brief(db, conn: sqlite3.Connection, investigation_id: int, brief: CaseBrief, *, source: str,
               check: dict | None, prompt_version: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM case_briefs WHERE investigation_id = ?",
                       (investigation_id,)).fetchone()
    version = int(row["v"]) + 1
    db.insert(conn, "case_briefs", {
        "investigation_id": investigation_id, "version": version, "data": brief.model_dump(mode="json"),
        "source": source, "check_results": check, "prompt_version": prompt_version, "created_at": now()})
    db.insert(conn, "note_outbox", {"investigation_id": investigation_id, "kind": "case_brief",
                                    "artifact_key": f"brief:v{version}", "created_at": now()}, or_ignore=True)
    return version


def latest_brief(conn: sqlite3.Connection, investigation_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM case_briefs WHERE investigation_id = ? ORDER BY version DESC LIMIT 1",
                       (investigation_id,)).fetchone()
    return dict(row) if row else None


async def run(tc) -> object:
    inv = tc.investigation
    version = inv["problem_version"]
    ctx = tc.services.with_task(task_id=tc.task["id"], step="intake", investigation_id=inv["id"])
    out = await generate(ctx, inv["problem_text"])
    english = [s.text_en for s in out.segments]
    lang = out.result.language.lower()

    def commit(conn: sqlite3.Connection) -> None:
        save_segments(conn, inv["id"], out.segments)
        values = {"problem_lang": lang,
                  "problem_text_en": " ".join(e for e in english if e) if lang != "en" and all(english) else None}
        if not (inv.get("title") or "").strip() and out.result.title.strip():
            values["title"] = out.result.title.strip()
        if out.brief is None:
            tc.db.update_investigation(inv["id"], conn=conn, **values)
            tc.db.event(inv["id"], "intake_not_accepted", out.result.not_accepted_reason or "",
                        level="warning", stage="intake",
                        data={"reason": out.result.not_accepted_reason, "problem_version": version}, conn=conn)
            return
        brief_version = save_brief(tc.db, conn, inv["id"], out.brief, source=f"intake:p{version}", check=out.check,
                                   prompt_version=f"{GENERATE_VERSION}+{CHECK_VERSION}")
        tc.db.update_investigation(inv["id"], conn=conn, complaints=out.complaints, systems=out.systems,
                                   special_populations=out.populations, **values)
        tc.db.event(inv["id"], "intake", f"brief v{brief_version}", stage="intake",
                    data={**(out.stats or {}), "brief_version": brief_version,
                          "check": (out.check or {}).get("counts")}, conn=conn)
    return commit


# ---------- stage ----------

def _not_accepted_reason(sc: StageContext, version: int) -> str | None:
    row = sc.conn.execute("SELECT data FROM events WHERE investigation_id = ? AND kind = 'intake_not_accepted' "
                          "ORDER BY id DESC LIMIT 1", (sc.id,)).fetchone()
    if row is None:
        return None
    data = json.loads(row["data"] or "{}")
    return data.get("reason") if data.get("problem_version") == version else None


def stage(next_stage: str = "safety") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("intake", f"intake:p{sc.inv['problem_version']}")

    def on_complete(sc: StageContext):
        version = sc.inv["problem_version"]
        if sc.conn.execute("SELECT 1 FROM case_briefs WHERE investigation_id = ? AND source = ?",
                           (sc.id, f"intake:p{version}")).fetchone():
            return next_stage
        reason = _not_accepted_reason(sc, version)
        if reason is not None:
            return Wait("problem_rewrite", REWRITE_MESSAGES.get(reason, REWRITE_MESSAGES["not_health"]))
        return STAY if sc.queued else Wait("problem_rewrite", REWRITE_MESSAGES["not_health"])
    return StageDef("intake", on_enter=on_enter, on_complete=on_complete)


STEP = StepDef("intake", run)


def rewrite(new_text: str):
    """`apply` for Machine.resolve_wait("problem_rewrite"): new text → new problem version → intake again."""
    text = (new_text or "").strip()
    if not text:
        raise ValueError("The new problem text is empty.")

    def apply(sc: StageContext) -> str:
        version = int(sc.inv["problem_version"]) + 1
        sc.conn.execute("DELETE FROM user_segments WHERE investigation_id = ? AND origin = 'problem'", (sc.id,))
        sc.db.update_investigation(sc.id, conn=sc.conn, problem_text=text, problem_version=version,
                                   problem_lang=None, problem_text_en=None)
        sc.db.event(sc.id, "problem_rewritten", f"p{version}", stage="intake", conn=sc.conn)
        return "intake"
    return apply
