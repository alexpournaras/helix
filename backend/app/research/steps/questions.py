"""questions (PLAN B8.4) and the `answers` wait (B4).

The questions step asks the model for questions about the open unknowns and the knowledge's history items, then
code makes the set safe and complete:
- keys are unique and never reused (a proposal whose key was asked before is dropped);
- choice questions have 2–8 options (checked; one repair call);
- every pending rule-out and every open priority-1 history item and unknown is present (code adds the missing
  ones with the item's own wording);
- duplicate panel rule-outs are merged (the dropped ones are closed as skipped, marked "merged");
- at most `questions.max_per_round` are asked: safety first, then priority, then expected impact. The rest stay
  open with `qround = 0` for a later round.
Asked questions carry the round's `qround`. Answers become `A{id}` segments; any answer → brief_update, all
skipped → the safety rule's steps 3–5.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .. import ensemble, llm
from ..db import _decode, now
from ..pipeline import Fail, StageContext, StageDef, StepDef, Wait
from ..schemas import Question, QuestionSet
from ..segment import answer_segment
from . import safety

GENERATE_VERSION = "questions.generate.v1"
PRIORITY = {"high": 1, "medium": 2, "low": 3, 1: 1, 2: 2, 3: 3}
FALLBACK = {"age": "How old are you?", "sex": "What is your sex?",
            "pregnancy": "Could you be pregnant, or have you given birth in the last 6 weeks?"}


# ---------- state ----------

def closed_rounds(conn: sqlite3.Connection, investigation_id: int, rnd: int) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM events WHERE investigation_id = ? AND kind = 'questions_closed' "
                        "AND json_extract(data, '$.round') = ?", (investigation_id, rnd)).fetchone()["n"]


def current_qround(conn: sqlite3.Connection, inv: dict) -> int:
    return closed_rounds(conn, inv["id"], inv["round"]) + 1


def all_questions(conn: sqlite3.Connection, investigation_id: int) -> list[dict]:
    return [_decode(r) for r in conn.execute("SELECT * FROM clarifying_questions WHERE investigation_id = ? ORDER BY id",
                                             (investigation_id,))]


def asked_now(conn: sqlite3.Connection, inv: dict) -> list[dict]:
    q = current_qround(conn, inv)
    return [r for r in all_questions(conn, inv["id"])
            if r["status"] == "open" and r["qround"] == q and r["research_round"] == inv["round"]]


def history_items(conn: sqlite3.Connection, investigation_id: int, kv: int) -> list[dict]:
    """Active history items at knowledge version ≤ k, best first: priority, then attestation count."""
    rows = conn.execute(
        "SELECT k.*, (SELECT COUNT(*) FROM knowledge_attestations a WHERE a.item_id = k.id) AS n_att "
        "FROM knowledge_items k WHERE k.investigation_id = ? AND k.kind = 'history_item' AND k.status = 'active' "
        "AND k.version <= ?", (investigation_id, kv)).fetchall()
    items = []
    for r in rows:
        d = _decode(r)
        attrs = d.get("attributes") or {}
        question = (attrs.get("question") or (d["text"] if d["text"].rstrip().endswith("?") else "")).strip()
        items.append({"id": d["id"], "ref": f"K{d['id']}", "text": d["text"], "question": question,
                      "priority": PRIORITY.get(attrs.get("priority"), 2), "attestations": d["n_att"]})
    items.sort(key=lambda i: (i["priority"], -i["attestations"], i["id"]))
    return items


# ---------- the step ----------

def material(brief, unknowns, items, rule_outs, asked) -> str:
    body = {"brief": brief.model_dump(mode="json", exclude_none=True, exclude={"unknowns"}),
            "unknowns": [{"key": u.key, "text": u.text, "priority": u.priority} for u in unknowns],
            "knowledge_items": [{"item": i["ref"], "text": i["question"] or i["text"], "priority": i["priority"]}
                                for i in items],
            "rule_outs": [{"key": r["key"], "text": r["text"]} for r in rule_outs],
            "asked": [{"key": r["key"], "text": r["text"]} for r in asked]}
    return llm.material(json.dumps(body, ensure_ascii=False, indent=1), kind="questions")


def validator(rule_out_keys: set[str]):
    def check(qs: QuestionSet) -> None:
        keys = [q.key for q in qs.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("question keys must be unique")
        for q in qs.questions:
            if q.answer_type == "choice" and not 2 <= len(q.options) <= 8:
                raise ValueError(f"{q.key}: a choice question needs 2 to 8 options")
        for m in qs.merge_rule_outs:
            for k in [m.keep, *m.drop]:
                if k not in rule_out_keys:
                    raise ValueError(f"merge names an unknown rule-out {k}")
            if any(not k.startswith("ruleout.panel.") for k in m.drop):
                raise ValueError("only panel rule-outs can be dropped")
    return check


def fallback_question(key: str, text: str, *, priority: int, item_id: int | None = None,
                      targets: list[str] | None = None) -> dict:
    if text.rstrip().endswith("?"):
        wording = text.strip()
    else:
        wording = FALLBACK.get(key) or f"Could you tell us more about this: {text.strip().rstrip('.')}?"
    return {"key": key, "text": wording[:300], "why": "", "answer_type": "text", "options": [],
            "priority": priority, "expected_impact": 0.5, "targets": targets or [], "item_id": item_id,
            "origin": "grounding" if item_id else "intake"}


def plan(proposals: list[Question], *, existing: list[dict], rule_outs: list[dict], unknowns, items,
         merges, max_per_round: int) -> tuple[list[dict], list[int], list[tuple[int, str]]]:
    """(new question rows, ids of existing rows to ask now, (id, kept key) of rule-outs merged away).
    New rows carry `ask` = True when asked this round."""
    used = {r["key"] for r in existing}
    item_ids = {i["ref"]: i["id"] for i in items}
    new: list[dict] = []
    for q in proposals:
        if q.key in used or any(n["key"] == q.key for n in new):
            continue  # keys are never reused: it was asked before
        d = q.model_dump(mode="json")
        d["item_id"] = item_ids.get(q.item_id) if q.item_id else None
        d["origin"] = "grounding" if d["item_id"] else "intake"
        new.append(d)
    covered = {t for n in new for t in n["targets"]} | {n["key"] for n in new}
    covered |= {f"K{n['item_id']}" for n in new if n["item_id"]}
    asked_items = {r["item_id"] for r in existing if r.get("item_id")}
    for i in items:  # every open priority-1 history item is present
        if i["priority"] == 1 and i["ref"] not in covered and i["id"] not in asked_items:
            key = f"history.k{i['id']}"
            if key not in used:
                new.append(fallback_question(key, i["question"] or i["text"], priority=1, item_id=i["id"],
                                             targets=[i["ref"]]))
    for u in unknowns:  # and every priority-1 unknown
        if u.priority == 1 and u.key not in covered and u.key not in used:
            new.append(fallback_question(u.key, u.text, priority=1, targets=[u.key]))
    merged: list[tuple[int, str]] = []
    by_key = {r["key"]: r for r in rule_outs}
    for m in merges:
        for k in m.drop:
            if k in by_key and k != m.keep:
                merged.append((by_key.pop(k)["id"], m.keep))
    pending = sorted(by_key.values(), key=lambda r: r["id"])
    # Safety first, then priority-1 required items, then priority and expected impact.
    ranked = sorted(new, key=lambda n: (n["priority"], -float(n["expected_impact"])))
    slots = max_per_round
    ask_existing = [r["id"] for r in pending][:slots]
    slots -= len(ask_existing)
    for n in ranked:
        n["ask"] = slots > 0
        slots -= 1 if n["ask"] else 0
    return ranked, ask_existing, merged


async def run(tc) -> Any:
    inv = tc.investigation
    q = int(tc.payload["qround"])
    with tc.db.read() as conn:
        found = safety.current_brief(conn, inv["id"])
        existing = all_questions(conn, inv["id"])
        kv = int(inv["knowledge_version"])
        review = safety.review_for(conn, inv["id"], found[0], kv) if found else None
        items = history_items(conn, inv["id"], kv)
    if found is None:
        raise RuntimeError("questions need a case brief")
    brief = found[1]
    status = {r["key"]: r["status"] for r in existing}
    pending_keys = set((review or {}).get("panel", {}).get("questions") or [])
    rule_outs = [r for r in existing if r["origin"] == "safety" and r["status"] == "open" and r["key"] in pending_keys]
    unknowns = [u for u in brief.unknowns if status.get(u.key) in (None, "open")]
    asked_items = {r["item_id"] for r in existing if r.get("item_id")}
    open_items = [i for i in items if i["id"] not in asked_items]
    asked = [r for r in existing if r["status"] != "open" or r["qround"]]
    cfg = {"max_per_round": 10, **((tc.settings or {}).get("questions") or {})}
    ctx = tc.services.with_task(task_id=tc.task["id"], step="questions", investigation_id=inv["id"])
    proposals: list[Question] = []
    merges = []
    if unknowns or open_items or len(rule_outs) > 1:
        member = await ensemble.single(
            ctx, "questions", QuestionSet, llm.base_system() + "\n\n" + llm.prompt(GENERATE_VERSION),
            material(brief, unknowns, open_items, rule_outs, asked), prompt_version=GENERATE_VERSION,
            validate=validator({r["key"] for r in rule_outs}))
        proposals, merges = member.value.questions, member.value.merge_rule_outs  # type: ignore[union-attr]
    new, ask_ids, merged = plan(proposals, existing=existing, rule_outs=rule_outs, unknowns=unknowns,
                                items=open_items, merges=merges, max_per_round=int(cfg["max_per_round"]))

    def commit(conn: sqlite3.Connection) -> None:
        rnd = inv["round"]
        for row_id, kept in merged:
            conn.execute("UPDATE clarifying_questions SET status = 'skipped', why = COALESCE(why, '') || ? "
                         "WHERE id = ?", (f" (merged with {kept})", row_id))
        for row_id in ask_ids:
            conn.execute("UPDATE clarifying_questions SET qround = ?, research_round = ? WHERE id = ?", (q, rnd, row_id))
        for n in new:
            tc.db.insert(conn, "clarifying_questions", {
                "investigation_id": inv["id"], "research_round": rnd, "qround": q if n["ask"] else 0,
                "origin": n["origin"], "item_id": n["item_id"], "key": n["key"], "text": n["text"],
                "why": n["why"], "answer_type": n["answer_type"], "options": n["options"],
                "priority": n["priority"], "expected_impact": n["expected_impact"], "targets": n["targets"]},
                or_ignore=True)
        n_asked = len(ask_ids) + sum(1 for n in new if n["ask"])
        if n_asked:
            tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "clarifications",
                                               "artifact_key": f"questions:r{rnd}:q{q}", "created_at": now()},
                         or_ignore=True)
        tc.db.event(inv["id"], "questions", f"r{rnd}:q{q}", stage="questions", conn=conn, data={
            "round": rnd, "qround": q, "asked": n_asked, "rule_outs": len(ask_ids),
            "new": len(new), "deferred": sum(1 for n in new if not n["ask"]), "merged": len(merged),
            "proposed": len(proposals)})
        if not n_asked:
            close_round(tc.db, conn, inv["id"], rnd, q, answered=0, skipped=0)
    return commit


def close_round(db, conn, investigation_id: int, rnd: int, q: int, *, answered: int, skipped: int) -> None:
    db.event(investigation_id, "questions_closed", f"r{rnd}:q{q}", stage="questions", conn=conn,
             data={"round": rnd, "qround": q, "answered": answered, "skipped": skipped})


# ---------- stage and the answers wait ----------

def stage() -> StageDef:
    def on_enter(sc: StageContext) -> None:
        q = current_qround(sc.conn, sc.inv)
        sc.queue("questions", f"questions:r{sc.inv['round']}:q{q}", {"qround": q}, priority=30)

    def on_complete(sc: StageContext):
        if asked_now(sc.conn, sc.inv):
            return Wait("answers", "Please answer what you can; you can skip any question.")
        q = current_qround(sc.conn, sc.inv)
        task = sc.conn.execute("SELECT status FROM research_tasks WHERE investigation_id = ? AND key = ?",
                               (sc.id, f"questions:r{sc.inv['round']}:q{q}")).fetchone()
        if task is not None and task["status"] != "done":
            # The round's task didn't finish (e.g. failed earlier): moving on would loop safety ↔ questions.
            return Fail(f"The questions task for round {sc.inv['round']}.{q} did not complete.")
        found = safety.current_brief(sc.conn, sc.id)
        review = safety.review_for(sc.conn, sc.id, found[0], int(sc.inv["knowledge_version"])) if found else None
        return safety.after_acknowledgement(sc, review)
    return StageDef("questions", on_enter=on_enter, on_complete=on_complete)


STEP = StepDef("questions", run)


def answer(answers: dict[Any, str | None]):
    """`apply` for Machine.resolve_wait("answers"). `answers` maps a question id (or key) to the answer text;
    a missing or empty answer is a skip."""
    def apply(sc: StageContext) -> str:
        rows = asked_now(sc.conn, sc.inv)
        q = current_qround(sc.conn, sc.inv)
        answered = skipped = 0
        for r in rows:
            text = answers.get(r["id"], answers.get(str(r["id"]), answers.get(r["key"])))
            text = (text or "").strip()
            if text:
                seg = answer_segment(r["id"], r["text"], text)
                sc.conn.execute("UPDATE clarifying_questions SET status = 'answered', answer = ?, answered_at = ? "
                                "WHERE id = ?", (text[:2000], now(), r["id"]))
                sc.conn.execute("INSERT OR REPLACE INTO user_segments (investigation_id, seg_id, origin, ref_id, text) "
                                "VALUES (?, ?, 'answer', ?, ?)", (sc.id, seg.seg_id, seg.ref_id, seg.text))
                answered += 1
            else:
                sc.conn.execute("UPDATE clarifying_questions SET status = 'skipped', answered_at = ? WHERE id = ?",
                                (now(), r["id"]))
                skipped += 1
        close_round(sc.db, sc.conn, sc.id, sc.inv["round"], q, answered=answered, skipped=skipped)
        if answered:
            return "brief_update"
        found = safety.current_brief(sc.conn, sc.id)
        review = safety.review_for(sc.conn, sc.id, found[0], int(sc.inv["knowledge_version"])) if found else None
        nxt = safety.after_acknowledgement(sc, review)
        return nxt
    return apply


def scripted(case_answers: dict[str, str]):
    """Eval resolver: a case's `answers` map a question key, or a word or phrase in the question, to the answer."""
    def resolve(case, inv) -> Any:
        def apply(sc: StageContext) -> str:
            got: dict[Any, str] = {}
            for r in asked_now(sc.conn, sc.inv):
                for pattern, reply in case_answers.items():
                    if pattern == r["key"] or re.search(re.escape(pattern), r["text"], re.I):
                        got[r["id"]] = reply
                        break
            return answer(got)(sc)
        return apply
    return resolve
