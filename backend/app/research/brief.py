"""Case brief code checks and derived values (PLAN B8.1, B8.5, B0).

- Facts are addressed by path (`chief_complaint`, `symptoms[0].duration`, `medications[1]`, `demographics.age`).
- Code checks: segment IDs must exist and every fact must cite one. A fact that fails is removed and becomes an
  unknown (it may still be true; the questions step can ask). A symptom without a valid name is removed whole.
- The brief check (family B) confirms each fact: `not_stated` → removed and added to unknowns; `implied` → marked.
- `case_facts` feeds the safety rules (B7.1); `populations` are the B0 special populations.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator

from .schemas import BriefCheck, CaseBrief, Unknown

_GONE = object()
_TOKEN = re.compile(r"([a-z_]+)|\[(\d+)\]")
SKIP_KEYS = ("unknowns",)

# Cautious keyword backstop for flags the model may miss (union with the model's flags; English text).
FLAG_PATTERNS: dict[str, re.Pattern] = {k: re.compile(v, re.I) for k, v in {
    "immunosuppressant": r"\b(immunosuppress\w*|methotrexate|tacrolimus|ciclosporin|cyclosporine|azathioprine|"
                         r"mycophenolate|sirolimus|everolimus|prednisolone|prednisone|dexamethasone|biologic\w*|"
                         r"\w+(?:ximab|zumab|mumab|ercept|acept))\b",
    "chemotherapy": r"\b(chemotherapy|chemo|immunotherapy for cancer)\b",
    "anticoagulant": r"\b(anticoagulant\w*|blood thinner\w*|warfarin|apixaban|rivaroxaban|edoxaban|dabigatran|"
                     r"heparin|enoxaparin)\b",
    "immunodeficiency": r"\b(hiv|aids|immunodeficien\w*|transplant\w*|asplenia|no spleen|splenectomy)\b",
    "diabetes": r"\b(diabet\w*|insulin|metformin)\b",
}.items()}
IMMUNE_FLAGS = ("immunosuppressant", "chemotherapy", "immunodeficiency")
POPULATION_ORDER = ("infant", "child", "pregnancy", "older", "immunocompromised", "serious_chronic")


# ---------- paths ----------

def parse_path(path: str) -> list[str | int]:
    tokens: list[str | int] = []
    for name, index in _TOKEN.findall(path):
        tokens.append(int(index) if index else name)
    if not tokens or "".join(f"[{t}]" if isinstance(t, int) else f".{t}" for t in tokens).lstrip(".") != path:
        raise ValueError(f"bad fact path {path!r}")
    return tokens


def _is_fact(node: Any) -> bool:
    return isinstance(node, dict) and "text" in node and "seg_ids" in node


def iter_facts(data: dict, path: str = "") -> Iterator[tuple[str, dict]]:
    """(path, fact dict) for every fact in a brief dict, in document order."""
    if _is_fact(data):
        yield path, data
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if key in SKIP_KEYS or value is None:
                continue
            yield from iter_facts(value, f"{path}.{key}" if path else key)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            if item is not None and item is not _GONE:
                yield from iter_facts(item, f"{path}[{i}]")


def fact_paths(brief: CaseBrief) -> list[str]:
    return [p for p, _ in iter_facts(brief.model_dump(mode="json"))]


def _remove(data: dict, path: str) -> None:
    tokens = parse_path(path)
    # The name of a symptom carries the symptom: remove the whole symptom.
    if len(tokens) == 3 and tokens[0] == "symptoms" and tokens[2] == "name":
        tokens = tokens[:2]
    parent: Any = data
    for t in tokens[:-1]:
        parent = parent[t]
        if parent is _GONE or parent is None:
            return  # an ancestor (a whole symptom) is already removed
    last = tokens[-1]
    if isinstance(last, int):
        parent[last] = _GONE
    else:
        parent[last] = None


def _compact(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _compact(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_compact(v) for v in node if v is not _GONE]
    return node


# ---------- unknowns ----------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return (s or "item")[:50].strip("_") or "item"


def unknown_for(path: str, fact: dict) -> Unknown:
    """A removed fact becomes an unknown: it may be true, but the problem text doesn't confirm it."""
    tokens = parse_path(path)
    names = [t for t in tokens if isinstance(t, str)]
    field = names[-1]
    priority = 1 if field in ("age", "sex", "pregnancy", "chief_complaint") else 2
    return Unknown(key=_slug(field), text=f"Not confirmed: {fact['text']}"[:300], priority=priority)


def add_unknowns(data: dict, unknowns: Iterable[Unknown]) -> None:
    existing = data.setdefault("unknowns", [])
    keys = {u["key"] for u in existing}
    for u in unknowns:
        key, n = u.key, 2
        while key in keys:
            key = f"{u.key}_{n}"[:60]
            n += 1
        keys.add(key)
        existing.append({**u.model_dump(mode="json"), "key": key})


def ensure_unknowns(brief: CaseBrief) -> CaseBrief:
    """Age and sex decide the special populations and several floor rules: missing → an unknown."""
    data = brief.model_dump(mode="json")
    keys = {u["key"] for u in data.get("unknowns", [])}
    extra = []
    if brief.demographics.age is None and "age" not in keys:
        extra.append(Unknown(key="age", text="Age is not stated.", priority=1))
    if brief.demographics.sex is None and "sex" not in keys:
        extra.append(Unknown(key="sex", text="Sex is not stated.", priority=2))
    add_unknowns(data, extra)
    return CaseBrief.model_validate(data)


def dedupe_unknowns(brief: CaseBrief) -> CaseBrief:
    data = brief.model_dump(mode="json")
    seen, out = set(), []
    for u in data["unknowns"]:
        if u["key"] not in seen:
            seen.add(u["key"])
            out.append(u)
    data["unknowns"] = out
    return CaseBrief.model_validate(data)


# ---------- code checks ----------

def clean_segments(brief: CaseBrief, valid: Iterable[str]) -> tuple[CaseBrief, int, int]:
    """Drop segment IDs that don't exist; a fact left without a citation is removed → unknown.

    Returns (brief, invalid IDs dropped, facts removed)."""
    valid = set(valid)
    data = brief.model_dump(mode="json")
    dropped_ids, removed = 0, []
    for path, fact in list(iter_facts(data)):
        good = [s for s in dict.fromkeys(fact["seg_ids"]) if s in valid]
        dropped_ids += len(fact["seg_ids"]) - len(good)
        fact["seg_ids"] = good
        if not good:
            removed.append((path, dict(fact)))
    return _remove_all(data, removed), dropped_ids, len(removed)


def _remove_all(data: dict, removed: list[tuple[str, dict]]) -> CaseBrief:
    for path, _ in removed:
        _remove(data, path)
    data = _compact(data)
    add_unknowns(data, [unknown_for(p, f) for p, f in removed])
    return CaseBrief.model_validate(data)


def check_covers(check: BriefCheck, paths: Iterable[str]) -> None:
    """The brief check must give one verdict for every fact (validation → one repair call)."""
    wanted = set(paths)
    got = [i.path for i in check.items]
    missing = sorted(wanted - set(got))
    extra = sorted(set(got) - wanted)
    if missing:
        raise ValueError(f"no verdict for {', '.join(missing[:10])}")
    if extra:
        raise ValueError(f"unknown paths {', '.join(extra[:10])}")


def apply_check(brief: CaseBrief, check: BriefCheck) -> tuple[CaseBrief, dict[str, int]]:
    """not_stated → removed + unknown; implied → marked. Returns (brief, verdict counts)."""
    verdicts = {i.path: i.verdict for i in check.items}
    data = brief.model_dump(mode="json")
    removed = []
    counts = {"stated": 0, "implied": 0, "not_stated": 0}
    for path, fact in list(iter_facts(data)):
        verdict = verdicts.get(path, "not_stated")  # no confirmation → the fact doesn't survive (B6)
        counts[verdict] += 1
        if verdict == "implied":
            fact["implied"] = True
        elif verdict == "not_stated":
            removed.append((path, dict(fact)))
    return _remove_all(data, removed), counts


# ---------- derived values ----------

def flags(brief: CaseBrief) -> set[str]:
    """Model flags ∪ keyword flags over conditions and medications."""
    out: set[str] = set()
    for item in [*brief.chronic_conditions, *brief.medications]:
        out.update(item.flags)
        for flag, pattern in FLAG_PATTERNS.items():
            if pattern.search(item.text):
                out.add(flag)
    return out


def case_facts(brief: CaseBrief) -> dict[str, Any]:
    """Facts for the safety rules (B7.1). Missing → absent, so rules see them as unknown."""
    d = brief.demographics
    facts: dict[str, Any] = {}
    if d.age is not None and d.age.years is not None:
        facts["age_years"] = d.age.years
        facts["age_months"] = d.age.years * 12
    if d.sex is not None:
        facts["sex"] = d.sex.value
    if d.pregnancy is not None:
        facts["pregnant"] = d.pregnancy.status == "pregnant"
        facts["postpartum"] = d.pregnancy.status == "postpartum"
        if d.pregnancy.weeks is not None:
            facts["weeks_pregnant"] = d.pregnancy.weeks
    f = flags(brief)
    if any(x in f for x in IMMUNE_FLAGS):
        facts["immunocompromised"] = True
    if "chemotherapy" in f:
        facts["chemotherapy"] = True
    if "anticoagulant" in f:
        facts["on_anticoagulants"] = True
    if "diabetes" in f:
        facts["diabetes"] = True
    return facts


def populations(brief: CaseBrief) -> list[str]:
    """B0 special populations: infant (< 1), child (< 18), pregnancy or postpartum, older adult (≥ 65),
    immunocompromised, serious chronic disease."""
    d = brief.demographics
    out: set[str] = set()
    if d.age is not None and d.age.years is not None:
        if d.age.years < 1:
            out.add("infant")
        elif d.age.years < 18:
            out.add("child")
        elif d.age.years >= 65:
            out.add("older")
    if d.pregnancy is not None and d.pregnancy.status in ("pregnant", "postpartum"):
        out.add("pregnancy")
    f = flags(brief)
    if any(x in f for x in IMMUNE_FLAGS):
        out.add("immunocompromised")
    if "serious_chronic" in f:
        out.add("serious_chronic")
    return [p for p in POPULATION_ORDER if p in out]


def population_key(pops: Iterable[str]) -> str:
    """Grounding key (B4 `ground:{complaint}:{population}`): sorted populations or `general`."""
    pops = sorted(set(pops))
    return "+".join(pops) if pops else "general"
