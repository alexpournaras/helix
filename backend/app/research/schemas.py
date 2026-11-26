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


# ---------- safety (B8.2) ----------

LEVEL = Literal["none", "routine_review", "urgent", "emergency"]


class SafetyFlag(_Out):
    flag: str = Field(min_length=1, max_length=200)
    rule_ref: str | None = None  # `rule:{id}`, `pack:{pack}:{id}` or `K{id}` when it matches a listed rule or item
    reason: str = Field(max_length=400)
    seg_ids: list[str] = Field(default_factory=list)


class GuidelineCheck(_Out):
    item: str  # `K{id}` of a guideline red flag shown in the material
    state: Literal["fires", "unknown", "not_applicable"]
    seg_ids: list[str] = Field(default_factory=list)


class RuleOutQuestion(_Out):
    key: str = Field(pattern=SLUG, max_length=60)
    text: str = Field(min_length=5, max_length=300)


class SafetyReview(_Out):
    level: LEVEL
    crisis: bool  # the text suggests thoughts of suicide or self-harm
    flags: list[SafetyFlag] = Field(default_factory=list)
    guideline_checks: list[GuidelineCheck] = Field(default_factory=list)
    recommendation: str = Field(max_length=600)
    rule_out_questions: list[RuleOutQuestion] = Field(default_factory=list, max_length=3)


# ---------- screening (B8.11; grounding uses it with its own prompt) ----------

DESIGN = Literal["guideline", "systematic_review", "rct", "cohort", "diagnostic_accuracy", "case_control",
                 "cross_sectional", "narrative_review", "mechanistic", "case_series", "case_report", "opinion",
                 "other"]
POPULATION = Literal["all", "infant", "child", "pregnancy", "older", "immunocompromised"]


class ScreenItem(_Out):
    paper_id: str
    relevance: int = Field(ge=0, le=3)
    hypothesis_keys: list[str] = Field(default_factory=list)
    design: DESIGN
    population_note: str = Field(default="", max_length=200)
    decision: Literal["include", "exclude"]
    reason: str = Field(max_length=300)


class ScreeningBatch(_Out):
    items: list[ScreenItem] = Field(default_factory=list)


# ---------- knowledge extraction (B8.3a step 5) ----------

class KRedFlag(_Out):
    condition: str = Field(min_length=3, max_length=300)
    features: list[str] = Field(default_factory=list, max_length=10)
    urgency: Literal["emergency", "urgent", "routine"]
    population: POPULATION = "all"
    question: str = Field(default="", max_length=300)  # how to ask the person about it (B8.2 rule-out)
    sentence_ids: list[str] = Field(default_factory=list)


class KDifferential(_Out):
    condition: str = Field(min_length=3, max_length=200)
    key_features: list[str] = Field(default_factory=list, max_length=10)
    typical_population: str = Field(default="", max_length=200)
    population: POPULATION = "all"
    sentence_ids: list[str] = Field(default_factory=list)


class KHistoryItem(_Out):
    attribute: str = Field(min_length=3, max_length=200)
    question: str = Field(default="", max_length=300)  # the question to the person
    why: str = Field(default="", max_length=300)
    population: POPULATION = "all"
    sentence_ids: list[str] = Field(default_factory=list)


class KEvaluation(_Out):
    test: str = Field(min_length=2, max_length=200)
    indication: str = Field(default="", max_length=300)
    population: POPULATION = "all"
    sentence_ids: list[str] = Field(default_factory=list)


class KManagementTopic(_Out):
    topic: str = Field(min_length=3, max_length=200)
    context: str = Field(default="", max_length=300)
    population: POPULATION = "all"
    sentence_ids: list[str] = Field(default_factory=list)


class KnowledgeExtraction(_Out):
    red_flags: list[KRedFlag] = Field(default_factory=list)
    differential: list[KDifferential] = Field(default_factory=list)
    history_items: list[KHistoryItem] = Field(default_factory=list)
    evaluations: list[KEvaluation] = Field(default_factory=list)
    management_topics: list[KManagementTopic] = Field(default_factory=list)


# ---------- attestation verification (B8.3a step 6, B8.15 rules) ----------

class AttestationVote(_Out):
    pair: str  # `X{n}` from the material
    verdict: Literal["supported", "partial", "unsupported"]
    population_check: Literal["same", "broader", "different", "not_stated"]
    overgeneralization: bool
    context_needed: bool
    reason: str = Field(max_length=300)


class AttestationBatch(_Out):
    items: list[AttestationVote] = Field(default_factory=list)


class SamePair(_Out):
    pair: str
    same: bool


class MergeJudgement(_Out):
    pairs: list[SamePair] = Field(default_factory=list)


class DuplicateGroup(_Out):
    keep: str  # `X{n}` of the item to keep (the clearest wording)
    same: list[str] = Field(default_factory=list)  # `X{n}` of items that mean the same thing


class MergeGroups(_Out):
    groups: list[DuplicateGroup] = Field(default_factory=list)


# ---------- questions (B8.4) and brief update (B8.5) ----------

class Question(_Out):
    key: str = Field(pattern=SLUG, max_length=60)
    text: str = Field(min_length=5, max_length=300)
    why: str = Field(default="", max_length=300)
    answer_type: Literal["text", "yes_no", "choice", "number"]
    options: list[str] = Field(default_factory=list, max_length=8)
    priority: int = Field(ge=0, le=3)
    expected_impact: float = Field(ge=0, le=1)
    targets: list[str] = Field(default_factory=list)  # unknown keys, `K{id}` items or rule-out keys it covers
    item_id: str | None = None  # `K{id}` when it asks about one knowledge item


class MergeRuleOut(_Out):
    keep: str  # a rule-out key shown in the material
    drop: list[str] = Field(default_factory=list)  # panel rule-outs that ask the same thing


class QuestionSet(_Out):
    questions: list[Question] = Field(default_factory=list)
    merge_rule_outs: list[MergeRuleOut] = Field(default_factory=list)


class RemovedFeature(_Out):
    key: str  # the previous brief's fact path (e.g. `symptoms[0].duration`)
    seg_ids: list[str] = Field(default_factory=list)  # the segments that contradict it


class BriefUpdate(_Out):
    translations: list[Translation] = Field(default_factory=list)
    complaints: list[Complaint] = Field(default_factory=list)
    proposed_systems: list[SYSTEMS] = Field(default_factory=list)
    brief: CaseBrief
    removed_features: list[RemovedFeature] = Field(default_factory=list)


# ---------- plan (B8.6) ----------

class Hypothesis(_Out):
    key: str = Field(pattern=SLUG, max_length=60)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    mechanism: str = Field(default="", max_length=1000)
    kind: Literal["red_flag", "common", "less_common"]  # red_flag: serious, must be ruled out
    item_id: str | None = None  # `K{id}` of the differential knowledge item it comes from
    features_explained: list[str] = Field(default_factory=list)  # case feature keys
    features_unexplained: list[str] = Field(default_factory=list)
    discriminators: list[str] = Field(default_factory=list, max_length=8)  # what would tell it apart
    prior: Literal["high", "moderate", "low"]
    prior_reason: str = Field(default="", max_length=500)


class UncoveredFeature(_Out):
    key: str  # a case feature key that no hypothesis explains
    reason: str = Field(min_length=1, max_length=300)


class Plan(_Out):
    research_questions: list[str] = Field(default_factory=list, max_length=20)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    uncovered_features: list[UncoveredFeature] = Field(default_factory=list)


class CoverageDecision(_Out):
    item_id: str  # `K{id}`
    decision: Literal["hypothesis", "excluded"]
    hypothesis_key: str | None = None
    reason: str = Field(default="", max_length=500)
    seg_ids: list[str] = Field(default_factory=list)  # case segments that justify an exclusion


class ConsolidatedPlan(Plan):
    knowledge_coverage: list[CoverageDecision] = Field(default_factory=list)


# ---------- queries (B8.7) ----------

QUERY_TYPES = Literal["guideline", "systematic_review", "review", "rct", "observational", "diagnostic",
                      "mechanism", "other"]


class SearchQuery(_Out):
    source: Literal["europepmc", "pubmed", "openalex", "semantic_scholar"]
    query: str = Field(min_length=3, max_length=1000)
    query_type: QUERY_TYPES
    rationale: str = Field(default="", max_length=300)


class QuerySet(_Out):
    queries: list[SearchQuery] = Field(default_factory=list)


class QueryRefinement(_Out):
    query: str = Field(min_length=3, max_length=1000)
    rationale: str = Field(default="", max_length=300)


# ---------- read (B8.14) ----------

class ProfileField(_Out):
    value: str | None = Field(default=None, max_length=600)
    sentence_ids: list[str] = Field(default_factory=list, max_length=6)


class PaperProfile(_Out):
    question: ProfileField
    design: ProfileField
    setting: ProfileField
    population: ProfileField
    inclusion_criteria: ProfileField
    n: ProfileField
    exposure: ProfileField
    comparator: ProfileField
    outcomes: ProfileField
    follow_up: ProfileField
    funding: ProfileField
    conflicts: ProfileField
    review_included_designs: ProfileField
    dataset_or_cohort: ProfileField  # study identity (B8.14): a named cohort, registry or dataset
    secondary_analysis: ProfileField  # "yes" when the paper re-analyses data published elsewhere


RISK = Literal["low", "some", "high", "unclear"]


class Criterion(_Out):
    criterion: str = Field(min_length=1, max_length=60)
    rating: RISK
    reason: str = Field(default="", max_length=300)
    sentence_ids: list[str] = Field(default_factory=list, max_length=4)


class Appraisal(_Out):
    criteria: list[Criterion] = Field(default_factory=list)
    overall_risk: RISK


class Effect(_Out):
    measure: str = Field(min_length=1, max_length=40)  # OR, RR, HR, MD, sensitivity, …
    value: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    p: str | None = Field(default=None, max_length=20)  # as written: "0.03", "<0.001"


class ClaimLink(_Out):
    hypothesis_key: str
    direction: Literal["supports", "contradicts", "neutral"]
    strength: Literal["weak", "moderate", "strong"]
    case_match: Literal["high", "partial", "low", "unknown"]
    reason: str = Field(default="", max_length=300)


class Claim(_Out):
    claim: str = Field(min_length=5, max_length=400)
    sentence_ids: list[str] = Field(min_length=1, max_length=4)
    evidence_type: Literal["finding", "mechanism", "guideline_recommendation", "epidemiology",
                           "diagnostic_accuracy", "background"]
    population: str = Field(default="", max_length=200)
    design: DESIGN
    n: int | None = Field(default=None, ge=1)
    effect: Effect | None = None
    limitations: str = Field(default="", max_length=300)
    links: list[ClaimLink] = Field(default_factory=list)


class ClaimSet(_Out):
    claims: list[Claim] = Field(default_factory=list)


class SameJudgement(_Out):
    pair: str  # `M{n}`
    same: bool
    reason: str = Field(default="", max_length=200)


class SameClaims(_Out):
    pairs: list[SameJudgement] = Field(default_factory=list)


MATCH = Literal["high", "partial", "low", "unknown"]


class Applicability(_Out):
    population_match: MATCH
    symptom_match: MATCH
    setting_match: MATCH
    special_population_fit: Literal["match", "partial", "mismatch", "not_applicable"]
    overall: Literal["high", "partial", "low"]
    reason: str = Field(min_length=1, max_length=400)


# ---------- claim verification (B8.15) ----------

class ClaimVote(_Out):
    pair: str  # `C{n}` from the material
    asserts_effect: bool  # the claim says there is an effect or association
    direction_check: Literal["same", "opposite", "not_applicable"]
    significance: Literal["significant", "not_significant", "not_reported", "not_applicable"]
    population_check: Literal["same", "broader", "different", "not_stated"]
    overgeneralization: bool
    context_needed: bool
    verdict: Literal["supported", "partial", "unsupported"]
    reason: str = Field(max_length=300)


class ClaimVoteBatch(_Out):
    items: list[ClaimVote] = Field(default_factory=list)


# ---------- map (B8.16) ----------

class MapItem(_Out):
    claim: str  # `C{n}` from the material
    links: list[ClaimLink] = Field(default_factory=list)  # one per hypothesis it bears on; `neutral` cancels


class MapBatch(_Out):
    items: list[MapItem] = Field(default_factory=list)


# ---------- fit and analysis (B8.20, B8.21) ----------

class FitItem(_Out):
    feature_key: str
    fit: Literal["explains", "compatible", "against", "unknown"]
    claim_ids: list[str] = Field(default_factory=list, max_length=6)  # `C{n}` from the material
    reason: str = Field(default="", max_length=300)


class FitSet(_Out):
    items: list[FitItem] = Field(default_factory=list)


class AnalysisStatement(_Out):
    text: str = Field(min_length=5, max_length=600)
    kind: Literal["evidence", "inference", "case_fact", "recommendation", "general", "caveat"]
    claim_ids: list[str] = Field(default_factory=list, max_length=8)  # `C{n}`
    seg_ids: list[str] = Field(default_factory=list, max_length=6)  # the person's segments


class HypothesisAnalysis(_Out):
    statements: list[AnalysisStatement] = Field(default_factory=list, max_length=12)
    strongest_support: list[str] = Field(default_factory=list, max_length=5)  # `C{n}`
    strongest_contra: list[str] = Field(default_factory=list, max_length=5)
    open_questions: list[str] = Field(default_factory=list, max_length=6)
