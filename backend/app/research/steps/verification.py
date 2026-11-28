"""verify (PLAN B8.15): panel → escalate → adjudicate for every new claim, then the re-read rule; then `mapping`
(1E.3).

verify:r{read_id}  the paper's pending claims (cluster heads): two verifiers (`verify`, avoiding the extractor's
                   model), the B8.15 code rules on each vote (`verify.claim_rule`), escalation with the surrounding
                   paragraphs (± 1) when the votes disagree or ask for context, adjudication by the strongest model of
                   the other family with the whole section and the study profile. supported / partial → `active`
                   (with `verification`), unsupported → `rejected`. Every vote goes to `claim_votes`; a
                   `paper_card` note per paper. More than `extraction.max_rejected_ratio` rejected → the paper is
                   flagged `reread_needed`.
reread:r{read_id}  strict extraction (`read.claims.strict.v1`, per pack, no whole-paper pass), the same checks,
                   reconciliation and verification; the set with more accepted (supported or partial) claims is kept,
                   the other becomes `superseded`; the paper is flagged `reread` (once).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .. import llm, verify as V
from ..db import _decode, now
from ..packing import pack_snapshot
from ..pipeline import STAY, StageContext, StageDef, StepDef
from ..schemas import ClaimVoteBatch
from ..segment import Snapshot, part_of
from . import grounding as G, read as R, safety
from .screen import case_summary

VERIFY_VERSION = "verify.claims.v1"
STRICT_VERSION = "read.claims.strict.v1"
SECTION_LIMIT = 12000


# ---------- material ----------

def claim_text(c: dict) -> str:
    text = c["claim"]
    eff = c.get("effect")
    if eff:
        bits = [f"{eff['measure']} {eff.get('value')}"]
        if eff.get("ci_low") is not None:
            bits.append(f"95% CI {eff['ci_low']} to {eff.get('ci_high')}")
        if eff.get("p"):
            bits.append(f"p {eff['p']}")
        text += f" (effect: {', '.join(bits)})"
    if c.get("population"):
        text += f" — population: {c['population']}"
    return text


def section_text(snap: Snapshot, ids: list[str], limit: int = SECTION_LIMIT) -> str:
    parts = {part_of(i) for i in ids}
    return " ".join(s.text for s in snap.segments if part_of(s.seg_id) in parts)[:limit]


def render(pairs: list[V.Pair], with_context: bool) -> str:
    lines = []
    for p in pairs:
        lines += [f"{p.pair_id}: {p.text}", f"  Text: {p.quote}"]
        if with_context:
            lines.append(f"  Context: {p.context}")
        lines.append("")
    return llm.material("\n".join(lines).strip(), kind="claim_verify")


def adjudication_renderer(snap: Snapshot, profile: dict, ids_by_pair: dict[str, list[str]]):
    prof = {k: (v or {}).get("value") for k, v in (profile or {}).items() if (v or {}).get("value")}

    def render_adjudicate(pairs: list[V.Pair]) -> str:
        lines = [f"Study profile: {json.dumps(prof, ensure_ascii=False)}", ""]
        for p in pairs:
            lines += [f"{p.pair_id}: {p.text}", f"  Text: {p.quote}",
                      f"  Section: {section_text(snap, ids_by_pair[p.pair_id])}", ""]
        return llm.material("\n".join(lines).strip(), kind="claim_adjudicate")
    return render_adjudicate


CAUSAL = re.compile(r"\b(caus\w*|leads? to|led to|results? in|resulted in|induc\w*|trigger\w*|due to|"
                    r"because of|attributable|responsible for)\b", re.I)


def claim_triggers(c: dict) -> list[str]:
    """Second-opinion triggers known when the claim is verified (PLAN B17.5 item 5). `number_in_conclusion` and
    `rank_changing` are covered later by `confirm`, which re-verifies the claims that drive the ratings."""
    checks = c.get("checks") or {}
    out = []
    if checks.get("ids_dropped") or checks.get("effect_cleared") or checks.get("n_cleared"):
        out.append("code_flag")
    if c.get("strong_link"):
        out.append("strong_link")
    if CAUSAL.search(c.get("claim") or ""):
        out.append("causal")
    if c.get("red_flag_link"):
        out.append("red_flag")
    if c.get("evidence_type") in ("guideline_recommendation", "diagnostic_accuracy"):
        out.append("recommendation")
    return out


async def verify_claims(ctx, claims: list[dict], snap: Snapshot, profile: dict) -> list[V.Pair]:
    """`claims` = [{claim, effect, population, quote, sentence_ids, extractor_model, extractor_family}] in order;
    returns one Pair per claim (same order) with `verdict` and `votes`."""
    pairs, ids_by_pair = [], {}
    for n, c in enumerate(claims, 1):
        pid = f"C{n}"
        ids_by_pair[pid] = list(c["sentence_ids"])
        pairs.append(V.Pair(pid, claim_text(c), c["quote"], G._context(snap, c["sentence_ids"]),
                            c.get("extractor_model") or "", c.get("extractor_family") or "",
                            triggers=claim_triggers(c), seed=c.get("claim") or ""))
    if pairs:
        await V.verify(ctx, "verify", pairs, system=llm.base_system() + "\n\n" + llm.prompt(VERIFY_VERSION),
                       prompt_version=VERIFY_VERSION, render=render, schema=ClaimVoteBatch, rule=V.claim_rule,
                       render_adjudicate=adjudication_renderer(snap, profile, ids_by_pair))
    return pairs


def write_votes(conn: sqlite3.Connection, claim_id: int, pair: V.Pair) -> dict:
    """Store every vote; set status and verification. Returns {status, escalated, adjudicated}."""
    stages = set()
    for v in pair.votes:
        stages.add(v["stage"])
        checks = {k: v.get(k) for k in ("verdict", "asserts_effect", "direction_check", "significance",
                                        "population_check", "overgeneralization", "context_needed", "family")}
        conn.execute("INSERT INTO claim_votes (claim_id, stage, model, verdict, checks, reason, created_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (claim_id, v["stage"], v["model"], v["rule_verdict"],
                                                      json.dumps(checks), (v.get("reason") or "")[:300], now()))
    verdict = pair.verdict or "unsupported"
    status = "rejected" if verdict == "unsupported" else "active"
    conn.execute("UPDATE evidence_claims SET status = ?, verification = ?, escalated = ?, adjudicated = ?, "
                 "checks = json_set(COALESCE(checks, '{}'), '$.verification_mode', ?) WHERE id = ?",
                 (status, None if status == "rejected" else verdict, int("escalate" in stages or "second" in stages),
                  int("adjudicate" in stages), V.verification_mode(pair.votes), claim_id))
    return {"status": status, "escalated": "escalate" in stages, "adjudicated": "adjudicate" in stages}


def _ratio(settings: dict) -> float:
    return float((((settings or {}).get("extraction") or {}).get("max_rejected_ratio")) or 0.4)


def _flags(row: dict) -> list[str]:
    flags = row.get("quality_flags") or []
    return flags if isinstance(flags, list) else json.loads(flags)


def load_read(conn: sqlite3.Connection, read_id: int) -> tuple[dict, Snapshot]:
    row = _decode(conn.execute("SELECT * FROM paper_reads WHERE id = ?", (read_id,)).fetchone())
    return row, Snapshot.from_dict(row["segments"])


def claim_rows(conn: sqlite3.Connection, read_id: int, status: str) -> list[dict]:
    rows = [_decode(r) for r in conn.execute("SELECT * FROM evidence_claims WHERE read_id = ? AND status = ? "
                                             "ORDER BY id", (read_id, status))]
    links: dict[int, list[dict]] = {}
    if rows:
        ids = [r["id"] for r in rows]
        for l in conn.execute(
                f"SELECT l.claim_id, l.strength, l.direction, h.kind FROM claim_links l JOIN hypotheses h "
                f"ON h.id = l.hypothesis_id WHERE l.claim_id IN ({','.join('?' * len(ids))})", ids):
            links.setdefault(l["claim_id"], []).append(dict(l))
    for r in rows:
        checks = r.get("checks") or {}
        r["extractor_family"] = checks.get("extractor_family", "")
        mine = links.get(r["id"], [])
        r["strong_link"] = any(l["strength"] == "strong" for l in mine)
        r["red_flag_link"] = any(l["kind"] == "red_flag" and l["direction"] != "neutral" for l in mine)
    return rows


# ---------- verify ----------

async def run_verify(tc) -> Any:
    inv = tc.investigation
    read_id = int(tc.payload["read_id"])
    with tc.db.read() as conn:
        row, snap = load_read(conn, read_id)
        pending = claim_rows(conn, read_id, "pending")
    ctx = tc.services.with_task(task_id=tc.task["id"], step="verify", investigation_id=inv["id"])
    pairs = await verify_claims(ctx, pending, snap, row.get("profile") or {})
    ratio = _ratio(tc.settings)

    def commit(conn: sqlite3.Connection) -> None:
        counts = {"claims": len(pending), "active": 0, "rejected": 0, "escalated": 0, "adjudicated": 0}
        for c, p in zip(pending, pairs):
            got = write_votes(conn, c["id"], p)
            counts["active" if got["status"] == "active" else "rejected"] += 1
            counts["escalated"] += got["escalated"]
            counts["adjudicated"] += got["adjudicated"]
        flags = _flags(row)
        if pending and counts["rejected"] / len(pending) > ratio and not row.get("reread") and "reread_needed" not in flags:
            flags.append("reread_needed")
            conn.execute("UPDATE paper_reads SET quality_flags = ? WHERE id = ?", (json.dumps(flags), read_id))
        tc.db.insert(conn, "note_outbox", {"investigation_id": inv["id"], "kind": "paper_card",
                                           "artifact_key": f"paper:{read_id}", "created_at": now()}, or_ignore=True)
        tc.db.event(inv["id"], "verify", f"r{read_id}", stage="verifying", conn=conn,
                    data={"read_id": read_id, "reread_needed": "reread_needed" in flags, **counts})
    return commit


# ---------- re-read ----------

async def run_reread(tc) -> Any:
    inv = tc.investigation
    read_id = int(tc.payload["read_id"])
    with tc.db.read() as conn:
        row, snap = load_read(conn, read_id)
        old = [_decode(r) for r in conn.execute(
            "SELECT id, status FROM evidence_claims WHERE read_id = ? AND status IN ('active', 'rejected') "
            "AND COALESCE(pass, '') NOT LIKE '%:reread'", (read_id,))]
        scr = _decode(conn.execute("SELECT design FROM screenings WHERE candidate_id = ?",
                                   (row["candidate_id"],)).fetchone() or {}) if row.get("candidate_id") else {}
        hyps = [_decode(r) for r in conn.execute("SELECT id, key, title, description FROM hypotheses WHERE "
                                                 "investigation_id = ? AND status = 'active' ORDER BY id", (inv["id"],))]
        found = safety.current_brief(conn, inv["id"])
    if found is None:
        raise RuntimeError("the re-read needs a case brief")
    cfg, ens = R._cfg(tc.settings), R._ens(tc.settings)
    packs = pack_snapshot(snap, int(cfg["max_pack_tokens"]), cfg.get("max_packs_per_paper"),
                          priority=bool(cfg.get("pack_priority"))).packs
    profile = row.get("profile") or {}
    ctx = tc.services.with_task(task_id=tc.task["id"], step="reread", investigation_id=inv["id"])
    ext = await R.extract_claims(ctx, snap=snap, packs=packs, summary=case_summary(found[1], inv), hyps=hyps,
                                 design=scr.get("design") or "other", limit=int(cfg["max_claims_per_pack"]),
                                 profile_n=(profile.get("n") or {}).get("value"), dual=ens["dual_extraction"],
                                 whole=False, version=STRICT_VERSION)
    heads = []
    for cl in ext.clusters:
        label, ck = cl.kept
        ref = next(c[1] for c in ext.checked if c[4] is ck)
        c = ck.claim
        heads.append({"claim": c.claim, "effect": c.effect.model_dump(mode="json") if c.effect else None,
                      "population": c.population, "quote": ck.quote, "sentence_ids": ck.sentence_ids,
                      "extractor_model": ref.model, "extractor_family": ref.family})
    pairs = await verify_claims(ctx, heads, snap, profile)
    old_accepted = sum(1 for o in old if o["status"] == "active")
    new_accepted = sum(1 for p in pairs if p.verdict in ("supported", "partial"))
    keep_new = new_accepted > old_accepted

    def commit(conn: sqlite3.Connection) -> None:
        by_key = {h["key"]: h["id"] for h in hyps}
        counts = R.save_claims(tc.db, conn, inv["id"], read_id, row.get("literature_id"), int(inv["round"]), ext, snap,
                               by_key, pass_suffix=":reread")
        for claim_id, p in zip(counts["ids"], pairs):
            write_votes(conn, claim_id, p)
        if keep_new:
            for o in old:
                if o["status"] == "active":
                    conn.execute("UPDATE evidence_claims SET status = 'superseded' WHERE id = ?", (o["id"],))
        else:
            for claim_id in counts["ids"]:
                conn.execute("UPDATE evidence_claims SET status = 'superseded' WHERE id = ? AND status = 'active'",
                             (claim_id,))
        flags = [f for f in _flags(row) if f != "reread_needed"] + ["reread", "reread_kept_" + ("new" if keep_new else "old")]
        conn.execute("UPDATE paper_reads SET reread = 1, quality_flags = ? WHERE id = ?", (json.dumps(flags), read_id))
        tc.db.event(inv["id"], "reread", f"r{read_id}", stage="verifying", conn=conn, data={
            "read_id": read_id, "old_accepted": old_accepted, "new_accepted": new_accepted, "kept": "new" if keep_new
            else "old", "new_claims": counts["claims"]})
    return commit


# ---------- stage ----------

def stage(next_stage: str = "mapping") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        for r in sc.conn.execute("SELECT DISTINCT read_id FROM evidence_claims WHERE investigation_id = ? AND "
                                 "status = 'pending' ORDER BY read_id", (sc.id,)):
            sc.queue("verify", f"verify:r{r['read_id']}", {"read_id": r["read_id"]}, priority=85)

    def on_complete(sc: StageContext):
        queued = 0
        for r in sc.conn.execute("SELECT id, quality_flags FROM paper_reads WHERE investigation_id = ? AND reread = 0 "
                                 "ORDER BY id", (sc.id,)):
            if "reread_needed" in _flags(dict(r)):
                queued += sc.queue("reread", f"reread:r{r['id']}", {"read_id": r["id"]}, priority=85)
        return STAY if queued else next_stage
    return StageDef("verifying", on_enter=on_enter, on_complete=on_complete, skip_check_steps=("verify",))


VERIFY_STEP = StepDef("verify", run_verify, on_fail="skip")
REREAD_STEP = StepDef("reread", run_reread, on_fail="skip")
