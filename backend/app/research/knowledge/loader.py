"""Clinical knowledge files: schema and loading (PLAN B7.1, B7.2, 1B.3).

- `sources.yaml`: every source the floor and packs cite (id → title, publisher, URL, kind).
- `sources.lock.json`: the last live check of every source URL and seed paper (written by
  `eval/tools/check_sources.py`), so tests can require "every source resolves" without the network.
- `universal/red_flags.yaml`: the universal safety floor.
- `packs/<complaint>.yaml`: curated complaint packs.
Knowledge files are content: an agent may draft them, only a human marks them reviewed (Part G.6).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from .conditions import FALSE, TRUE, UNKNOWN, Case, Result, compile_condition, evaluate

ROOT = Path(__file__).resolve().parent
SOURCES_PATH = ROOT / "sources.yaml"
LOCK_PATH = ROOT / "sources.lock.json"
FLOOR_PATH = ROOT / "universal" / "red_flags.yaml"
PACKS_DIR = ROOT / "packs"

AREAS = ("heart_lungs_allergy", "brain_nerves", "infection_sepsis", "abdomen_gut", "urinary_genital", "eye", "ent",
         "skin", "hormones_metabolism", "mental_health", "poisoning_injury", "pregnancy", "children", "older_adults",
         "possible_cancer")
POPULATIONS = ("all", "infant", "child", "pregnancy", "older", "immunocompromised")
LEVELS = ("emergency", "urgent", "routine_review")
LEVEL_RANK = {"emergency": 3, "urgent": 2, "routine_review": 1}
_ID = r"^[a-z0-9_]+(\.[a-z0-9_]+)+$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=3)
    publisher: str = Field(min_length=2)
    url: str = Field(pattern=r"^https://")
    kind: Literal["triage", "guideline", "red_flag_guidance", "patient_information", "review"]


class SourcesFile(_Strict):
    version: int
    sources: list[Source]


class Review(_Strict):
    status: Literal["unreviewed", "reviewed"] = "unreviewed"
    reviewers: list[str] = Field(default_factory=list)
    date: str | None = None
    notes: str = ""


class Example(_Strict):
    text: str = Field(min_length=1)
    facts: dict[str, Any] = Field(default_factory=dict)


class Examples(_Strict):
    fires: list[Example] = Field(min_length=1)
    no: list[Example] = Field(min_length=1)
    unknown: list[Example] = Field(default_factory=list)


class Rule(_Strict):
    id: str = Field(pattern=_ID)
    area: Literal[AREAS]  # type: ignore[valid-type]
    level: Literal[LEVELS]  # type: ignore[valid-type]
    population: Literal[POPULATIONS] = "all"  # type: ignore[valid-type]
    title: str = Field(min_length=5)
    fires: dict[str, Any]
    unknown: str | None = None  # the attribute to ask about when the rule is unknown
    rule_out_question: str = Field(min_length=5)
    crisis: bool = False  # show crisis resources immediately (self-harm, suicide)
    sources: list[str] = Field(min_length=1)
    examples: Examples
    notes: str = ""
    _compiled: dict = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._compiled = compile_condition(self.fires)

    @field_validator("rule_out_question")
    @classmethod
    def _question(cls, value: str) -> str:
        if not value.rstrip().endswith("?"):
            raise ValueError("rule_out_question must be a question")
        return value

    def check(self, case: Case) -> Result:
        """The rule's state for a case: its population and its conditions, both three-state."""
        pop = population_state(self.population, case.facts)
        result = evaluate(self._compiled, case)
        if pop == FALSE or result.state == FALSE:
            return Result(FALSE)
        if pop == UNKNOWN or result.state == UNKNOWN:
            missing = result.unknown_facts + ([f"population:{self.population}"] if pop == UNKNOWN else [])
            return Result(UNKNOWN, result.seg_ids, missing)
        return result


class Floor(_Strict):
    version: int
    review: Review
    rules: list[Rule]


class Etiology(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    key_features: list[str] = Field(default_factory=list)
    discriminating_questions: list[str] = Field(default_factory=list)
    typical_evaluations: list[str] = Field(default_factory=list)
    red_flag: bool = False
    sources: list[str] = Field(min_length=1)


class HistoryItem(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    attribute: str
    priority: Literal["high", "medium", "low"]
    question: str
    sources: list[str] = Field(min_length=1)


class Evaluation(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    test: str
    indication: str
    sources: list[str] = Field(min_length=1)


class ManagementTopic(_Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    topic: str
    context: str
    sources: list[str] = Field(min_length=1)


class Vocabulary(_Strict):
    mesh_terms: list[str] = Field(default_factory=list)
    synonyms: list[str] = Field(default_factory=list)


class SeedPaper(_Strict):
    pmid: str | None = Field(default=None, pattern=r"^\d+$")
    doi: str | None = None
    kind: Literal["guideline", "systematic_review"]
    title: str = ""


class Pack(_Strict):
    version: int
    complaint: str = Field(pattern=r"^[a-z0-9_]+$")
    mesh: list[str] = Field(min_length=1)
    review: Review
    built_by: str = ""
    red_flags: list[Rule] = Field(default_factory=list)
    etiologies: list[Etiology] = Field(default_factory=list)
    history_checklist: list[HistoryItem] = Field(default_factory=list)
    evaluations: list[Evaluation] = Field(default_factory=list)
    management_topics: list[ManagementTopic] = Field(default_factory=list)
    vocabulary: Vocabulary = Field(default_factory=Vocabulary)
    seed_literature: list[SeedPaper] = Field(default_factory=list)

    @field_validator("mesh")
    @classmethod
    def _mesh(cls, values: list[str]) -> list[str]:
        for v in values:
            if not (v.startswith("D") and v[1:].isdigit()):
                raise ValueError(f"bad MeSH descriptor UI {v!r}")
        return values


# ---------- populations ----------

def population_state(population: str, facts: dict[str, Any]) -> str:
    """Is the case in the population? true / false / unknown (a needed fact is missing)."""
    age = facts.get("age_years")
    if age is None and facts.get("age_months") is not None:
        age = facts["age_months"] / 12
    if population == "all":
        return TRUE
    if population in ("infant", "child", "older"):
        if age is None:
            return UNKNOWN
        return TRUE if {"infant": age < 1, "child": age < 16, "older": age >= 65}[population] else FALSE
    if population == "pregnancy":
        if facts.get("pregnant") is True or facts.get("postpartum") is True:
            return TRUE
        if facts.get("sex") == "male" or (age is not None and (age < 10 or age > 60)):
            return FALSE
        if facts.get("pregnant") is False and facts.get("postpartum") is not True:
            return FALSE
        return UNKNOWN
    if population == "immunocompromised":
        if facts.get("immunocompromised") is True or facts.get("chemotherapy") is True:
            return TRUE
        if facts.get("immunocompromised") is False:
            return FALSE
        return UNKNOWN
    raise ValueError(f"unknown population {population!r}")


# ---------- loading ----------

def _yaml(path: Path) -> Any:
    return yaml.safe_load(Path(path).read_text("utf-8")) or {}


def load_sources(path: Path = SOURCES_PATH) -> dict[str, Source]:
    data = SourcesFile.model_validate(_yaml(path))
    out: dict[str, Source] = {}
    for s in data.sources:
        if s.id in out:
            raise ValueError(f"duplicate source id {s.id}")
        out[s.id] = s
    return out


def load_lock(path: Path = LOCK_PATH) -> dict:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError:
        return {"sources": {}, "papers": {}}


def load_floor(path: Path = FLOOR_PATH) -> Floor:
    return Floor.model_validate(_yaml(path))


def load_pack(path: Path) -> Pack:
    return Pack.model_validate(_yaml(path))


def load_packs(folder: Path = PACKS_DIR) -> dict[str, Pack]:
    packs = {}
    for path in sorted(Path(folder).glob("*.yaml")):
        pack = load_pack(path)
        if pack.complaint != path.stem:
            raise ValueError(f"{path.name}: complaint {pack.complaint!r} must match the file name")
        packs[pack.complaint] = pack
    return packs


@lru_cache(maxsize=1)
def floor() -> Floor:
    """The universal floor, loaded once per process."""
    return load_floor()


def packs_for(mesh_uis: list[str], packs: dict[str, Pack] | None = None) -> list[Pack]:
    """Curated packs matching a case's verified descriptors (B7.2)."""
    packs = packs if packs is not None else load_packs()
    wanted = set(mesh_uis)
    return [p for p in packs.values() if wanted & set(p.mesh)]
