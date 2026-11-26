"""Verification: panel → escalate → adjudicate (PLAN B8.15), for knowledge attestations (B8.3a step 6) and
claims (1E.2). The vote schema and its code rule are parameters: `rule_verdict` for attestations, `claim_rule`
for claims.

Code rules applied to each vote: a different population → unsupported; a broader population or an
overgeneralisation → at most partial. Claims also: the opposite direction → unsupported; an effect claimed where
the result isn't significant → unsupported. Combining: all supported → supported; all unsupported → unsupported;
anything else (or `context_needed`) → escalate with more context; still unresolved → adjudicate by the strongest
model of the family that didn't extract (its verdict is final). Every vote is kept.
"""

from __future__ import annotations

import asyncio
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
