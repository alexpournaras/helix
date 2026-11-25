"""Merging curated and guideline knowledge (PLAN B7.4, 1B.3).

- Duplicates: same kind and population, and the same normalised name or the same MeSH descriptor. Near names
  (token Jaccard ≥ BORDERLINE_JACCARD) go to an optional judge (A astra in the pipeline); without one they stay apart.
- Trust: `curated` (reviewed pack or floor) > `verified` (≥ 1 supported attestation) > `partial` (only partially
  supported) > `proposed` (no attestation). Unsupported attestations are dropped; a guideline item left with
  none is rejected.
- Disagreements: duplicates whose attributes conflict (e.g. different age thresholds) are kept side by side with
  their own sources, and both carry the same `disagreement` key for the report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

TRUST_RANK = {"curated": 4, "verified": 3, "partial": 2, "proposed": 1}
BORDERLINE_JACCARD = 0.5
_STOP = {"the", "a", "an", "of", "and", "or", "with", "in", "on", "to", "for", "due", "by"}
Judge = Callable[["Item", "Item"], bool]


@dataclass
class Attestation:
    literature_id: int | None
    sentence_ids: list[str]
    quote: str
    verdict: str  # supported / partial / unsupported


@dataclass
class Item:
    kind: str  # red_flag, differential, history_item, evaluation, management_topic, vocabulary
    key: str
    text: str
    origin: str  # curated, guideline, proposed
    population: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    pack_ref: str | None = None
    mesh_ui: str | None = None
    sources: list[str] = field(default_factory=list)
    attestations: list[Attestation] = field(default_factory=list)
    merged_from: list[str] = field(default_factory=list)
    disagreement: str | None = None
    status: str = "active"  # active / rejected

    @property
    def trust(self) -> str:
        if self.origin == "curated":
            return "curated"
        verdicts = {a.verdict for a in self.attestations}
        if "supported" in verdicts:
            return "verified"
        if "partial" in verdicts:
            return "partial"
        return "proposed"


@dataclass
class MergeResult:
    items: list[Item]
    merges: list[tuple[str, str]] = field(default_factory=list)  # (kept key, merged key)
    disagreements: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


def normalise(name: str) -> str:
    """Comparison key: bracketed parts dropped ("(CBT)"), stop words removed, plurals singular ("anomalies" →
    "anomaly"), words sorted (so "self-harm risk" = "risk for self-harm")."""
    name = re.sub(r"\([^)]*\)", " ", name)
    words = re.findall(r"[a-z0-9]+", name.casefold())
    out = []
    for w in words:
        if w in _STOP:
            continue
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return " ".join(sorted(out))


def _jaccard(a: str, b: str) -> float:
    x, y = set(a.split()), set(b.split())
    return len(x & y) / len(x | y) if x | y else 0.0


def _clean(item: Item) -> Item:
    kept = [a for a in item.attestations if a.verdict in ("supported", "partial")]
    item = replace(item, attestations=kept)
    if item.origin == "guideline" and not kept:
        item = replace(item, status="rejected")
    return item


# Attributes where two sources can genuinely disagree (B7.4: e.g. different age thresholds). Free-text attributes
# (why, question, context, features, indication…) are wording and never make a disagreement.
CLINICAL_KEYS = ("age_threshold", "threshold", "timeframe", "interval", "dose_free_limit")
URGENCY = {"emergency": "emergency", "urgent": "urgent", "routine": "routine_review",
           "routine_review": "routine_review"}


def _level(item: Item) -> str | None:
    raw = item.attributes.get("level") or item.attributes.get("urgency")
    return URGENCY.get(raw) if raw else None


def _conflicts(a: Item, b: Item) -> bool:
    la, lb = _level(a), _level(b)
    if la and lb and la != lb:
        return True  # e.g. one guideline says urgent, another emergency
    shared = (set(a.attributes) & set(b.attributes)) & set(CLINICAL_KEYS)
    return any(a.attributes[k] != b.attributes[k] for k in shared)


def _same(a: Item, b: Item, judge: Judge | None, same: set[frozenset] | None = None) -> bool:
    if a.kind != b.kind or (a.population or "all") != (b.population or "all"):
        return False
    if same and frozenset((a.key, b.key)) in same:
        return True  # decided by the grouping judge, whatever the wording
    if a.mesh_ui and a.mesh_ui == b.mesh_ui:
        return True
    na, nb = normalise(a.text), normalise(b.text)
    if na == nb:
        return True
    if judge is not None and _jaccard(na, nb) >= BORDERLINE_JACCARD:
        return bool(judge(a, b))
    return False


def _combine(kept: Item, other: Item) -> Item:
    seen = {(x.literature_id, tuple(x.sentence_ids)) for x in kept.attestations}
    extra = [x for x in other.attestations if (x.literature_id, tuple(x.sentence_ids)) not in seen]
    return replace(
        kept,
        sources=list(dict.fromkeys(kept.sources + other.sources)),
        attestations=kept.attestations + extra,
        merged_from=kept.merged_from + [other.key] + other.merged_from,
        pack_ref=kept.pack_ref or other.pack_ref,
        mesh_ui=kept.mesh_ui or other.mesh_ui,
        attributes={**other.attributes, **kept.attributes},
    )


def merge(items: Iterable[Item], judge: Judge | None = None, same: set[frozenset] | None = None) -> MergeResult:
    """`judge` decides borderline pairs (Jaccard ≥ BORDERLINE_JACCARD); `same` is a set of key pairs already
    judged to be duplicates (e.g. by a grouping call), merged regardless of wording."""
    result = MergeResult(items=[])
    pool = [_clean(i) for i in items]
    result.rejected = [i.key for i in pool if i.status == "rejected"]
    active = [i for i in pool if i.status == "active"]
    # Highest trust first, so the kept item of a group is the most trusted one; stable within a trust level.
    active.sort(key=lambda i: -TRUST_RANK[i.trust])
    groups: list[list[Item]] = []
    for item in active:
        for group in groups:
            # A judged pair may name any member of a group, not only its first (kept) item.
            if _same(group[0], item, judge, same) or (same and any(
                    frozenset((m.key, item.key)) in same and _same(m, item, None, same) for m in group[1:])):
                group.append(item)
                break
        else:
            groups.append([item])
    for group in groups:
        kept = group[0]
        sides = [kept]
        for other in group[1:]:
            if any(_conflicts(s, other) for s in sides):
                sides.append(other)
                continue
            merged = _combine(sides[0], other)
            result.merges.append((sides[0].key, other.key))
            sides[0] = merged
        if len(sides) > 1:
            tag = f"{kept.kind}:{normalise(kept.text)}"
            sides = [replace(s, disagreement=tag) for s in sides]
            result.disagreements.append(tag)
        result.items += sides
    result.items += [i for i in pool if i.status == "rejected"]
    return result


def from_floor_and_packs(floor, packs) -> list[Item]:
    """Curated items: universal floor rules and every pack's items (origin and trust `curated`)."""
    out: list[Item] = []
    if floor is not None:
        for r in floor.rules:
            out.append(Item("red_flag", f"rule:{r.id}", r.title, "curated", population=r.population,
                            attributes={"level": r.level}, sources=list(r.sources)))
    for p in packs:
        ref = f"pack:{p.complaint}"
        for r in p.red_flags:
            out.append(Item("red_flag", f"{ref}:{r.id}", r.title, "curated", population=r.population,
                            attributes={"level": r.level}, pack_ref=f"{ref}:{r.id}", sources=list(r.sources)))
        for e in p.etiologies:
            out.append(Item("differential", f"{ref}:{e.id}", e.name, "curated", pack_ref=f"{ref}:{e.id}",
                            attributes={"red_flag": e.red_flag}, sources=list(e.sources)))
        for h in p.history_checklist:
            out.append(Item("history_item", f"{ref}:{h.id}", h.question, "curated", pack_ref=f"{ref}:{h.id}",
                            attributes={"attribute": h.attribute, "priority": h.priority}, sources=list(h.sources)))
        for ev in p.evaluations:
            out.append(Item("evaluation", f"{ref}:{ev.id}", ev.test, "curated", pack_ref=f"{ref}:{ev.id}",
                            sources=list(ev.sources)))
        for m in p.management_topics:
            out.append(Item("management_topic", f"{ref}:{m.id}", m.topic, "curated", pack_ref=f"{ref}:{m.id}",
                            sources=list(m.sources)))
    return out
