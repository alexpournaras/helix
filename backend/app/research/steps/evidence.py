"""map (PLAN B8.16), group (B8.17) and confirm (B8.18); then `matrix` (1E.4).

map:b{hash}     active claims in batches of 20 (`map`, one model) against the hypotheses each claim hasn't been
                mapped to yet (`checks.mapped` keeps the hypothesis IDs seen, so older claims meet only new
                hypotheses). Mapping links are stored with origin `mapping`; they win over extraction links, and a
                neutral one cancels it (`weights.effective_links`). Disagreements with the extraction are counted.
group:r{round}  (code, library for the references) union-find over the papers read: a shared registry ID or the
                same normalised dataset or cohort name → one study. Kinds: guideline, review_sr, review_narrative,
                primary. Review inclusion: a review's references (PMID, PMCID, DOI) matched against the papers read.
confirm:b{hash} per hypothesis the top `confirm.per_hypothesis` supporting and contradicting claims by provisional
                weight, plus every `strong` link, at most `confirm.max_claims`; re-verified by the strongest model of
                the family that didn't extract (with the whole section, its neighbours and the profile). The most
                cautious verdict wins (`unsupported` rejects the claim); `consequential = 1`.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from .. import ensemble, llm, verify as V, weights as W
from ..db import _decode, now
from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import ClaimVoteBatch, MapBatch
from ..segment import Snapshot, part_of
from ..sources import sources_for
from . import safety, verification as VF
from .screen import case_summary

MAP_VERSION = "map.v1"
CONFIRM_VERSION = "verify.claims.v1"  # the confirmer answers the verification schema
MAP_BATCH = 20
CONFIRM_BATCH = 10
VERDICT_ORDER = ["supported", "partial", "unsupported"]
GENERIC_DATASETS = {"", "none", "no", "na", "n a", "not stated", "not reported", "unknown"}


def _key(prefix: str, ids: list[int]) -> str:
    return f"{prefix}:b" + hashlib.sha1(",".join(map(str, sorted(ids))).encode()).hexdigest()[:10]


def _checks(row: dict) -> dict:
    c = row.get("checks") or {}
    return c if isinstance(c, dict) else json.loads(c)


# ---------- map ----------

def unmapped(conn: sqlite3.Connection, investigation_id: int) -> dict[tuple[int, ...], list[int]]:
    """{hypothesis IDs still to map: [claim IDs]} for active claims."""
    hyps = [r["id"] for r in conn.execute("SELECT id FROM hypotheses WHERE investigation_id = ? AND status = 'active' "
                                          "ORDER BY id", (investigation_id,))]
    groups: dict[tuple[int, ...], list[int]] = {}
    for r in conn.execute("SELECT id, checks FROM evidence_claims WHERE investigation_id = ? AND status = 'active' "
                          "ORDER BY id", (investigation_id,)):
        seen = set(_checks(dict(r)).get("mapped") or [])
        missing = tuple(h for h in hyps if h not in seen)
        if missing:
            groups.setdefault(missing, []).append(r["id"])
    return groups


def map_material(summary: dict, hyps: list[dict], claims: list[dict]) -> str:
    lines = [f"Case: {json.dumps(summary, ensure_ascii=False)}", "", "Hypotheses:"]
    lines += [f"- {h['key']}: {h['title']}" + (f" — {h['description']}" if h.get("description") else "") for h in hyps]
    lines += ["", "Claims:"]
    for n, c in enumerate(claims, 1):
        lines.append(f"C{n}: {c['claim']} (population: {c.get('population') or 'not stated'}; design: "
                     f"{c.get('design') or 'unknown'}; type: {c.get('evidence_type')})")
    return llm.material("\n".join(lines), kind="map")


def map_validator(n: int):
    wanted = {f"C{i}" for i in range(1, n + 1)}

    def check(b: MapBatch) -> None:
        got = [i.claim for i in b.items]
        if set(got) != wanted or len(got) != len(wanted):
            raise ValueError("one item per claim is required")
    return check


async def run_map(tc) -> Any:
    inv = tc.investigation
    claim_ids = [int(i) for i in tc.payload["claim_ids"]]
    hyp_ids = [int(i) for i in tc.payload["hypothesis_ids"]]
    with tc.db.read() as conn:
        claims = [_decode(conn.execute("SELECT * FROM evidence_claims WHERE id = ?", (i,)).fetchone()) for i in claim_ids]
        hyps = [_decode(conn.execute("SELECT id, key, title, description FROM hypotheses WHERE id = ?", (h,)).fetchone())
                for h in hyp_ids]
        found = safety.current_brief(conn, inv["id"])
        extract = {(r["claim_id"], r["hypothesis_id"]): r["direction"] for r in conn.execute(
            f"SELECT claim_id, hypothesis_id, direction FROM claim_links WHERE origin = 'extract' AND claim_id IN "
            f"({','.join('?' * len(claim_ids))})", claim_ids)}
    claims = [c for c in claims if c["status"] == "active"]
    if not claims or found is None:
        return None
    ctx = tc.services.with_task(task_id=tc.task["id"], step="map", investigation_id=inv["id"])
    m = await ensemble.single(ctx, "map", MapBatch, llm.base_system() + "\n\n" + llm.prompt(MAP_VERSION),
                              map_material(case_summary(found[1], inv), hyps, claims), prompt_version=MAP_VERSION,
                              validate=map_validator(len(claims)))
    by_key = {h["key"]: h["id"] for h in hyps}
    links = {i.claim: i.links for i in m.value.items}  # type: ignore[union-attr]

    def commit(conn: sqlite3.Connection) -> None:
        counts = {"claims": len(claims), "links": 0, "neutral": 0, "disagree": 0, "unknown_keys": 0}
        for n, c in enumerate(claims, 1):
            for link in links.get(f"C{n}", []):
                hid = by_key.get(link.hypothesis_key)
                if hid is None:
                    counts["unknown_keys"] += 1
                    continue
                conn.execute("INSERT OR REPLACE INTO claim_links (claim_id, hypothesis_id, origin, direction, strength, "
                             "case_match, reason) VALUES (?, ?, 'mapping', ?, ?, ?, ?)",
                             (c["id"], hid, link.direction, link.strength, link.case_match, link.reason))
                counts["links"] += 1
                counts["neutral"] += link.direction == "neutral"
                old = extract.get((c["id"], hid))
                if old is not None and old != link.direction:
                    counts["disagree"] += 1  # logged; the mapping wins (B8.16)
            checks = _checks(c)
            checks["mapped"] = sorted(set(checks.get("mapped") or []) | set(hyp_ids))
            conn.execute("UPDATE evidence_claims SET checks = ? WHERE id = ?", (json.dumps(checks), c["id"]))
        tc.db.event(inv["id"], "map", tc.task["key"], stage="mapping", conn=conn, data=counts)
    return commit


def mapping_stage(next_stage: str = "grouping") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        for hyp_ids, claim_ids in unmapped(sc.conn, sc.id).items():
            for start in range(0, len(claim_ids), MAP_BATCH):
                batch = claim_ids[start:start + MAP_BATCH]
                sc.queue("map", _key("map", batch) + f":h{'-'.join(map(str, hyp_ids))}",
                         {"claim_ids": batch, "hypothesis_ids": list(hyp_ids)}, priority=90)
    return StageDef("mapping", on_enter=on_enter, on_complete=lambda sc: next_stage, skip_check_steps=("map",))


# ---------- group ----------

def norm_dataset(name: str | None) -> str:
    n = " ".join(re.findall(r"[a-z0-9]+", (name or "").casefold()))
    n = re.sub(r"\b(the|study|cohort|trial|dataset|data|registry)\b", " ", n)
    n = " ".join(n.split())
    return "" if n in GENERIC_DATASETS else n


def study_kind(design: str | None) -> str:
    return {"guideline": "guideline", "systematic_review": "review_sr",
            "narrative_review": "review_narrative"}.get(design or "", "primary")


def group_reads(reads: list[dict]) -> list[list[dict]]:
    """Union-find over reads by registry ID and normalised dataset (B8.17)."""
    parent = {r["id"]: r["id"] for r in reads}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    owner: dict[str, int] = {}
    for r in reads:
        ident = r.get("study_identity") or {}
        keys = [f"reg:{i['id']}" for i in ident.get("registry_ids") or []]
        ds = norm_dataset(((ident.get("dataset_or_cohort") or {}).get("value")))
        if ds:
            keys.append(f"ds:{ds}")
        for k in keys:
            if k in owner:
                parent[find(r["id"])] = find(owner[k])
            else:
                owner[k] = r["id"]
    groups: dict[int, list[dict]] = {}
    for r in reads:
        groups.setdefault(find(r["id"]), []).append(r)
    return list(groups.values())


async def run_group(tc) -> Any:
    inv = tc.investigation
    with tc.db.read() as conn:
        reads = [_decode(r) for r in conn.execute(
            "SELECT r.id, r.literature_id, r.study_identity, s.design, c.pmid, c.pmcid, c.doi FROM paper_reads r "
            "LEFT JOIN candidates c ON c.id = r.candidate_id LEFT JOIN screenings s ON s.candidate_id = r.candidate_id "
            "WHERE r.investigation_id = ? AND r.status = 'read' AND r.purpose IN ('research', 'both') ORDER BY r.id",
            (inv["id"],))]
    src = sources_for(tc.services)
    refs: dict[int, set[str]] = {}
    failed = 0
    for r in reads:
        if study_kind(r.get("design")) in ("review_sr", "review_narrative") and r.get("literature_id"):
            try:
                got = await src.load(int(r["literature_id"]))
            except Exception:  # noqa: BLE001 — inclusion is best effort; the review still counts
                failed += 1
                continue
            ids = set()
            for ref in (got.details.references if got.details else []) or []:
                ids |= {f"pmid:{ref.pmid}" if ref.pmid else "", f"pmcid:{ref.pmcid}" if ref.pmcid else "",
                        f"doi:{ref.doi.lower()}" if ref.doi else ""}
            refs[r["id"]] = ids - {""}

    def commit(conn: sqlite3.Connection) -> None:
        rnd = int(inv["round"])
        conn.execute("DELETE FROM studies WHERE investigation_id = ?", (inv["id"],))  # rebuilt every round
        study_of: dict[int, int] = {}
        kinds: dict[int, str] = {}
        for group in group_reads(reads):
            first = group[0]
            ident = [r.get("study_identity") or {} for r in group]
            registry = sorted({i["id"] for x in ident for i in x.get("registry_ids") or []})
            dataset = next((((x.get("dataset_or_cohort") or {}).get("value")) for x in ident
                            if norm_dataset(((x.get("dataset_or_cohort") or {}).get("value")))), None)
            kind = study_kind(first.get("design"))
            sid = tc.db.insert(conn, "studies", {"investigation_id": inv["id"], "kind": kind,
                                                 "label": dataset or (registry[0] if registry else f"paper r{first['id']}"),
                                                 "registry_ids": registry, "dataset": dataset, "round": rnd})
            kinds[sid] = kind
            for r in group:
                conn.execute("INSERT INTO study_members (study_id, read_id) VALUES (?, ?)", (sid, r["id"]))
                study_of[r["id"]] = sid
        inclusions = 0
        for review_read, ids in refs.items():
            review_sid = study_of[review_read]
            for r in reads:
                sid = study_of[r["id"]]
                if sid == review_sid or kinds[sid] != "primary":
                    continue
                keys = [("pmid", r.get("pmid")), ("pmcid", r.get("pmcid")), ("doi", (r.get("doi") or "").lower())]
                hit = next((k for k, v in keys if v and f"{k}:{v}" in ids), None)
                if hit:
                    conn.execute("INSERT OR IGNORE INTO review_inclusions (review_study_id, included_study_id, "
                                 "matched_by) VALUES (?, ?, ?)", (review_sid, sid, hit))
                    inclusions += 1
        tc.db.event(inv["id"], "group", f"r{rnd}", stage="grouping", conn=conn, data={
            "round": rnd, "papers": len(reads), "studies": len(set(study_of.values())), "inclusions": inclusions,
            "references_failed": failed})
    return commit


def grouping_stage(next_stage: str = "confirming") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        sc.queue("group", f"group:r{int(sc.inv['round'])}", {}, priority=90)
    return StageDef("grouping", on_enter=on_enter, on_complete=lambda sc: next_stage)


# ---------- confirm ----------

def _confirm_cfg(settings: dict) -> dict:
    return {"per_hypothesis": 5, "max_claims": 100, **((settings or {}).get("confirm") or {})}


def choose(prov: list[dict], per_hypothesis: int, max_claims: int, done: set[int]) -> list[int]:
    """Top supporting and contradicting claims per hypothesis by provisional weight, plus every `strong` link;
    at most `max_claims`, heaviest first. Claims confirmed before are skipped."""
    best: dict[int, float] = {}
    by_h: dict[tuple[int, str], list[dict]] = {}
    for p in prov:
        if p["claim_id"] in done:
            continue
        by_h.setdefault((p["hypothesis_id"], p["direction"]), []).append(p)
        if p["strength"] == "strong":
            best[p["claim_id"]] = max(best.get(p["claim_id"], 0), p["weight"])
    for group in by_h.values():
        for p in sorted(group, key=lambda x: (-x["weight"], x["claim_id"]))[:per_hypothesis]:
            best[p["claim_id"]] = max(best.get(p["claim_id"], 0), p["weight"])
    return [cid for cid, _ in sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))][:max_claims]


def neighbours_text(snap: Snapshot, ids: list[str], limit: int = 16000) -> str:
    parts_in_order = list(dict.fromkeys(part_of(s.seg_id) for s in snap.segments))
    wanted = set()
    for p in {part_of(i) for i in ids}:
        if p in parts_in_order:
            i = parts_in_order.index(p)
            wanted |= set(parts_in_order[max(0, i - 1):i + 2])
    return " ".join(s.text for s in snap.segments if part_of(s.seg_id) in wanted)[:limit]


async def run_confirm(tc) -> Any:
    inv = tc.investigation
    ids = [int(i) for i in tc.payload["claim_ids"]]
    family = tc.payload["family"]
    with tc.db.read() as conn:
        claims = [_decode(r) for r in conn.execute(
            f"SELECT * FROM evidence_claims WHERE status = 'active' AND id IN ({','.join('?' * len(ids))}) ORDER BY id",
            ids)]
        reads = {r: VF.load_read(conn, r) for r in {c["read_id"] for c in claims}}
    if not claims:
        return None
    lines = []
    for n, c in enumerate(claims, 1):
        row, snap = reads[c["read_id"]]
        prof = {k: (v or {}).get("value") for k, v in (row.get("profile") or {}).items() if (v or {}).get("value")}
        lines += [f"C{n}: {VF.claim_text(c)}", f"  Text: {c['quote']}", f"  Study profile: {json.dumps(prof)}",
                  f"  Sections: {neighbours_text(snap, c['sentence_ids'])}", ""]
    wanted = {f"C{n}" for n in range(1, len(claims) + 1)}

    def check(b: ClaimVoteBatch) -> None:
        if {i.pair for i in b.items} != wanted:
            raise ValueError("one item per claim is required")
    ctx = tc.services.with_task(task_id=tc.task["id"], step="confirm", investigation_id=inv["id"])
    member = await ensemble.adjudicate(ctx, ClaimVoteBatch, llm.base_system() + "\n\n" + llm.prompt(CONFIRM_VERSION),
                                       llm.material("\n".join(lines).strip(), kind="claim_confirm"),
                                       extractor_family=family, prompt_version=CONFIRM_VERSION, role="verify",
                                       validate=check)
    votes = {v.pair: v for v in member.value.items}  # type: ignore[union-attr]

    def commit(conn: sqlite3.Connection) -> None:
        counts = {"claims": len(claims), "rejected": 0, "downgraded": 0}
        for n, c in enumerate(claims, 1):
            v = votes[f"C{n}"]
            got = V.claim_rule(v)
            final = max([c["verification"] or "supported", got], key=VERDICT_ORDER.index)  # the most cautious
            checks = {k: getattr(v, k) for k in ("verdict", "asserts_effect", "direction_check", "significance",
                                                 "population_check", "overgeneralization", "context_needed")}
            conn.execute("INSERT INTO claim_votes (claim_id, stage, model, verdict, checks, reason, created_at) "
                         "VALUES (?, 'confirm', ?, ?, ?, ?, ?)", (c["id"], member.model.model, got,
                                                                  json.dumps(checks), v.reason[:300], now()))
            status = "rejected" if final == "unsupported" else "active"
            counts["rejected"] += status == "rejected"
            counts["downgraded"] += status == "active" and final != c["verification"]
            conn.execute("UPDATE evidence_claims SET confirmation = ?, consequential = 1, status = ?, verification = ? "
                         "WHERE id = ?", (got, status, None if status == "rejected" else final, c["id"]))
        tc.db.event(inv["id"], "confirm", tc.task["key"], stage="confirming", conn=conn, data=counts)
    return commit


def confirming_stage(next_stage: str = "matrix") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        cfg = _confirm_cfg(sc.settings)
        done = {r["id"] for r in sc.conn.execute("SELECT id FROM evidence_claims WHERE investigation_id = ? AND "
                                                 "confirmation IS NOT NULL", (sc.id,))}
        chosen = choose(W.provisional(sc.conn, sc.id, sc.settings), int(cfg["per_hypothesis"]),
                        int(cfg["max_claims"]), done)
        if not chosen:
            return
        fam = {r["id"]: _checks(dict(r)).get("extractor_family") or "openai" for r in sc.conn.execute(
            f"SELECT id, checks FROM evidence_claims WHERE id IN ({','.join('?' * len(chosen))})", chosen)}
        by_family: dict[str, list[int]] = {}
        for cid in chosen:
            by_family.setdefault(fam[cid], []).append(cid)
        for family, ids in by_family.items():
            for start in range(0, len(ids), CONFIRM_BATCH):
                batch = ids[start:start + CONFIRM_BATCH]
                sc.queue("confirm", _key("confirm", batch), {"claim_ids": batch, "family": family}, priority=90)
    return StageDef("confirming", on_enter=on_enter, on_complete=lambda sc: next_stage,
                    skip_check_steps=("confirm",))


MAP_STEP = StepDef("map", run_map, on_fail="skip")
GROUP_STEP = StepDef("group", run_group)
CONFIRM_STEP = StepDef("confirm", run_confirm, on_fail="skip")
