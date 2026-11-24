"""Knowledge validation (PLAN 1B.1, 1B.3). Every check returns a list of problems; empty means valid.

- sources: unique, https, and resolved at the last live check (`sources.lock.json`);
- rules: unique IDs, known sources, each rule's examples behave as stated (the positive and negative unit test
  per rule), a rule that can be `unknown` names the attribute to ask about, crisis rules are mental-health rules;
- floor coverage: every B7.1 area and special population, emergency and urgent levels, a self-harm crisis rule;
- packs: sources, seed papers resolved, unique item IDs, no doses in management topics (B8 dose rule).
"""

from __future__ import annotations

import re
from typing import Iterable

from .conditions import FALSE, TRUE, UNKNOWN, Case, facts_of
from .loader import AREAS, POPULATIONS, Floor, Pack, Rule, Source

DOSE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|g|ml|mL|iu|IU|units?)\b|\b(?:once|twice|three times)\s+(?:a|per)\s+day\b",
                  re.IGNORECASE)


def _case(example) -> Case:
    from app.research.segment import split_user
    return Case(segments=[(f"U{i}", s) for i, s in enumerate(split_user(example.text), 1)], facts=dict(example.facts))


def check_rule(rule: Rule, sources: dict[str, Source]) -> list[str]:
    problems = [f"{rule.id}: unknown source {s}" for s in rule.sources if s not in sources]
    for kind, expected in (("fires", TRUE), ("no", FALSE), ("unknown", UNKNOWN)):
        for ex in getattr(rule.examples, kind):
            got = rule.check(_case(ex)).state
            if got != expected:
                problems.append(f"{rule.id}: example ({kind}) gave {got}: {ex.text[:70]!r}")
    can_be_unknown = "mention" in repr(rule.fires) or facts_of(rule._compiled) or rule.population != "all"
    if can_be_unknown and not rule.unknown:
        problems.append(f"{rule.id}: can be unknown, so it needs an `unknown` attribute")
    if rule.crisis and rule.area != "mental_health":
        problems.append(f"{rule.id}: crisis rules belong to mental_health")
    return problems


def check_rules(rules: Iterable[Rule], sources: dict[str, Source]) -> list[str]:
    problems, seen = [], set()
    for rule in rules:
        if rule.id in seen:
            problems.append(f"duplicate rule id {rule.id}")
        seen.add(rule.id)
        problems += check_rule(rule, sources)
    return problems


def check_floor(floor: Floor, sources: dict[str, Source]) -> list[str]:
    problems = check_rules(floor.rules, sources)
    areas = {r.area for r in floor.rules}
    problems += [f"floor: no rule for area {a}" for a in AREAS if a not in areas]
    pops = {r.population for r in floor.rules}
    problems += [f"floor: no rule for population {p}" for p in POPULATIONS if p not in pops]
    levels = {r.level for r in floor.rules}
    problems += [f"floor: no {lv} rule" for lv in ("emergency", "urgent") if lv not in levels]
    if not any(r.crisis and r.level == "emergency" for r in floor.rules):
        problems.append("floor: no emergency crisis rule (self-harm or suicide)")
    return problems


def check_sources(sources: dict[str, Source], lock: dict, used: set[str] | None = None) -> list[str]:
    """Every (used) source resolved at the last live check, at the same URL."""
    problems = []
    checked = lock.get("sources", {})
    for sid in sorted(used if used is not None else sources):
        src = sources.get(sid)
        if src is None:
            continue
        entry = checked.get(sid)
        if not entry:
            problems.append(f"source {sid}: never checked (run eval/tools/check_sources.py)")
        elif entry.get("url") != src.url:
            problems.append(f"source {sid}: URL changed since the last check")
        elif entry.get("status") != "ok":
            problems.append(f"source {sid}: did not resolve at the last check ({entry.get('status')})")
    unused = sorted(set(sources) - (used if used is not None else set(sources)))
    problems += [f"source {sid}: not cited by any rule or pack" for sid in unused]
    return problems


def check_pack(pack: Pack, sources: dict[str, Source], lock: dict) -> list[str]:
    problems = check_rules(pack.red_flags, sources)
    seen = set()
    for group in (pack.etiologies, pack.history_checklist, pack.evaluations, pack.management_topics):
        for item in group:
            if item.id in seen:
                problems.append(f"{pack.complaint}: duplicate item id {item.id}")
            seen.add(item.id)
            problems += [f"{pack.complaint}.{item.id}: unknown source {s}" for s in item.sources if s not in sources]
    for topic in pack.management_topics:
        if DOSE.search(f"{topic.topic} {topic.context}"):
            problems.append(f"{pack.complaint}.{topic.id}: management topics never contain doses")
    papers = lock.get("papers", {})
    for seed in pack.seed_literature:
        key = seed.pmid or seed.doi
        if not key:
            problems.append(f"{pack.complaint}: seed paper without pmid or doi")
        elif (papers.get(key) or {}).get("status") != "ok":
            problems.append(f"{pack.complaint}: seed {key} not verified at the last check")
    return problems


def cited_sources(floor: Floor | None, packs: Iterable[Pack]) -> set[str]:
    used: set[str] = set()
    if floor is not None:
        used |= {s for r in floor.rules for s in r.sources}
    for pack in packs:
        used |= {s for r in pack.red_flags for s in r.sources}
        for group in (pack.etiologies, pack.history_checklist, pack.evaluations, pack.management_topics):
            used |= {s for item in group for s in item.sources}
    return used
