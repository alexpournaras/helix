"""Pydantic schemas for model outputs (PLAN B2, B8). API bodies join in 1G.

Every schema here goes through `strict.to_provider_schema`, so: no free-form maps (use lists of pairs),
no recursion, and constraints (lengths, patterns, ranges) are enforced by Pydantic after the call and listed
in the prompt by `strict.constraint_notes`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SYSTEMS = Literal[
    "ent", "neurology", "cardiovascular", "respiratory", "digestive", "mouth_dental", "musculoskeletal", "skin",
    "urogenital", "female_reproductive", "pregnancy", "eye", "endocrine_metabolic", "hematology",
    "immunology_allergy", "infection", "oncology", "mental_health", "congenital_neonatal", "injury_poisoning",
    "general",
]
# Conditions and medications that change who the case is (B0 special populations, B7.1 rule facts).
FLAGS = Literal["immunosuppressant", "chemotherapy", "anticoagulant", "immunodeficiency", "diabetes",
                "serious_chronic"]
NOT_ACCEPTED = Literal["not_health", "not_human", "prescription_or_dose"]
SLUG = r"^[a-z0-9]+(_[a-z0-9]+)*$"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- the case brief (B8.1) ----------

class Fact(_Out):
    """One stated fact: English text and the user segments that state it."""
    text: str = Field(min_length=1, max_length=500)
    seg_ids: list[str] = Field(default_factory=list)
    implied: bool = False  # set by code after the brief check, never by the model


class FlaggedFact(Fact):
    flags: list[FLAGS] = Field(default_factory=list)


class AgeFact(Fact):
    years: float | None = Field(default=None, ge=0, le=125)


class SexFact(Fact):
    value: Literal["female", "male", "other"]


class PregnancyFact(Fact):
    status: Literal["pregnant", "postpartum", "not_pregnant"]
    weeks: float | None = Field(default=None, ge=0, le=45)


class Symptom(_Out):
    name: Fact
    body_site: Fact | None = None
    laterality: Fact | None = None
    onset: Fact | None = None
    duration: Fact | None = None
    frequency: Fact | None = None
    pattern: Fact | None = None
    character: Fact | None = None
    severity: Fact | None = None
    triggers: Fact | None = None
    relievers: Fact | None = None
    modifiers: Fact | None = None


class Demographics(_Out):
    age: AgeFact | None = None
    sex: SexFact | None = None
    pregnancy: PregnancyFact | None = None


class Lifestyle(_Out):
    smoking: Fact | None = None
    alcohol: Fact | None = None
    substances: Fact | None = None
    occupation: Fact | None = None


class Feature(_Out):
    key: str = Field(pattern=SLUG, max_length=60)
    text: str = Field(min_length=1, max_length=500)
    seg_ids: list[str] = Field(default_factory=list)
    implied: bool = False


class Unknown(_Out):
    key: str = Field(pattern=SLUG, max_length=60)
    text: str = Field(min_length=1, max_length=300)
    priority: int = Field(ge=1, le=3)  # 1 = needed before research (B4 safety rule step 4)


class CaseBrief(_Out):
    chief_complaint: Fact | None = None
    symptoms: list[Symptom] = Field(default_factory=list)
    associated_symptoms: list[Fact] = Field(default_factory=list)
    stated_negatives: list[Fact] = Field(default_factory=list)
    demographics: Demographics = Field(default_factory=Demographics)
    chronic_conditions: list[FlaggedFact] = Field(default_factory=list)
    medications: list[FlaggedFact] = Field(default_factory=list)
    allergies: list[Fact] = Field(default_factory=list)
    family_history: list[Fact] = Field(default_factory=list)
    lifestyle: Lifestyle = Field(default_factory=Lifestyle)
    travel: list[Fact] = Field(default_factory=list)
    recent_infections: list[Fact] = Field(default_factory=list)
    vitals_and_tests: list[Fact] = Field(default_factory=list)
    case_features: list[Feature] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    follow_up_focus: Fact | None = None


# ---------- intake (B8.1) ----------

class Translation(_Out):
    seg_id: str
    text_en: str = Field(min_length=1)


class Complaint(_Out):
    key: str = Field(pattern=SLUG, max_length=60)
    text: str = Field(min_length=1, max_length=300)
    seg_ids: list[str] = Field(default_factory=list)
    mesh_candidates: list[str] = Field(default_factory=list, max_length=5)


class IntakeResult(_Out):
    is_health_problem: bool
    is_human: bool
    not_accepted_reason: NOT_ACCEPTED | None = None
    language: str = Field(min_length=2, max_length=8)  # ISO 639-1, e.g. "en", "el"
    translations: list[Translation] = Field(default_factory=list)
    title: str = Field(max_length=120)
    complaints: list[Complaint] = Field(default_factory=list)
    proposed_systems: list[SYSTEMS] = Field(default_factory=list)
    brief: CaseBrief


class CheckItem(_Out):
    path: str
    verdict: Literal["stated", "implied", "not_stated"]
    reason: str = Field(max_length=300)


class BriefCheck(_Out):
    items: list[CheckItem] = Field(default_factory=list)
