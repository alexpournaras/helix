"""safety (PLAN B8.2), safety_finalize (B8.3) and the B4 safety rule.

safety: rules (code) → panel (A + B) → combined review. The level is the most cautious of the rule level,
every panel level and every guideline red flag that fires; the panel can't lower it. Unknown rules,
unknown guideline red flags and the panel's rule-out questions become questions with priority 0.
A crisis rule is recorded the moment the stage is entered (before the panel), so the UI can show crisis
resources at once.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import brief as B, ensemble, llm, safety_rules as R
from ..db import _decode, now
from ..pipeline import Complete, StageContext, StageDef, StepDef, Wait
from ..schemas import CaseBrief, SafetyReview
from . import intake

PANEL_VERSION = "safety.panel.v1"
URGENCY_LEVEL = {"emergency": "emergency", "urgent": "urgent", "routine": "routine_review",
                 "routine_review": "routine_review"}
STATE_ORDER = ("not_applicable", "unknown", "fires")
DEFAULTS = {"allow_continue_after_urgent": True, "allow_continue_after_emergency": False}


# ---------- inputs ----------

def segments(conn: sqlite3.Connection, investigation_id: int) -> list[dict]:
    """Every user segment (problem, answers, follow-ups), in a stable order."""
    rows = conn.execute("SELECT seg_id, origin, text, text_en FROM user_segments WHERE investigation_id = ?",
                        (investigation_id,)).fetchall()
    order = {"problem": 0, "answer": 1, "followup": 2, "note": 3}

    def key(r):
        digits = "".join(ch for ch in r["seg_id"] if ch.isdigit())
        return (order.get(r["origin"], 9), int(digits or 0), r["seg_id"])
    return [dict(r) for r in sorted(rows, key=key)]


def person_text(seg: dict) -> str:
    """What the person wrote. An answer segment is "Q: … A: …": only the answer is theirs. The question's words
    ("Could you be pregnant?") must never trigger a rule."""
    text = seg["text_en"] or seg["text"]
    if seg.get("origin") == "answer" and " A: " in text:
        return text.split(" A: ", 1)[1]
    return text


def english(segs: list[dict]) -> list[tuple[str, str]]:
    """(seg_id, English text) for the rules: the person's own words only."""
    return [(s["seg_id"], person_text(s)) for s in segs]


def mesh_uis(inv: dict) -> list[str]:
    return [m["ui"] for c in inv.get("complaints") or [] for m in c.get("mesh") or []]


def guideline_red_flags(conn: sqlite3.Connection, investigation_id: int, knowledge_version: int,
                        populations: list[str] | None = None) -> list[dict]:
    """Active guideline red flags at knowledge version ≤ k, for everyone or for one of the case's populations."""
    allowed = {"all", *(populations or [])}
    rows = conn.execute(
        "SELECT * FROM knowledge_items WHERE investigation_id = ? AND kind = 'red_flag' AND origin = 'guideline' "
        "AND status = 'active' AND version <= ? ORDER BY id", (investigation_id, knowledge_version)).fetchall()
    out = []
    for r in rows:
        item = _decode(r)
        attrs = item.get("attributes") or {}
        if (item.get("population") or "all") not in allowed:
            continue
        out.append({"ref": f"K{item['id']}", "id": item["id"], "text": item["text"],
                    "features": attrs.get("features") or [], "question": (attrs.get("question") or "").strip(),
                    "level": URGENCY_LEVEL.get(attrs.get("urgency"), "urgent"),
                    "population": item.get("population") or "all"})
    return out


def review_for(conn: sqlite3.Connection, investigation_id: int, brief_version: int, kv: int) -> dict | None:
    row = conn.execute("SELECT * FROM safety_reviews WHERE investigation_id = ? AND brief_version = ? AND "
                       "knowledge_version = ?", (investigation_id, brief_version, kv)).fetchone()
    return _decode(row) if row else None


def current_brief(conn: sqlite3.Connection, investigation_id: int) -> tuple[int, CaseBrief] | None:
    row = intake.latest_brief(conn, investigation_id)
    if row is None:
        return None
    data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    return int(row["version"]), CaseBrief.model_validate(data)


# ---------- the step ----------

def panel_material(segs: list[dict], brief: CaseBrief, rules: R.RuleOutcome, items: list[dict]) -> str:
    body = {
        "segments": [{"seg_id": s["seg_id"], "text": s["text_en"] or s["text"]} for s in segs],  # with the questions
        "brief": brief.model_dump(mode="json", exclude_none=True),
        "rules_fired": [{"ref": h.ref, "level": h.level, "title": h.title} for h in rules.fired],
        "rules_unknown": [{"ref": h.ref, "level": h.level, "title": h.title, "question": h.question}
                          for h in rules.unknown],
        "guideline_red_flags": [{"item": i["ref"], "text": i["text"], "features": i.get("features") or [],
                                 "level": i["level"], "population": i["population"]} for i in items],
    }
    return llm.material(json.dumps(body, ensure_ascii=False, indent=1), kind="safety")


def panel_validator(items: list[dict]):
    wanted = {i["ref"] for i in items}

    def check(review: SafetyReview) -> None:
        got = [c.item for c in review.guideline_checks]
        missing, extra = wanted - set(got), set(got) - wanted
        if missing:
            raise ValueError(f"guideline_checks missing {', '.join(sorted(missing))}")
        if extra:
            raise ValueError(f"guideline_checks for unknown items {', '.join(sorted(extra))}")
        if len(got) != len(set(got)):
            raise ValueError("one guideline_checks entry per item")
    return check


def combine(rules: R.RuleOutcome, members: list[tuple[str, SafetyReview]], items: list[dict],
            valid_ids: set[str]) -> dict:
    """B8.2 combining. `members` is [(model label, review)] for the members that answered."""
    flags = []
    for model, review in members:
        for f in review.flags:
            verified = bool(f.seg_ids) and all(s in valid_ids for s in f.seg_ids)
            flags.append({**f.model_dump(mode="json"), "model": model, "verified": verified})
    item_state: dict[str, str] = {}
    for _, review in members:
        for c in review.guideline_checks:
            prev = item_state.get(c.item, "not_applicable")
            item_state[c.item] = max(prev, c.state, key=STATE_ORDER.index)
    fired_items = [i for i in items if item_state.get(i["ref"]) == "fires"]
    unknown_items = [i for i in items if item_state.get(i["ref"]) == "unknown"]
    level = R.most_cautious([rules.level] + [r.level for _, r in members] + [i["level"] for i in fired_items])
    crisis = rules.crisis or any(r.crisis for _, r in members)
    questions: list[dict] = []
    for h in rules.unknown:
        questions.append({"key": f"ruleout.{h.ref}", "text": h.question, "why": h.title, "targets": [h.ref]})
    for i in unknown_items:
        if not i.get("question", "").endswith("?"):
            continue  # no plain question from the guideline extraction; the panel's own questions cover it
        questions.append({"key": f"ruleout.{i['ref']}", "text": i["question"], "why": i["text"],
                          "targets": [i["ref"]], "item_id": i["id"]})
    for _, review in members:
        for q in review.rule_out_questions:
            questions.append({"key": f"ruleout.panel.{q.key}", "text": q.text, "why": "safety panel",
                              "targets": []})
    seen, unique = set(), []
    for q in questions:
        if q["key"] not in seen:
            seen.add(q["key"])
            unique.append(q)
    recs = [r.recommendation for _, r in members]
    return {"level": level, "crisis": crisis, "flags": flags, "item_states": item_state,
            "fired_items": [i["ref"] for i in fired_items], "questions": unique,
            "display": R.display_text(level, recs, crisis)}


async def run(tc) -> Any:
    inv = tc.investigation
    with tc.db.read() as conn:
        found = current_brief(conn, inv["id"])
        segs = segments(conn, inv["id"])
        kv = int(inv["knowledge_version"])
        items = guideline_red_flags(conn, inv["id"], kv, inv.get("special_populations") or [])
    if found is None:
        raise RuntimeError("safety needs a case brief")
    brief_version, brief = found
    rules = R.evaluate(english(segs), B.case_facts(brief), mesh_uis(inv))
    ctx = tc.services.with_task(task_id=tc.task["id"], step="safety", investigation_id=inv["id"])
    system = llm.base_system() + "\n\n" + llm.prompt(PANEL_VERSION)
    result = await ensemble.panel(ctx, "safety", SafetyReview, system, panel_material(segs, brief, rules, items),
                                  prompt_version=PANEL_VERSION, validate=panel_validator(items))
    members = [(m.model.label(), m.value) for m in result.members if m.ok]
    combined = combine(rules, members, items, {s["seg_id"] for s in segs})

    def commit(conn: sqlite3.Connection) -> None:
        review_id = tc.db.insert(conn, "safety_reviews", {
            "investigation_id": inv["id"], "brief_version": brief_version, "knowledge_version": kv,
            "rules_evaluated": rules.evaluated, "rule_level": rules.level,
            "rule_hits": [h.to_dict() for h in rules.fired + rules.unknown] +
                         [{"ref": r, "state": "true", "origin": "guideline"} for r in combined["fired_items"]],
            "panel": {"members": result.votes(), "independence": result.independence,
                      "substitutions": result.substitutions, "flags": combined["flags"],
                      "item_states": combined["item_states"], "crisis": combined["crisis"],
                      "questions": [q["key"] for q in combined["questions"]], "rules_version": R.RULES_VERSION,
                      "prompt_version": PANEL_VERSION},
            "level": combined["level"], "recommendation": combined["display"]})
        for q in combined["questions"]:
            tc.db.insert(conn, "clarifying_questions", {
                "investigation_id": inv["id"], "research_round": inv["round"], "origin": "safety",
                "item_id": q.get("item_id"), "key": q["key"], "text": q["text"], "why": q["why"],
                "priority": 0, "qround": 0, "targets": q["targets"]}, or_ignore=True)
        tc.db.update_investigation(inv["id"], conn=conn, safety_level=combined["level"])
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "safety_review",
                                           "artifact_key": f"safety:b{brief_version}:k{kv}", "created_at": now()},
                     or_ignore=True)
        tc.db.event(inv["id"], "safety", combined["level"], stage="safety", conn=conn, data={
            "review_id": review_id, "brief_version": brief_version, "knowledge_version": kv,
            "rule_level": rules.level, "fired": len(rules.fired), "unknown": len(rules.unknown),
            "questions": len(combined["questions"]), "crisis": combined["crisis"],
            "independence": result.independence})
    return commit


# ---------- B4 safety rule ----------

def _cfg(sc: StageContext) -> dict:
    return {**DEFAULTS, **((sc.settings or {}).get("safety") or {})}


def _questions_cfg(sc: StageContext) -> dict:
    return {"max_rounds": 2, **((sc.settings or {}).get("questions") or {})}


def grounding_needed(sc: StageContext) -> bool:
    """A covered complaint has no grounding run (ok, gap or failed) for the current population key."""
    from .grounding import _cfg, wanted
    if not _cfg(sc.settings).get("enabled", True):
        return False
    for key, pop in wanted(sc.inv, sc.settings):
        if not sc.conn.execute("SELECT 1 FROM grounding_runs WHERE investigation_id = ? AND complaint_key = ? AND "
                               "population = ?", (sc.id, key, pop)).fetchone():
            return True
    return False


def questions_needed(sc: StageContext, review: dict | None) -> bool:
    """B4 step 4: rounds remain; a priority-1 unknown, an unanswered rule-out or an open priority-1 history
    item; and not a follow-up cycle unless a rule-out is pending."""
    rnd = sc.inv["round"]
    used = sc.conn.execute("SELECT COUNT(*) AS n FROM research_tasks WHERE investigation_id = ? AND step = 'questions' "
                           "AND key LIKE ?", (sc.id, f"questions:r{rnd}:%")).fetchone()["n"]
    if used >= int(_questions_cfg(sc)["max_rounds"]):
        return False
    asked = {r["key"]: r["status"] for r in sc.conn.execute(
        "SELECT key, status FROM clarifying_questions WHERE investigation_id = ?", (sc.id,))}
    pending_keys = set((review or {}).get("panel", {}).get("questions") or [])
    rule_out = any(asked.get(k) == "open" for k in pending_keys)
    if sc.inv.get("segment_no", 0) and not rule_out:
        return False  # a follow-up cycle asks only for pending rule-outs
    if rule_out:
        return True
    found = current_brief(sc.conn, sc.id)
    if found and any(u.priority == 1 and asked.get(u.key) in (None, "open") for u in found[1].unknowns):
        return True
    history = sc.conn.execute(
        "SELECT k.id FROM knowledge_items k WHERE k.investigation_id = ? AND k.kind = 'history_item' AND "
        "k.status = 'active' AND json_extract(k.attributes, '$.priority') IN ('high', 1) AND NOT EXISTS "
        "(SELECT 1 FROM clarifying_questions q WHERE q.investigation_id = k.investigation_id AND q.item_id = k.id "
        "AND q.status != 'open')", (sc.id,)).fetchone()
    return history is not None


def after_acknowledgement(sc: StageContext, review: dict | None) -> str:
    """B4 safety rule steps 3–5."""
    if grounding_needed(sc):
        return "grounding"
    if questions_needed(sc, review):
        return "questions"
    return sc.inv.get("return_stage") or "planning"


def decide(sc: StageContext, review: dict) -> Any:
    cfg = _cfg(sc)
    level, acked = review["level"], bool(review.get("acknowledged_at"))
    if level == "emergency":
        if not cfg["allow_continue_after_emergency"]:
            return "safety_finalize"
        if not acked:
            return Wait("safety_ack", R.TEMPLATES["emergency"])
    elif level == "urgent" and not acked:
        if not cfg["allow_continue_after_urgent"]:
            return "safety_finalize"
        return Wait("safety_ack", R.TEMPLATES["urgent"])
    return after_acknowledgement(sc, review)


def stage() -> StageDef:
    def on_enter(sc: StageContext) -> None:
        found = current_brief(sc.conn, sc.id)
        if found is None:
            return
        version, brief = found
        kv = int(sc.inv["knowledge_version"])
        if review_for(sc.conn, sc.id, version, kv):
            return
        # Crisis resources can't wait for the panel: record a crisis rule hit now (IDs only, no text).
        rules = R.evaluate(english(segments(sc.conn, sc.id)), B.case_facts(brief), mesh_uis(sc.inv))
        if rules.crisis and not sc.conn.execute(
                "SELECT 1 FROM events WHERE investigation_id = ? AND kind = 'crisis' AND "
                "json_extract(data, '$.brief_version') = ?", (sc.id, version)).fetchone():
            sc.event("crisis", "crisis resources", level="warning",
                     data={"brief_version": version, "rules": [h.ref for h in rules.fired if h.crisis]})
            sc.update(safety_level="emergency")
        sc.queue("safety", f"safety:b{version}:k{kv}", priority=10)

    def on_complete(sc: StageContext):
        found = current_brief(sc.conn, sc.id)
        if found is None:
            return Complete("incomplete", "No case brief.")
        review = review_for(sc.conn, sc.id, found[0], int(sc.inv["knowledge_version"]))
        if review is None:
            return Complete("incomplete", "The safety review is missing.")
        return decide(sc, review)
    return StageDef("safety", on_enter=on_enter, on_complete=on_complete)


STEP = StepDef("safety", run)


def acknowledge():
    """`apply` for Machine.resolve_wait("safety_ack"): acknowledge the current review, then steps 3–5."""
    def apply(sc: StageContext) -> str:
        found = current_brief(sc.conn, sc.id)
        review = review_for(sc.conn, sc.id, found[0], int(sc.inv["knowledge_version"])) if found else None
        if review is None:
            return "safety"
        sc.conn.execute("UPDATE safety_reviews SET acknowledged_at = ? WHERE id = ?", (now(), review["id"]))
        sc.db.event(sc.id, "safety_ack", review["level"], stage="safety", conn=sc.conn,
                    data={"review_id": review["id"]})
        review["acknowledged_at"] = now()
        nxt = decide(sc, review)
        return nxt if isinstance(nxt, str) else "safety"
    return apply


# ---------- safety_finalize (B8.3) ----------

def safety_report(segs: list[dict], review: dict) -> tuple[str, dict]:
    """Markdown and data: fixed text, flags with the user's own sentences, crisis resources, the stop notice."""
    by_id = {s["seg_id"]: s for s in segs}
    panel = review.get("panel") or {}
    crisis = bool(panel.get("crisis")) or any(h.get("crisis") for h in review.get("rule_hits") or []
                                              if h.get("state") == "true")
    lines = ["# Safety", ""]
    if crisis:
        lines += [R.CRISIS_RESOURCES, ""]
    lines += [R.TEMPLATES.get(review["level"]) or R.TEMPLATES["urgent"], ""]
    flags = []
    for h in review.get("rule_hits") or []:
        if h.get("state") == "true" and h.get("title"):
            flags.append({"title": h["title"], "seg_ids": h.get("seg_ids") or [], "ref": h["ref"]})
    for f in panel.get("flags") or []:
        if f.get("verified"):
            flags.append({"title": f["flag"], "seg_ids": f["seg_ids"], "ref": f.get("rule_ref")})
    if flags:
        lines.append("## What raised the concern")
        for f in flags:
            quotes = [by_id[s]["text"] for s in f["seg_ids"] if s in by_id]
            said = " ".join(f"“{q}”" for q in quotes)
            lines.append(f"- {f['title']}" + (f": you wrote {said}" if said else ""))
        lines.append("")
    lines.append(R.STOPPED)
    data = {"level": review["level"], "review_id": review["id"], "crisis": crisis, "flags": flags}
    return "\n".join(lines).strip() + "\n", data


async def run_finalize(tc) -> Any:
    inv = tc.investigation

    def commit(conn: sqlite3.Connection) -> None:
        found = current_brief(conn, inv["id"])
        review = review_for(conn, inv["id"], found[0], int(inv["knowledge_version"])) if found else None
        if review is None:
            raise RuntimeError("safety_finalize needs a safety review")
        markdown, data = safety_report(segments(conn, inv["id"]), review)
        version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM reports WHERE investigation_id = ?",
                               (inv["id"],)).fetchone()["v"]
        tc.db.insert(conn, "reports", {"investigation_id": inv["id"], "version": version, "kind": "safety",
                                       "status": "final", "data": data, "markdown": markdown})
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "final_report",
                                           "artifact_key": f"report:v{version}", "created_at": now()}, or_ignore=True)
        tc.db.event(inv["id"], "safety_report", f"v{version}", stage="safety_finalize", conn=conn,
                    data={"level": review["level"], "crisis": data["crisis"]})
    return commit


def finalize_stage() -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("safety_finalize", f"safety_finalize:s{sc.inv['segment_no']}", priority=0)

    def on_complete(sc: StageContext):
        return Complete("safety_stop", "Research stopped for safety.")
    return StageDef("safety_finalize", on_enter=on_enter, on_complete=on_complete)


FINALIZE_STEP = StepDef("safety_finalize", run_finalize, code_step=True)
