"""Safety rule engine and fixed texts (PLAN B8.2, B8.3, B7.1, B1.8).

- Rules: the universal floor + the red flags of the curated packs that match the case's verified MeSH
  descriptors. Population rules apply only to their population (`Rule.check` is three-state on population and
  conditions). Patterns run on the English text of every user segment; facts come from the brief.
- `unknown` → a rule-out question (priority 0).
- The level is the most cautious of the rule hits; the panel can raise it, never lower it (B1.8).
- Emergency and urgent wording is fixed text. Crisis resources are shown when the self-harm rule fires or a
  panel member reports a crisis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .knowledge import loader
from .knowledge.conditions import TRUE, UNKNOWN, Case

LEVELS = ("none", "routine_review", "urgent", "emergency")
RULES_VERSION = "safety_rules.v1"

TEMPLATES = {
    "emergency": (
        "Some of what you describe can be a sign of a medical emergency. Call your local emergency number now "
        "(112 in Europe, 999 in the UK, 911 in the US) or go to the nearest emergency department. "
        "Don't wait for this research."),
    "urgent": (
        "Some of what you describe should be checked by a doctor soon. Contact your doctor or an urgent care "
        "service within the next few days, sooner if it gets worse. If anything changes suddenly, call your "
        "local emergency number."),
    "routine_review": (
        "Some of what you describe is worth mentioning to your doctor at a routine appointment."),
    "none": "",
}
CRISIS_RESOURCES = (
    "If you are thinking about suicide or harming yourself, you don't have to face this alone. Please talk to "
    "someone now:\n"
    "- Call your local emergency number if you might act on these thoughts (112 in Europe, 999 in the UK, "
    "911 in the US).\n"
    "- Greece: 1018 (suicide prevention line, Klimaka), 24 hours.\n"
    "- UK and Ireland: Samaritans, 116 123, free, 24 hours.\n"
    "- US: call or text 988 (Suicide & Crisis Lifeline).\n"
    "- Elsewhere: your local emergency number, or a crisis line listed at findahelpline.com.")
STOPPED = "Research stopped for your safety. Helix won't continue this investigation."


@dataclass
class RuleHit:
    ref: str  # `rule:{id}` or `pack:{pack}:{id}`
    level: str
    title: str
    state: str  # true / unknown
    question: str
    seg_ids: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    crisis: bool = False
    population: str = "all"

    def to_dict(self) -> dict:
        return {"ref": self.ref, "level": self.level, "title": self.title, "state": self.state,
                "question": self.question, "seg_ids": self.seg_ids, "missing": self.missing,
                "crisis": self.crisis, "population": self.population}


@dataclass
class RuleOutcome:
    evaluated: list[str]
    fired: list[RuleHit]
    unknown: list[RuleHit]

    @property
    def level(self) -> str:
        return most_cautious([h.level for h in self.fired])

    @property
    def crisis(self) -> bool:
        return any(h.crisis for h in self.fired)


def most_cautious(levels: Iterable[str]) -> str:
    best = 0
    for level in levels:
        best = max(best, LEVELS.index(level) if level in LEVELS else len(LEVELS) - 1)
    return LEVELS[best]


def rules_for(mesh_uis: Iterable[str], packs: dict | None = None) -> list[tuple[str, loader.Rule]]:
    """(ref, rule) for the floor and the matching packs' red flags."""
    out = [(f"rule:{r.id}", r) for r in loader.floor().rules]
    for pack in loader.packs_for(list(mesh_uis), packs):
        out += [(f"pack:{pack.complaint}:{r.id}", r) for r in pack.red_flags]
    return out


def evaluate(segments: list[tuple[str, str]], facts: dict, mesh_uis: Iterable[str] = (),
             packs: dict | None = None) -> RuleOutcome:
    case = Case(segments, facts)
    evaluated, fired, unknown = [], [], []
    for ref, rule in rules_for(mesh_uis, packs):
        evaluated.append(ref)
        result = rule.check(case)
        if result.state not in (TRUE, UNKNOWN):
            continue
        hit = RuleHit(ref, rule.level, rule.title, result.state, rule.rule_out_question, list(result.seg_ids),
                      list(result.unknown_facts), rule.crisis, rule.population)
        (fired if result.state == TRUE else unknown).append(hit)
    return RuleOutcome(evaluated, fired, unknown)


def display_text(level: str, recommendations: Iterable[str] = (), crisis: bool = False) -> str:
    """The fixed template for the level, then the panel's recommendations; crisis resources first."""
    parts = []
    if crisis:
        parts.append(CRISIS_RESOURCES)
    if TEMPLATES.get(level):
        parts.append(TEMPLATES[level])
    recs = [r.strip() for r in recommendations if r and r.strip()]
    if recs and level in ("routine_review", "urgent"):  # emergency wording stays fixed (B1.8)
        parts.append("\n".join(f"- {r}" for r in dict.fromkeys(recs)))
    return "\n\n".join(parts)
