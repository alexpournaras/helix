"""read (PLAN B8.14): one task per fetched paper, then `verifying` (1E.2).

read:{candidate id}
  snapshot  the library copy (`sources.load`), or the snapshot grounding already made for the same paper (the
            read is then shared: purpose `both`); packs of `extraction.max_pack_tokens` in document order;
  stage A   profile (`read.profile`, one model) and the appraisal panel (`read.appraisal`; criteria by design,
            the higher overall risk and the more cautious rating per criterion win), on the first pack; study
            identity = registry IDs found by regex over the whole snapshot + the profile's dataset and
            secondary-analysis fields;
  stage B   dual extraction per pack (`read.claims`; `ensembles.dual_extraction` off → one), then the
            whole-paper pass (`read.whole_paper`, IDs from anywhere in the paper) when enabled;
            code checks (claims.py): IDs shown, quotes by code, effect numbers and n in the cited text, unknown
            hypothesis keys dropped; a claim without a valid ID is rejected (rejected_items);
  reconcile claims of different extractors matched by ID overlap + token Jaccard; borderline pairs go to
            `read.reconcile`; a cluster keeps the claim citing fewer sentences (agreement 2), the rest are
            `superseded` members;
  stage C   applicability (`read.applicability`); a special-population mismatch caps it at `low`, a partial fit
            at `partial`.
Claims are saved `pending` (verification is 1E.2) with their extraction links.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import claims as C, ensemble, llm
from ..db import _decode, now
from ..packing import pack_snapshot
from ..pipeline import StageContext, StageDef, StepDef
from ..schemas import Appraisal, Applicability, ClaimSet, PaperProfile, SameClaims
from ..segment import Snapshot, paper_snapshot
from ..sources import sources_for
from . import safety
from .screen import case_summary

PROFILE_VERSION = "read.profile.v1"
APPRAISAL_VERSION = "read.appraisal.v1"
CLAIMS_VERSION = "read.claims.v1"
WHOLE_VERSION = "read.whole_paper.v1"
RECONCILE_VERSION = "read.reconcile.v1"
APPLICABILITY_VERSION = "read.applicability.v1"

CRITERIA = {
    "rct": ["randomisation", "allocation_concealment", "blinding", "outcome_data_completeness", "selective_reporting"],
    "cohort": ["selection", "exposure_measurement", "confounding", "follow_up", "outcome_measurement"],
    "case_control": ["selection", "exposure_measurement", "confounding", "follow_up", "outcome_measurement"],
    "cross_sectional": ["sampling", "measurement_validity", "confounding", "response_rate"],
    "systematic_review": ["search", "inclusion", "risk_of_bias_assessment", "heterogeneity"],
    "guideline": ["development_rigour", "evidence_linkage", "currency", "conflicts"],
    "other": ["source_clarity", "case_selection", "conflicts"],
}
RISK_ORDER = ["high", "unclear", "some", "low"]  # most cautious first
FIT_ORDER = ["low", "partial", "high"]


def criteria_for(design: str | None) -> list[str]:
    return CRITERIA.get(design or "other", CRITERIA["other"])


def _cfg(settings: dict) -> dict:
    return {"max_pack_tokens": 32000, "max_packs_per_paper": None, "max_claims_per_pack": 20,
            **((settings or {}).get("extraction") or {})}


def _ens(settings: dict) -> dict:
    return {"dual_extraction": True, "whole_paper_pass": True, "appraisal_panel": True,
            **((settings or {}).get("ensembles") or {})}


# ---------- materials ----------

def hypotheses_text(hyps: list[dict]) -> str:
    return "\n".join(f"- {h['key']}: {h['title']}" + (f" — {h['description']}" if h.get("description") else "")
                     for h in hyps)


def claims_material(summary: dict, hyps: list[dict], design: str, text: str, limit: int, kind: str,
                    existing: list[str] | None = None) -> str:
    parts = [f"Case: {json.dumps(summary, ensure_ascii=False)}", "", "Hypotheses:", hypotheses_text(hyps), "",
             f"Paper design: {design}", f"Maximum claims: {limit}"]
    if existing is not None:
        parts += ["", "Claims already extracted:", *([f"- {c}" for c in existing] or ["(none)"])]
    parts += ["", "Paper:", text]
    return llm.material("\n".join(parts), kind=kind)


def appraisal_material(design: str, text: str) -> str:
    return llm.material(f"Design: {design}\nCriteria: {', '.join(criteria_for(design))}\n\nPaper:\n{text}",
                        kind="appraisal")


def applicability_material(summary: dict, brief, profile: PaperProfile, design: str) -> str:
    case = {**summary, "age": brief.demographics.age.text if brief.demographics.age else None,
            "sex": brief.demographics.sex.value if brief.demographics.sex else None}
    paper = {k: getattr(profile, k).value for k in ("population", "setting", "inclusion_criteria")}
    return llm.material(json.dumps({"case": case, "paper": {**paper, "design": design}}, ensure_ascii=False, indent=1),
                        kind="applicability")


# ---------- combining ----------

def section_of(snapshot: Snapshot, seg_id: str) -> str | None:
    """The section title of a sentence or cell, or of a table row's first cell."""
    if seg_id in snapshot._index:
        return snapshot.get(seg_id).section
    cells = snapshot.row_cells(seg_id)
    return cells[0].section if cells else None


def clean_ids(ids: list[str], snapshot: Snapshot) -> list[str]:
    return [i for i in ids if i in snapshot]


def combine_appraisals(values: list[Appraisal], design: str, snapshot: Snapshot) -> dict:
    """The higher overall risk wins; for each criterion the more cautious rating (B8.14)."""
    names = criteria_for(design)
    out = {"design": design, "criteria": [], "overall_risk": "unclear"}
    if not values:
        return out
    out["overall_risk"] = min((v.overall_risk for v in values), key=RISK_ORDER.index)
    for name in names:
        rated = [c for v in values for c in v.criteria if c.criterion == name]
        if not rated:
            out["criteria"].append({"criterion": name, "rating": "unclear", "reason": "not rated", "sentence_ids": []})
            continue
        worst = min(rated, key=lambda c: RISK_ORDER.index(c.rating))
        out["criteria"].append({"criterion": name, "rating": worst.rating, "reason": worst.reason,
                                "sentence_ids": clean_ids(worst.sentence_ids, snapshot)})
    if any(c["rating"] == "high" for c in out["criteria"]):
        out["overall_risk"] = "high"
    return out


def applicability_rule(a: Applicability, populations: list[str]) -> dict:
    d = a.model_dump(mode="json")
    if not populations:
        d["special_population_fit"] = "not_applicable"
    cap = {"mismatch": "low", "partial": "partial"}.get(d["special_population_fit"])
    if cap and FIT_ORDER.index(d["overall"]) > FIT_ORDER.index(cap):
        d["overall"] = cap
        d["capped_by"] = "special_population"
    return d


def appraisal_validator(design: str):
    names = set(criteria_for(design))

    def check(a: Appraisal) -> None:
        got = {c.criterion for c in a.criteria}
        missing = sorted(names - got)
        if missing:
            raise ValueError(f"rate every criterion: missing {', '.join(missing)}")
    return check


def claims_validator(limit: int):
    def check(cs: ClaimSet) -> None:
        if len(cs.claims) > limit:
            raise ValueError(f"at most {limit} claims")
    return check


# ---------- extraction (also used by the re-read, 1E.2) ----------

def _system(version: str) -> str:
    return llm.base_system() + "\n\n" + llm.prompt(version)


@dataclass
class Extraction:
    checked: list  # [(extractor label, model ref, pack no, pass, claims.Checked)]
    rejected: list  # [(raw claim JSON, reason, extractor label)]
    clusters: list  # [claims.Cluster]
    borderline: int = 0


async def extract_claims(ctx, *, snap: Snapshot, packs: list, summary: dict, hyps: list[dict], design: str,
                         limit: int, profile_n: str | None, dual: bool, whole: bool,
                         version: str = CLAIMS_VERSION) -> Extraction:
    """Stage B and reconciliation: dual extraction per pack, the whole-paper pass, code checks, the judge for
    borderline pairs."""
    keys = {h["key"] for h in hyps}

    async def per_pack(pack):
        text = claims_material(summary, hyps, design, pack.text, limit, "paper_part")
        if dual:
            r = await ensemble.dual(ctx, "read.claims", ClaimSet, _system(version), text, prompt_version=version,
                                    validate=claims_validator(limit))
            return [(f"m{i}", m.model, m.value) for i, m in enumerate(r.members) if m.ok]
        m = await ensemble.single(ctx, "read.claims", ClaimSet, _system(version), text, prompt_version=version,
                                  validate=claims_validator(limit))
        return [("m0", m.model, m.value)]
    results = await asyncio.gather(*[per_pack(p) for p in packs])
    checked: list = []
    rejected: list = []
    for pack, members in zip(packs, results):
        for label, ref, value in members:
            for claim in value.claims:
                ok, why = C.check_claim(claim, allowed=pack.seg_ids, snapshot=snap, profile_n=profile_n,
                                        hypothesis_keys=keys)
                if ok is None:
                    rejected.append((claim.model_dump_json(), why, label))
                else:
                    checked.append((label, ref, pack.no, "pack", ok))
    if whole:
        text = "\n\n".join(p.text for p in packs)
        m = await ensemble.single(ctx, "read.whole_paper", ClaimSet, _system(WHOLE_VERSION),
                                  claims_material(summary, hyps, design, text, limit, "paper",
                                                  existing=[c[4].claim.claim for c in checked]),
                                  prompt_version=WHOLE_VERSION, validate=claims_validator(limit))
        for claim in m.value.claims:  # type: ignore[union-attr]
            ok, why = C.check_claim(claim, allowed=None, snapshot=snap, profile_n=profile_n, hypothesis_keys=keys)
            if ok is None:
                rejected.append((claim.model_dump_json(), why, "whole"))
            else:
                checked.append(("whole", m.model, None, "whole", ok))
    pairs_in = [(c[0], c[4]) for c in checked]
    clusters, unjudged = C.reconcile(pairs_in)
    if unjudged:
        lines = [f"M{n}: A: {pairs_in[i][1].claim.claim}\n    B: {pairs_in[j][1].claim.claim}"
                 for n, (i, j) in enumerate(unjudged, 1)]
        wanted = {f"M{n}" for n in range(1, len(unjudged) + 1)}

        def check_pairs(s: SameClaims) -> None:
            if {p.pair for p in s.pairs} != wanted:
                raise ValueError("one entry per pair is required")
        m = await ensemble.single(ctx, "read.reconcile", SameClaims, _system(RECONCILE_VERSION),
                                  llm.material("\n".join(lines), kind="claim_pairs"),
                                  prompt_version=RECONCILE_VERSION, validate=check_pairs)
        answers = {p.pair: p.same for p in m.value.pairs}  # type: ignore[union-attr]
        clusters, _ = C.reconcile(pairs_in, {pair: answers[f"M{n}"] for n, pair in enumerate(unjudged, 1)})
    return Extraction(checked, rejected, clusters, len(unjudged))


def save_claims(db, conn: sqlite3.Connection, investigation_id: int, read_id: int, literature_id: int | None,
                rnd: int, ext: Extraction, snap: Snapshot, by_key: dict[str, int], *, pass_suffix: str = "") -> dict:
    """Write the clusters: kept claims `pending` (agreement 1 or 2), members `superseded`, with extraction links;
    rejected claims go to rejected_items. Returns counts and the kept claim IDs (`ids`)."""
    meta = {id(c[4]): c for c in ext.checked}
    counts: dict = {"claims": 0, "agreement_2": 0, "superseded": 0, "ids": []}

    def save(entry, status: str, agreement: int | None, cluster: int | None) -> int:
        label, ref, pack_no, pass_, ck = meta[id(entry[1])]
        c = ck.claim
        claim_id = db.insert(conn, "evidence_claims", {
            "investigation_id": investigation_id, "read_id": read_id, "literature_id": literature_id,
            "pass": pass_ + pass_suffix, "extractor_model": ref.model, "cluster_id": cluster, "agreement": agreement,
            "pack_no": pack_no, "claim": c.claim, "sentence_ids": ck.sentence_ids, "quote": ck.quote,
            "section_title": section_of(snap, ck.sentence_ids[0]),
            "evidence_type": c.evidence_type, "population": c.population, "design": c.design, "n": c.n,
            "effect": c.effect.model_dump(mode="json") if c.effect else None, "limitations": c.limitations,
            "status": status, "checks": {**ck.checks, "extractor": label, "extractor_family": ref.family},
            "round": rnd})
        for link in c.links:
            conn.execute("INSERT OR IGNORE INTO claim_links (claim_id, hypothesis_id, origin, direction, strength, "
                         "case_match, reason) VALUES (?, ?, 'extract', ?, ?, ?, ?)",
                         (claim_id, by_key[link.hypothesis_key], link.direction, link.strength, link.case_match,
                          link.reason))
        return claim_id
    for cl in ext.clusters:
        kept = save(cl.kept, "pending", cl.agreement, None)
        conn.execute("UPDATE evidence_claims SET cluster_id = ? WHERE id = ?", (kept, kept))
        counts["claims"] += 1
        counts["agreement_2"] += cl.agreement == 2
        counts["ids"].append(kept)
        for member in cl.members:
            save(member, "superseded", None, kept)
            counts["superseded"] += 1
    for raw, why, _label in ext.rejected:
        db.insert(conn, "rejected_items", {"investigation_id": investigation_id, "step": "read", "ref": f"r{read_id}",
                                           "raw": raw, "reason": why, "created_at": now()})
    return counts


# ---------- the step ----------

async def run(tc) -> Any:
    inv = tc.investigation
    cid = int(tc.payload["candidate_id"])
    with tc.db.read() as conn:
        cand = _decode(conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone())
        scr = _decode(conn.execute("SELECT design FROM screenings WHERE candidate_id = ?", (cid,)).fetchone() or {})
        hyps = [_decode(r) for r in conn.execute("SELECT id, key, title, description FROM hypotheses WHERE "
                                                 "investigation_id = ? AND status = 'active' ORDER BY id", (inv["id"],))]
        found = safety.current_brief(conn, inv["id"])
        shared = conn.execute("SELECT id, segments FROM paper_reads WHERE investigation_id = ? AND literature_id = ? "
                              "AND segments IS NOT NULL ORDER BY id LIMIT 1",
                              (inv["id"], cand["literature_id"])).fetchone() if cand["literature_id"] else None
    if found is None:
        raise RuntimeError("reading needs a case brief")
    brief = found[1]
    if shared is not None:
        snap = Snapshot.from_dict(_decode(shared)["segments"])
        fulltext_source = None
    else:
        fetched = await sources_for(tc.services).load(int(cand["literature_id"]))
        snap = paper_snapshot(fetched.article.abstract or cand["abstract"], fetched.details)
        fulltext_source = (fetched.details.full_text_from if fetched.details else "") or None
    cfg, ens = _cfg(tc.settings), _ens(tc.settings)
    packed = pack_snapshot(snap, int(cfg["max_pack_tokens"]), cfg.get("max_packs_per_paper"))
    packs = packed.packs
    if not packs:
        raise RuntimeError("the paper has no text to read")
    design = scr.get("design") or "other"
    limit = int(cfg["max_claims_per_pack"])
    summary = case_summary(brief, inv)
    pops = inv.get("special_populations") or []
    keys = {h["key"] for h in hyps}
    ctx = tc.services.with_task(task_id=tc.task["id"], step="read", investigation_id=inv["id"])

    async def profile():
        m = await ensemble.single(ctx, "read.profile", PaperProfile, _system(PROFILE_VERSION),
                                  llm.material(packs[0].text, kind="paper"), prompt_version=PROFILE_VERSION)
        return m.value

    async def appraisal():
        text = appraisal_material(design, packs[0].text)
        check = appraisal_validator(design)
        if ens["appraisal_panel"]:
            r = await ensemble.panel(ctx, "read.appraisal", Appraisal, _system(APPRAISAL_VERSION), text,
                                     prompt_version=APPRAISAL_VERSION, validate=check)
            return r.votes(), r.values
        m = await ensemble.single(ctx, "read.appraisal", Appraisal, _system(APPRAISAL_VERSION), text,
                                  prompt_version=APPRAISAL_VERSION, validate=check)
        return [{"model": m.model.model, "family": m.model.family, "ok": True,
                 "value": m.value.model_dump(mode="json")}], [m.value]

    prof, (appraisal_votes, appraisal_values) = await asyncio.gather(profile(), appraisal())
    ext = await extract_claims(ctx, snap=snap, packs=packs, summary=summary, hyps=hyps, design=design, limit=limit,
                               profile_n=prof.n.value, dual=ens["dual_extraction"], whole=ens["whole_paper_pass"])
    m = await ensemble.single(ctx, "read.applicability", Applicability, _system(APPLICABILITY_VERSION),
                              applicability_material(summary, brief, prof, design),
                              prompt_version=APPLICABILITY_VERSION)
    applicability = applicability_rule(m.value, pops)  # type: ignore[arg-type]
    appraisal_combined = combine_appraisals(appraisal_values, design, snap)
    profile_d = {k: {"value": f.value, "sentence_ids": clean_ids(f.sentence_ids, snap)}
                 for k, f in prof}  # type: ignore[union-attr]
    identity = {"registry_ids": C.registry_ids(snap), "dataset_or_cohort": profile_d["dataset_or_cohort"],
                "secondary_analysis": profile_d["secondary_analysis"]}
    by_key = {h["key"]: h["id"] for h in hyps}

    def commit(conn: sqlite3.Connection) -> None:
        rnd = int(inv["round"])
        values = {"profile": json.dumps(profile_d), "appraisal_votes": json.dumps(appraisal_votes),
                  "appraisal": json.dumps(appraisal_combined), "study_identity": json.dumps(identity),
                  "applicability": json.dumps(applicability), "status": "read"}
        if shared is not None:
            read_id = shared["id"]
            conn.execute(f"UPDATE paper_reads SET purpose = 'both', candidate_id = COALESCE(candidate_id, ?), "
                         f"{', '.join(f'{k} = ?' for k in values)} WHERE id = ?", (cid, *values.values(), read_id))
        else:
            read_id = tc.db.insert(conn, "paper_reads", {
                "investigation_id": inv["id"], "candidate_id": cid, "literature_id": cand["literature_id"],
                "round": rnd, "purpose": "research", "scope": packed.coverage, "fulltext_source": fulltext_source,
                "parser": "jats" if snap.has_body else None, "skipped_sections": snap.skipped_sections,
                "segments": snap.to_dict(), "segments_hash": snap.hash,
                "packs": [p.summary() for p in packs], "profile": profile_d, "appraisal_votes": appraisal_votes,
                "appraisal": appraisal_combined, "study_identity": identity, "applicability": applicability,
                "status": "read"})
        counts = save_claims(tc.db, conn, inv["id"], read_id, cand["literature_id"], rnd, ext, snap, by_key)
        conn.execute("UPDATE candidates SET status = 'read' WHERE id = ?", (cid,))
        tc.db.event(inv["id"], "read", f"c{cid}", stage="reading", conn=conn, data={
            "candidate_id": cid, "read_id": read_id, "packs": len(packs), "scope": packed.coverage,
            "shared_with_grounding": shared is not None, "rejected": len(ext.rejected), "borderline": ext.borderline,
            "overall_risk": appraisal_combined["overall_risk"], "applicability": applicability["overall"],
            **{k: v for k, v in counts.items() if k != "ids"}})
    return commit


def stage(next_stage: str = "verifying") -> StageDef:
    def on_enter(sc: StageContext) -> None:
        for r in sc.conn.execute("SELECT id FROM candidates WHERE investigation_id = ? AND purpose = 'research' AND "
                                 "status = 'fetched' ORDER BY id", (sc.id,)):
            sc.queue("read", f"read:{r['id']}", {"candidate_id": r["id"]}, priority=80)

    # One paper failing is skipped (B10); more than half skipped fails the investigation.
    return StageDef("reading", on_enter=on_enter, on_complete=lambda sc: next_stage, skip_check_steps=("read",))


STEP = StepDef("read", run, on_fail="skip")
