"""Three-state rule conditions over a case (PLAN B7.1 "fires"): `true`, `unknown` or `false`.

A case is its English segments (`[(seg_id, text_en)]`) plus structured facts from the brief (age, pregnancy, …).
Condition grammar (YAML):

    {all: [c, …]}          true if every part is true; false if any is false; else unknown
    {any: [c, …]}          true if any part is true; false if every part is false; else unknown
    {not: c}               swaps true and false
    {text: [regex, …]}     a positive (non-negated) mention → true; otherwise false
    {mention: [regex, …]}  a positive mention → true; only negated mentions → false; never mentioned → unknown
                           (use it for the features a triage nurse would ask about)
    {fact: name, <op>: v}  op is eq, ne, lt, lte, gt, gte or in; a missing fact → unknown

Patterns are case-insensitive regexes matched on whole words. A mention is negated when a negation cue ("no",
"not", "without", "denies", "never", "n't", …) comes before it in the same segment, within NEGATION_WINDOW words,
with no scope breaker ("but", "however", …) in between.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

TRUE, UNKNOWN, FALSE = "true", "unknown", "false"
NEGATION_WINDOW = 6
FACTS = {
    "age_years": float, "age_months": float, "sex": str, "pregnant": bool, "weeks_pregnant": float,
    "postpartum": bool, "immunocompromised": bool, "chemotherapy": bool, "on_anticoagulants": bool,
    "diabetes": bool, "postmenopausal": bool,
}
OPS = ("eq", "ne", "lt", "lte", "gt", "gte", "in")

_NEGATION = re.compile(
    r"\b(?:no|not|never|none|without|nor|neither|denies|deny|denied|negative\s+for|free\s+of|absence\s+of|"
    r"\w+n't|cannot|cant|dont|doesnt|didnt|havent|hasnt|isnt|wasnt|arent|werent)\b", re.IGNORECASE)
_BREAKER = re.compile(r"\b(?:but|however|although|though|except|apart\s+from|yet|still)\b|[;:]", re.IGNORECASE)
_PSEUDO = re.compile(r"\b(?:not\s+only|not\s+just|no\s+doubt|not\s+sure|no\s+idea|no\s+matter|no\s+longer\s+able)\b",
                     re.IGNORECASE)


class ConditionError(ValueError):
    pass


@dataclass
class Result:
    state: str
    seg_ids: list[str] = field(default_factory=list)  # segments with a positive mention that counted
    unknown_facts: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # pragma: no cover - guard against `if result:`
        raise TypeError("Use result.state")


@dataclass
class Case:
    segments: list[tuple[str, str]]  # (seg_id, English text)
    facts: dict[str, Any] = field(default_factory=dict)


# ---------- compiling ----------

def _pattern(raw: str) -> re.Pattern:
    try:
        return re.compile(rf"(?<!\w)(?:{raw})(?!\w)", re.IGNORECASE)
    except re.error as exc:
        raise ConditionError(f"bad pattern {raw!r}: {exc}") from exc


def compile_condition(node: Any) -> dict:
    """Validate a condition tree and pre-compile its patterns. Raises ConditionError."""
    if not isinstance(node, dict) or not node:
        raise ConditionError(f"a condition must be a mapping, got {node!r}")
    keys = set(node)
    if keys in ({"all"}, {"any"}):
        (op,) = keys
        parts = node[op]
        if not isinstance(parts, list) or not parts:
            raise ConditionError(f"{op} needs a non-empty list")
        return {op: [compile_condition(p) for p in parts]}
    if keys == {"not"}:
        return {"not": compile_condition(node["not"])}
    if keys in ({"text"}, {"mention"}):
        (op,) = keys
        patterns = node[op]
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
            raise ConditionError(f"{op} needs a list of patterns")
        return {op: [_pattern(p) for p in patterns], "raw": list(patterns)}
    if "fact" in keys:
        name = node["fact"]
        ops = keys - {"fact"}
        if name not in FACTS:
            raise ConditionError(f"unknown fact {name!r} (known: {', '.join(FACTS)})")
        if len(ops) != 1 or not ops <= set(OPS):
            raise ConditionError(f"fact {name} needs exactly one of {OPS}")
        (op,) = ops
        return {"fact": name, "op": op, "value": node[op]}
    raise ConditionError(f"unknown condition keys {sorted(keys)}")


def patterns_of(compiled: dict) -> list[str]:
    """Every raw pattern in a compiled tree (for reports and duplicate checks)."""
    if "raw" in compiled:
        return list(compiled["raw"])
    out: list[str] = []
    for key in ("all", "any"):
        for part in compiled.get(key, []):
            out += patterns_of(part)
    if "not" in compiled:
        out += patterns_of(compiled["not"])
    return out


def facts_of(compiled: dict) -> list[str]:
    if "fact" in compiled:
        return [compiled["fact"]]
    out: list[str] = []
    for key in ("all", "any"):
        for part in compiled.get(key, []):
            out += facts_of(part)
    if "not" in compiled:
        out += facts_of(compiled["not"])
    return out


# ---------- matching ----------

def negated(text: str, start: int) -> bool:
    """Is the match starting at `start` inside a negation's scope?"""
    before = re.split(r"[.!?]\s", text[:start])[-1]  # negation never crosses a sentence end
    window = before.split()[-NEGATION_WINDOW:]
    if not window:
        return False
    scope = " ".join(window)
    cues = list(_NEGATION.finditer(scope))
    if not cues:
        return False
    last = cues[-1]
    if _PSEUDO.search(scope[last.start():last.start() + 16]):
        return False
    return not _BREAKER.search(scope[last.end():])


def mentions(patterns: list[re.Pattern], segments: Iterable[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """(segments with a positive mention, segments with only negated mentions)."""
    positive, negative = [], []
    for seg_id, text in segments:
        text = (text or "").replace("’", "'")
        hits = [m for p in patterns for m in p.finditer(text or "")]
        if not hits:
            continue
        if any(not negated(text, m.start()) for m in hits):
            positive.append(seg_id)
        else:
            negative.append(seg_id)
    return positive, negative


def _fact(case: Case, name: str, op: str, value: Any) -> str:
    have = case.facts.get(name)
    if have is None:
        return UNKNOWN
    try:
        ok = {
            "eq": lambda: have == value, "ne": lambda: have != value, "lt": lambda: have < value,
            "lte": lambda: have <= value, "gt": lambda: have > value, "gte": lambda: have >= value,
            "in": lambda: have in value,
        }[op]()
    except TypeError:
        return UNKNOWN
    return TRUE if ok else FALSE


def evaluate(compiled: dict, case: Case) -> Result:
    if "all" in compiled or "any" in compiled:
        op = "all" if "all" in compiled else "any"
        results = [evaluate(p, case) for p in compiled[op]]
        states = {r.state for r in results}
        if op == "all":
            state = FALSE if FALSE in states else UNKNOWN if UNKNOWN in states else TRUE
            counted = results if state != FALSE else []
        else:
            state = TRUE if TRUE in states else UNKNOWN if UNKNOWN in states else FALSE
            counted = [r for r in results if r.state == TRUE] if state == TRUE else results
        seg_ids = list(dict.fromkeys(s for r in counted for s in r.seg_ids))
        unknown = list(dict.fromkeys(f for r in results for f in r.unknown_facts)) if state == UNKNOWN else []
        return Result(state, seg_ids, unknown)
    if "not" in compiled:
        inner = evaluate(compiled["not"], case)
        flipped = {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[inner.state]
        return Result(flipped, [], inner.unknown_facts)
    if "text" in compiled:
        positive, _ = mentions(compiled["text"], case.segments)
        return Result(TRUE if positive else FALSE, positive)
    if "mention" in compiled:
        positive, negative = mentions(compiled["mention"], case.segments)
        if positive:
            return Result(TRUE, positive)
        return Result(FALSE if negative else UNKNOWN, [])
    state = _fact(case, compiled["fact"], compiled["op"], compiled["value"])
    return Result(state, [], [compiled["fact"]] if state == UNKNOWN else [])
