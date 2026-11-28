"""Verification: panel → escalate → adjudicate (PLAN B8.15), for knowledge attestations (B8.3a step 6) and
claims (1E.2). The vote schema and its code rule are parameters: `rule_verdict` for attestations, `claim_rule`
for claims.

Code rules applied to each vote: a different population → unsupported; a broader population or an
overgeneralisation → at most partial. Claims also: the opposite direction → unsupported; an effect claimed where
the result isn't significant → unsupported. Combining: all supported → supported; all unsupported → unsupported;
anything else (or `context_needed`) → escalate with more context; still unresolved → adjudicate by the strongest
model of the family that didn't extract (its verdict is final). Every vote is kept.

Single-verifier mode (`ensembles.verify_panel: 1`, the budget profile, PLAN B17.5 item 5): one verifier checks every
pair; `verify.second` checks a pair again (with context) only when a trigger in `verification.second_opinion`
holds — the first vote is unsure (`partial`, `context_needed`, or no answer), or the caller marked the pair
(`code_flag`, `strong_link`, `causal`, `red_flag`, `recommendation`) — plus a seeded random audit of
`verification.audit_rate` of the rest. The two agree → that verdict; they disagree → adjudicate. Vote stages:
`verify`, `second`, `audit`, `adjudicate`.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from . import ensemble, llm
from .schemas import AttestationBatch, AttestationVote, ClaimVote

BATCH = 10


def rule_verdict(vote: AttestationVote) -> str:
    if vote.population_check == "different":
        return "unsupported"
    if vote.verdict == "supported" and (vote.population_check == "broader" or vote.overgeneralization):
        return "partial"
    return vote.verdict


def claim_rule(vote: ClaimVote) -> str:
    """B8.15 for claims: opposite direction, an effect that isn't significant, or a different population →
    unsupported; a broader population or an overgeneralisation → at most partial."""
    if vote.direction_check == "opposite":
        return "unsupported"
    if vote.asserts_effect and vote.significance == "not_significant":
        return "unsupported"
    return rule_verdict(vote)  # type: ignore[arg-type]


def combine(votes: Sequence[Any], rule: Callable[[Any], str] = rule_verdict) -> str | None:
    """The combined verdict, or None when the panel must escalate."""
    if not votes or any(v.context_needed for v in votes):
        return None
    verdicts = {rule(v) for v in votes}
    if verdicts == {"supported"}:
        return "supported"
    if verdicts == {"unsupported"}:
        return "unsupported"
    return None


@dataclass
class Pair:
    """One thing to verify: `text` (the item or claim) against `quote`, with `context` for escalation."""
    pair_id: str
    text: str
    quote: str
    context: str
    extractor_model: str
    extractor_family: str
    verdict: str | None = None
    votes: list[dict] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)  # second-opinion triggers the caller found (single mode)
    seed: str = ""  # stable text for the random audit


def _validator(ids: list[str]):
    wanted = set(ids)

    def check(batch: AttestationBatch) -> None:
        got = [i.pair for i in batch.items]
        if set(got) != wanted or len(got) != len(wanted):
            missing = sorted(wanted - set(got))
            raise ValueError(f"one item per pair is required (missing: {', '.join(missing[:10]) or 'none'})")
    return check


def _votes(result_members, stage: str, rule=rule_verdict) -> dict[str, list[tuple[dict, Any]]]:
    out: dict[str, list[tuple[dict, AttestationVote]]] = {}
    for m in result_members:
        if not m.ok:
            continue
        for v in m.value.items:
            rec = {"stage": stage, "model": m.model.model, "family": m.model.family,
                   **v.model_dump(mode="json"), "rule_verdict": rule(v)}
            out.setdefault(v.pair, []).append((rec, v))
    return out


async def verify(ctx: llm.CallContext, role: str, pairs: list[Pair], *, system: str, prompt_version: str,
                 render: Callable[[list[Pair], bool], str], schema: type = AttestationBatch,
                 rule: Callable[[Any], str] = rule_verdict,
                 render_adjudicate: Callable[[list[Pair]], str] | None = None) -> list[Pair]:
    """Fill `verdict` and `votes` on every pair. `render(pairs, with_context)` builds the panel material;
    `render_adjudicate(pairs)` the adjudicator's (default: `render(pairs, True)`)."""
    opts = {"schema": schema, "rule": rule, "render_adjudicate": render_adjudicate or (lambda ps: render(ps, True))}
    if int(getattr(ctx.settings.ensembles, "verify_panel", 2)) == 1:
        await _verify_single(ctx, role, pairs, system, prompt_version, render, **opts)
        return pairs
    by_model: dict[str, list[Pair]] = {}
    for p in pairs:
        by_model.setdefault(p.extractor_model, []).append(p)
    # Batches run in parallel (the provider limiter caps concurrency).
    await asyncio.gather(*[_verify_batch(ctx, role, group[start:start + BATCH], model, system, prompt_version, render,
                                         **opts)
                           for model, group in by_model.items() for start in range(0, len(group), BATCH)])
    return pairs


async def _verify_batch(ctx, role, batch: list[Pair], extractor_model: str, system: str, prompt_version: str,
                        render, *, schema, rule, render_adjudicate) -> None:
    open_pairs = batch
    for stage, with_context in (("verify", False), ("escalate", True)):
        if not open_pairs:
            return
        result = await ensemble.panel(ctx, role, schema, system, render(open_pairs, with_context),
                                      prompt_version=prompt_version, validate=_validator([p.pair_id for p in open_pairs]),
                                      avoid_models=[extractor_model])
        votes = _votes(result.members, stage, rule)
        still = []
        for p in open_pairs:
            got = votes.get(p.pair_id, [])
            p.votes += [r for r, _ in got]
            p.verdict = combine([v for _, v in got], rule)
            if p.verdict is None:
                still.append(p)
        open_pairs = still
    by_family: dict[str, list[Pair]] = {}
    for p in open_pairs:
        by_family.setdefault(p.extractor_family, []).append(p)
    for family, group in by_family.items():
        member = await ensemble.adjudicate(ctx, schema, system, render_adjudicate(group),
                                           extractor_family=family, prompt_version=prompt_version,
                                           role=role, validate=_validator([p.pair_id for p in group]))
        for v in member.value.items:  # type: ignore[union-attr]
            p = next(x for x in group if x.pair_id == v.pair)
            p.votes.append({"stage": "adjudicate", "model": member.model.model, "family": member.model.family,
                            **v.model_dump(mode="json"), "rule_verdict": rule(v)})
            p.verdict = rule(v)


# ---------- single-verifier mode (PLAN B17.5 item 5) ----------

def audit_pick(seed: str, rate: float) -> bool:
    """A stable pseudo-random choice: the same pair in the same investigation is always (not) audited."""
    if rate <= 0:
        return False
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def needs_second(vote: Any, verdict: str | None, triggers: list[str], allowed: set[str]) -> bool:
    unsure = vote is None or getattr(vote, "context_needed", False) or verdict not in ("supported", "unsupported")
    return (unsure and "unsure" in allowed) or any(t in allowed for t in triggers)


async def _verify_single(ctx, role, pairs: list[Pair], system: str, prompt_version: str, render, *, schema, rule,
                         render_adjudicate) -> None:
    cfg = getattr(ctx.settings, "verification", None) or {}
    size = int(cfg.get("batch_size") or BATCH)
    allowed = set(cfg.get("second_opinion") or [])
    rate = float(cfg.get("audit_rate") or 0)
    second_role = "verify.second" if "verify.second" in ctx.settings.roles else role

    async def first(batch: list[Pair]) -> None:
        m = await ensemble.single(ctx, role, schema, system, render(batch, False), prompt_version=prompt_version,
                                  validate=_validator([p.pair_id for p in batch]))
        got = _votes([m], "verify", rule)
        for p in batch:
            rec = got.get(p.pair_id, [])
            p.votes += [r for r, _ in rec]
            vote = rec[0][1] if rec else None
            p.verdict = rule(vote) if vote is not None else None
            p._first = vote  # type: ignore[attr-defined]
    await asyncio.gather(*[first(pairs[i:i + size]) for i in range(0, len(pairs), size)])

    again: list[tuple[Pair, str]] = []
    for p in pairs:
        vote = getattr(p, "_first", None)
        if needs_second(vote, p.verdict, p.triggers, allowed):
            again.append((p, "second"))
        elif audit_pick(f"{ctx.investigation_id}:{p.seed or p.text}:{p.quote}", rate):
            again.append((p, "audit"))
    disputed: list[Pair] = []

    async def second(batch: list[tuple[Pair, str]]) -> None:
        group = [p for p, _ in batch]
        m = await ensemble.single(ctx, second_role, schema, system, render(group, True),
                                  prompt_version=prompt_version, validate=_validator([p.pair_id for p in group]))
        for p, stage in batch:
            rec = _votes([m], stage, rule).get(p.pair_id, [])
            p.votes += [r for r, _ in rec]
            vote = rec[0][1] if rec else None
            verdict = rule(vote) if vote is not None else None
            clear = verdict in ("supported", "unsupported", "partial") and not getattr(vote, "context_needed", False)
            if getattr(p, "_first", None) is None:
                if clear:
                    p.verdict = verdict  # the first verifier gave no answer for it; a clear second one stands
                    continue
            elif clear and verdict == p.verdict:
                continue  # the two agree
            disputed.append(p)
    await asyncio.gather(*[second(again[i:i + size]) for i in range(0, len(again), size)])

    by_family: dict[str, list[Pair]] = {}
    for p in disputed:
        by_family.setdefault(p.extractor_family, []).append(p)
    for family, group in by_family.items():
        member = await ensemble.adjudicate(ctx, schema, system, render_adjudicate(group), extractor_family=family,
                                           prompt_version=prompt_version, role=role,
                                           validate=_validator([p.pair_id for p in group]))
        for v in member.value.items:  # type: ignore[union-attr]
            p = next(x for x in group if x.pair_id == v.pair)
            p.votes.append({"stage": "adjudicate", "model": member.model.model, "family": member.model.family,
                            **v.model_dump(mode="json"), "rule_verdict": rule(v)})
            p.verdict = rule(v)
    for p in pairs:
        if p.verdict is None:
            p.verdict = "unsupported"  # no usable vote at all: not kept (the cautious end)
        if hasattr(p, "_first"):
            del p._first  # type: ignore[attr-defined]


def verification_mode(votes: list[dict]) -> str:
    stages = {v.get("stage") for v in votes}
    if "adjudicate" in stages:
        return "adjudicated"
    if "audit" in stages:
        return "audit"
    if "second" in stages:
        return "second"
    if sum(1 for v in votes if v.get("stage") == "verify") >= 2 or "escalate" in stages:
        return "panel"
    return "single"
