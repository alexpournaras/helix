"""plan (PLAN B8.6): two planners → consolidation → knowledge coverage, and the `plan_approval` wait (B4).

plan:s{segment}
  1. planners (`plan`, A + B; `ensembles.planners` = 1 → one): each proposes a Plan from the brief, the case
     features, the person's segments and the differential knowledge items;
  2. consolidation (`plan.consolidate`, A): one plan plus `knowledge_coverage`;
  3. checks (code, one repair through the validator): hypothesis count within `plan.min/max_hypotheses`, unique
     keys, feature references exist, every case feature explained or uncovered with a reason, at least one
     red-flag hypothesis, every active differential item in the coverage, every exclusion cites the person's
     segments, `item_id`s are differential items;
  4. saves hypotheses (origin `knowledge` with an item, else `proposed`), coverage, uncovered features, a
     `research_plan` note.
Then `review_plan` autonomy waits for `plan_approval`; `autonomous` goes on to `querying`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import ensemble, llm
from ..db import _decode, now
from ..pipeline import StageContext, StageDef, StepDef, Wait
from ..schemas import CaseBrief, ConsolidatedPlan, Hypothesis, Plan
from . import safety

GENERATE_VERSION = "plan.generate.v1"
CONSOLIDATE_VERSION = "plan.consolidate.v1"


# ---------- inputs ----------

def differential_items(conn: sqlite3.Connection, investigation_id: int, kv: int) -> list[dict]:
    """Every active differential item at knowledge version ≤ k (all populations: each must be covered)."""
    rows = conn.execute("SELECT * FROM knowledge_items WHERE investigation_id = ? AND kind = 'differential' "
                        "AND status = 'active' AND version <= ? ORDER BY id", (investigation_id, kv)).fetchall()
    out = []
    for r in rows:
        d = _decode(r)
        attrs = d.get("attributes") or {}
        out.append({"ref": f"K{d['id']}", "id": d["id"], "text": d["text"],
                    "key_features": attrs.get("key_features") or [], "population": d.get("population") or "all",
                    "trust": d["trust"]})
    return out


def red_flag_items(conn: sqlite3.Connection, investigation_id: int, kv: int) -> list[str]:
    rows = conn.execute("SELECT text FROM knowledge_items WHERE investigation_id = ? AND kind = 'red_flag' "
                        "AND status = 'active' AND version <= ? ORDER BY id", (investigation_id, kv)).fetchall()
    return list(dict.fromkeys(r["text"] for r in rows))


def limits(settings: dict) -> tuple[int, int]:
    cfg = {"min_hypotheses": 5, "max_hypotheses": 14, **((settings or {}).get("plan") or {})}
    return int(cfg["min_hypotheses"]), int(cfg["max_hypotheses"])


def material(brief: CaseBrief, segs: list[dict], items: list[dict], red_flags: list[str], lim: tuple[int, int],
             plans: list[Plan] | None = None) -> str:
    body: dict[str, Any] = {
        "brief": brief.model_dump(mode="json", exclude_none=True, exclude={"case_features"}),
        "case_features": [{"key": f.key, "text": f.text} for f in brief.case_features],
        "segments": [{"seg_id": s["seg_id"], "text": s["text_en"] or s["text"]} for s in segs],
        "differential_items": [{"item": i["ref"], "text": i["text"], "key_features": i["key_features"],
                                "population": i["population"]} for i in items],
        "red_flags": red_flags,
        "hypotheses": {"min": lim[0], "max": lim[1]},
    }
    if plans is not None:
        body["plans"] = [p.model_dump(mode="json") for p in plans]
    return llm.material(json.dumps(body, ensure_ascii=False, indent=1), kind="plan")


# ---------- checks ----------

def check_hypotheses(hyps: list[Hypothesis], uncovered: list[str], *, feature_keys: set[str], item_refs: set[str],
                     lim: tuple[int, int], every_feature: bool = True, red_flag: bool = True) -> None:
    """The B8.6 checks shared by planners, consolidation and user edits. `uncovered` are feature keys.
    Raises ValueError (→ repair)."""
    if not lim[0] <= len(hyps) <= lim[1]:
        raise ValueError(f"the plan needs {lim[0]} to {lim[1]} hypotheses, not {len(hyps)}")
    keys = [h.key for h in hyps]
    if len(keys) != len(set(keys)):
        raise ValueError("hypothesis keys must be unique")
    for h in hyps:
        bad = [k for k in [*h.features_explained, *h.features_unexplained] if k not in feature_keys]
        if bad:
            raise ValueError(f"{h.key}: unknown case feature keys {', '.join(bad)}")
        if h.item_id is not None and h.item_id not in item_refs:
            raise ValueError(f"{h.key}: {h.item_id} is not a differential knowledge item in the material")
    bad = [k for k in uncovered if k not in feature_keys]
    if bad:
        raise ValueError(f"unknown uncovered feature keys {', '.join(bad)}")
    explained = {k for h in hyps for k in h.features_explained} | set(uncovered)
    missing = sorted(feature_keys - explained)
    if missing and every_feature:
        raise ValueError(f"case features neither explained nor listed as uncovered: {', '.join(missing)}")
    if red_flag and not any(h.kind == "red_flag" for h in hyps):
        raise ValueError("at least one red_flag hypothesis is required")


def planner_validator(feature_keys: set[str], item_refs: set[str], lim: tuple[int, int]):
    def check(p: Plan) -> None:
        check_hypotheses(p.hypotheses, [u.key for u in p.uncovered_features], feature_keys=feature_keys, item_refs=item_refs, lim=lim)
    return check


def consolidation_validator(feature_keys: set[str], item_refs: set[str], seg_ids: set[str], lim: tuple[int, int]):
    def check(p: ConsolidatedPlan) -> None:
        check_hypotheses(p.hypotheses, [u.key for u in p.uncovered_features], feature_keys=feature_keys, item_refs=item_refs, lim=lim)
        keys = {h.key for h in p.hypotheses}
        seen: set[str] = set()
        for c in p.knowledge_coverage:
            if c.item_id not in item_refs:
                raise ValueError(f"coverage names {c.item_id}, which is not a differential item in the material")
            if c.item_id in seen:
                raise ValueError(f"{c.item_id} appears twice in knowledge_coverage")
            seen.add(c.item_id)
            if c.decision == "hypothesis" and c.hypothesis_key not in keys:
                raise ValueError(f"{c.item_id}: hypothesis_key {c.hypothesis_key!r} is not a hypothesis in the plan")
            if c.decision == "excluded":
                if not c.seg_ids or any(s not in seg_ids for s in c.seg_ids):
                    raise ValueError(f"{c.item_id}: an exclusion must cite the person's segments from the material")
                if not c.reason.strip():
                    raise ValueError(f"{c.item_id}: an exclusion needs a reason")
                if any(h.item_id == c.item_id for h in p.hypotheses):
                    raise ValueError(f"{c.item_id} is excluded but a hypothesis comes from it")
        missing = sorted(item_refs - seen, key=lambda r: int(r[1:]))
        if missing:
            raise ValueError(f"every differential item needs a coverage decision; missing {', '.join(missing)}")
    return check


# ---------- the step ----------

async def run(tc) -> Any:
    inv = tc.investigation
    kv = int(inv["knowledge_version"])
    with tc.db.read() as conn:
        found = safety.current_brief(conn, inv["id"])
        segs = safety.segments(conn, inv["id"])
        items = differential_items(conn, inv["id"], kv)
        flags = red_flag_items(conn, inv["id"], kv)
    if found is None:
        raise RuntimeError("the plan needs a case brief")
    brief_version, brief = found
    settings = tc.settings or {}
    lim = limits(settings)
    feature_keys = {f.key for f in brief.case_features}
    item_refs = {i["ref"] for i in items}
    seg_ids = {s["seg_id"] for s in segs}
    ctx = tc.services.with_task(task_id=tc.task["id"], step="plan", investigation_id=inv["id"])
    gen_system = llm.base_system() + "\n\n" + llm.prompt(GENERATE_VERSION)
    gen_material = material(brief, segs, items, flags, lim)
    check = planner_validator(feature_keys, item_refs, lim)
    planners = int(((settings.get("ensembles") or {}).get("planners")) or 2)
    if planners <= 1:
        m = await ensemble.single(ctx, "plan", Plan, gen_system, gen_material, prompt_version=GENERATE_VERSION,
                                  validate=check)
        plans, independence = [m.value], "single"
    else:
        result = await ensemble.panel(ctx, "plan", Plan, gen_system, gen_material, prompt_version=GENERATE_VERSION,
                                      validate=check)
        plans, independence = result.values, result.independence
    con = await ensemble.single(
        ctx, "plan.consolidate", ConsolidatedPlan, llm.base_system() + "\n\n" + llm.prompt(CONSOLIDATE_VERSION),
        material(brief, segs, items, flags, lim, plans=plans),  # type: ignore[arg-type]
        prompt_version=CONSOLIDATE_VERSION, validate=consolidation_validator(feature_keys, item_refs, seg_ids, lim))
    final: ConsolidatedPlan = con.value  # type: ignore[assignment]
    by_ref = {i["ref"]: i["id"] for i in items}

    def commit(conn: sqlite3.Connection) -> None:
        rnd = int(inv["round"])
        existing = {r["key"] for r in conn.execute("SELECT key FROM hypotheses WHERE investigation_id = ?",
                                                   (inv["id"],))}
        # A coverage decision may name the hypothesis that covers an item without the hypothesis naming it.
        cover_item = {c.hypothesis_key: c.item_id for c in final.knowledge_coverage if c.decision == "hypothesis"}
        ids: dict[str, int] = {}
        for h in final.hypotheses:
            ref = h.item_id or cover_item.get(h.key)
            item_id = by_ref.get(ref) if ref else None
            if h.key in existing:
                continue  # a later segment's plan keeps the earlier hypothesis (keys are never reused)
            ids[h.key] = tc.db.insert(conn, "hypotheses", {
                "investigation_id": inv["id"], "key": h.key, "title": h.title, "description": h.description,
                "mechanism": h.mechanism, "kind": h.kind, "item_id": item_id,
                "features_explained": h.features_explained, "features_unexplained": h.features_unexplained,
                "discriminators": h.discriminators, "prior": h.prior, "prior_reason": h.prior_reason,
                "round_added": rnd, "origin": "knowledge" if item_id else "proposed"})
        for r in conn.execute("SELECT id, key FROM hypotheses WHERE investigation_id = ?", (inv["id"],)):
            ids.setdefault(r["key"], r["id"])
        for c in final.knowledge_coverage:
            conn.execute("INSERT OR REPLACE INTO knowledge_coverage (investigation_id, item_id, decision, "
                         "hypothesis_id, reason, seg_ids) VALUES (?, ?, ?, ?, ?, ?)",
                         (inv["id"], by_ref[c.item_id], c.decision,
                          ids.get(c.hypothesis_key) if c.decision == "hypothesis" else None, c.reason,
                          json.dumps(c.seg_ids)))
        for u in final.uncovered_features:
            conn.execute("INSERT OR REPLACE INTO plan_uncovered (investigation_id, feature_key, reason, round) "
                         "VALUES (?, ?, ?, ?)", (inv["id"], u.key, u.reason, rnd))
        seg = int(inv.get("segment_no") or 1)
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "research_plan",
                                           "artifact_key": f"plan:s{seg}", "created_at": now()}, or_ignore=True)
        tc.db.event(inv["id"], "plan", f"s{seg}", stage="planning", conn=conn, data={
            "segment": seg, "brief_version": brief_version, "knowledge_version": kv, "planners": len(plans),
            "independence": independence, "hypotheses": len(final.hypotheses),
            "red_flag": sum(1 for h in final.hypotheses if h.kind == "red_flag"),
            "proposed": sum(1 for h in final.hypotheses if not (h.item_id or cover_item.get(h.key))),
            "covered": sum(1 for c in final.knowledge_coverage if c.decision == "hypothesis"),
            "excluded": sum(1 for c in final.knowledge_coverage if c.decision == "excluded"),
            "uncovered": len(final.uncovered_features)})
    return commit


# ---------- stage and the approval wait ----------

def stage(next_stage: str = "querying") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("plan", f"plan:s{int(sc.inv.get('segment_no') or 1)}", {}, priority=30)

    def on_complete(sc: StageContext):
        if (sc.settings or {}).get("autonomy", "review_plan") == "review_plan":
            return Wait("plan_approval", "Please review the research plan: edit, add or retire hypotheses, "
                                         "then approve it.")
        return next_stage
    return StageDef("planning", on_enter=on_enter, on_complete=on_complete)


STEP = StepDef("plan", run)


def active_hypotheses(conn: sqlite3.Connection, investigation_id: int) -> list[dict]:
    return [_decode(r) for r in conn.execute("SELECT * FROM hypotheses WHERE investigation_id = ? AND "
                                             "status = 'active' ORDER BY id", (investigation_id,))]


class NeedsConfirmation(ValueError):
    """Retiring the last red-flag hypothesis needs `confirm_no_red_flag=True` (B8.6 approval)."""


def approve(*, retire: dict[str, str] | None = None, add: list[dict] | None = None,
            confirm_no_red_flag: bool = False, next_stage: str = "querying"):
    """`apply` for Machine.resolve_wait("plan_approval"). `retire` maps hypothesis keys to a reason; `add` is a
    list of Hypothesis dicts (origin `user`). The B8.6 checks apply to the resulting active set (count, unique
    keys, feature references); removing the last red-flag hypothesis raises NeedsConfirmation unless
    `confirm_no_red_flag`. A case feature only a retired hypothesis explained becomes uncovered. A failed check
    raises ValueError and nothing is written."""
    retire = retire or {}
    new = [Hypothesis.model_validate(h) for h in add or []]

    def apply(sc: StageContext) -> str:
        found = safety.current_brief(sc.conn, sc.id)
        brief = found[1] if found else CaseBrief()
        current = active_hypotheses(sc.conn, sc.id)
        known = {r["key"] for r in sc.conn.execute("SELECT key FROM hypotheses WHERE investigation_id = ?", (sc.id,))}
        unknown = [k for k in retire if k not in {h["key"] for h in current}]
        if unknown:
            raise ValueError(f"not an active hypothesis: {', '.join(unknown)}")
        reused = [h.key for h in new if h.key in known]
        if reused:
            raise ValueError(f"hypothesis keys are never reused: {', '.join(reused)}")
        items = differential_items(sc.conn, sc.id, int(sc.inv["knowledge_version"]))
        kept = [Hypothesis.model_validate({
            "key": h["key"], "title": h["title"], "kind": h["kind"] or "common",
            "features_explained": h["features_explained"], "features_unexplained": h["features_unexplained"],
            "prior": h["prior"] or "moderate"}) for h in current if h["key"] not in retire]
        result = kept + new
        removes_red_flag = (not any(h.kind == "red_flag" for h in result)
                            and any(h["kind"] == "red_flag" for h in current))
        if removes_red_flag and not confirm_no_red_flag:
            raise NeedsConfirmation("this removes the last red-flag hypothesis; confirm to continue")
        uncovered = [r["feature_key"] for r in sc.conn.execute(
            "SELECT feature_key FROM plan_uncovered WHERE investigation_id = ?", (sc.id,))]
        feature_keys = {f.key for f in brief.case_features}
        check_hypotheses(result, uncovered, feature_keys=feature_keys, item_refs={i["ref"] for i in items},
                         lim=limits(sc.settings), every_feature=False, red_flag=not removes_red_flag)
        rnd = int(sc.inv["round"])
        for key, reason in retire.items():
            sc.conn.execute("UPDATE hypotheses SET status = 'retired', retired_reason = ? WHERE investigation_id = ? "
                            "AND key = ?", (f"user: {reason}"[:500], sc.id, key))
            # Its knowledge items are now excluded, by the user.
            sc.conn.execute("UPDATE knowledge_coverage SET decision = 'excluded', hypothesis_id = NULL, reason = ? "
                            "WHERE investigation_id = ? AND hypothesis_id = (SELECT id FROM hypotheses WHERE "
                            "investigation_id = ? AND key = ?)", (f"retired by the user: {reason}"[:500], sc.id,
                                                                  sc.id, key))
        explained = {k for h in result for k in h.features_explained} | set(uncovered)
        for key in sorted(feature_keys - explained):
            sc.conn.execute("INSERT OR REPLACE INTO plan_uncovered (investigation_id, feature_key, reason, round) "
                            "VALUES (?, ?, ?, ?)", (sc.id, key, "Only a hypothesis the user retired explained it.", rnd))
        by_ref = {i["ref"]: i["id"] for i in items}
        for h in new:
            sc.db.insert(sc.conn, "hypotheses", {
                "investigation_id": sc.id, "key": h.key, "title": h.title, "description": h.description,
                "mechanism": h.mechanism, "kind": h.kind, "item_id": by_ref.get(h.item_id) if h.item_id else None,
                "features_explained": h.features_explained, "features_unexplained": h.features_unexplained,
                "discriminators": h.discriminators, "prior": h.prior, "prior_reason": h.prior_reason,
                "round_added": rnd, "origin": "user"})
        sc.event("plan_approved", data={"retired": len(retire), "added": len(new),
                                        "red_flag_removed": removes_red_flag})
        return next_stage
    return apply
