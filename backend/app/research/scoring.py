"""Publication-type map, design weights and screening score (PLAN B8.11). Selection and claim weights join in 1D/1E."""

from __future__ import annotations

import math
from typing import Iterable

# First match wins (B8.11). Publication types are compared case-insensitively.
PUB_TYPE_DESIGN: list[tuple[tuple[str, ...], str]] = [
    (("practice guideline", "guideline", "consensus development conference"), "guideline"),
    (("systematic review", "meta-analysis"), "systematic_review"),
    (("randomized controlled trial",), "rct"),
    (("case reports",), "case_report"),
    (("editorial", "comment", "letter"), "opinion"),
    (("review",), "narrative_review"),
]
DESIGN_WEIGHT = {"guideline": 1.0, "systematic_review": .95, "rct": .85, "cohort": .7, "diagnostic_accuracy": .7,
                 "case_control": .6, "cross_sectional": .5, "narrative_review": .45, "mechanistic": .4,
                 "case_series": .35, "case_report": .25, "opinion": .15, "other": .3}


def design_from_pub_types(pub_types: Iterable[str]) -> str | None:
    """The design the publication types imply, or None (then the model's design is used)."""
    types = {t.strip().casefold() for t in pub_types if t}
    for names, design in PUB_TYPE_DESIGN:
        if any(n in types for n in names):
            return design
    return None


def design_for(pub_types: Iterable[str], model_design: str | None) -> str:
    return design_from_pub_types(pub_types) or model_design or "other"


def recency(year: int | None, this_year: int) -> float:
    if year is None:
        return .5
    return max(0.0, 1 - (this_year - year) / 15)


def citations(cited_by: int | None) -> float:
    if cited_by is None:
        return .25
    return min(1.0, math.log10(1 + cited_by) / 3)


def score(*, relevance: int, design: str, year: int | None, this_year: int, cited_by: int | None,
          full_text_likely: bool, n_queries: int, seed: bool = False, population_match: bool = False) -> tuple[float, dict]:
    parts = {"relevance": .45 * relevance / 3, "design": .20 * DESIGN_WEIGHT.get(design, .3),
             "recency": .10 * recency(year, this_year), "citations": .10 * citations(cited_by),
             "full_text": .10 * (1.0 if full_text_likely else 0.0),
             "multi_query": .05 * min(1.0, max(0, n_queries - 1) / 3),
             "seed": .1 if seed else 0.0, "population": .05 if population_match else 0.0}
    return round(sum(parts.values()), 4), {k: round(v, 4) for k, v in parts.items()}
