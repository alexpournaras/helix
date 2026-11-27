"""Research settings: profiles (quality / balanced / economy) + user overrides (PLAN B15, B6).

Effective settings = quality defaults ← profile overlay ← user overrides (deep merge), validated
by Pydantic. Every investigation stores a snapshot of its effective settings at creation.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Provider = Literal["openai", "anthropic", "ollama", "fake"]


def provider_of(model: str) -> Provider:
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith("fake-"):
        return "fake"
    return "ollama"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRef(Strict):
    model: str
    effort: str | None = None
    provider: Provider | None = None

    @model_validator(mode="after")
    def _provider(self) -> "ModelRef":
        if self.provider is None:
            self.provider = provider_of(self.model)
        return self

    @property
    def family(self) -> str:
        return self.provider or provider_of(self.model)

    def label(self) -> str:
        return f"{self.model}@{self.effort}" if self.effort else self.model


class RoleSpec(Strict):
    """Members run together (a panel / dual); fallback members replace unavailable ones in order."""
    members: list[ModelRef] = Field(min_length=1)
    fallback: list[ModelRef] = Field(default_factory=list)


def _m(model: str, effort: str | None = None) -> dict:
    return {"model": model, "effort": effort}


ASTRA, SOL, LUNA = "gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-luna"
FABLE, OPUS, SONNET, HAIKU = "claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"


def _role(*members: dict, fallback: list[dict] | None = None) -> dict:
    return {"members": list(members), "fallback": fallback or []}


# Fallback chains: same-strength model of the other family first, then a step down.
_FB_A_STRONG = [_m(OPUS, "high"), _m(SOL, "high")]
_FB_B_STRONG = [_m(ASTRA, "high"), _m(SONNET, "high")]
_FB_A_MID = [_m(SONNET, "high"), _m(LUNA, "high")]
_FB_B_MID = [_m(SOL, "high"), _m(HAIKU, "high")]

QUALITY_ROLES: dict[str, dict] = {
    "intake": _role(_m(ASTRA, "high"), fallback=_FB_A_STRONG),
    "brief_check": _role(_m(OPUS, "high"), fallback=_FB_B_STRONG),
    "brief_update": _role(_m(ASTRA, "high"), fallback=_FB_A_STRONG),
    "safety": _role(_m(ASTRA, "high"), _m(OPUS, "high"), fallback=[_m(SOL, "high"), _m(SONNET, "high")]),
    "grounding.queries": _role(_m(SOL, "high"), fallback=_FB_A_MID),
    "grounding.screen": _role(_m(SOL, "high"), _m(SONNET, "high"), fallback=[_m(LUNA, "high"), _m(HAIKU, "high")]),
    "grounding.extract": _role(_m(ASTRA, "high"), _m(OPUS, "high"), fallback=[_m(SOL, "high"), _m(SONNET, "high")]),
    "grounding.verify": _role(_m(SONNET, "high"), _m(SOL, "high"), fallback=[_m(HAIKU, "high"), _m(LUNA, "high")]),
    "grounding.merge": _role(_m(ASTRA, "high"), fallback=_FB_A_STRONG),
    "questions": _role(_m(OPUS, "high"), fallback=_FB_B_STRONG),
    "plan": _role(_m(ASTRA, "high"), _m(FABLE, "high"), fallback=[_m(OPUS, "high"), _m(SOL, "high")]),
    "plan.consolidate": _role(_m(ASTRA, "high"), fallback=_FB_A_STRONG),
    "queries": _role(_m(SOL, "high"), fallback=_FB_A_MID),
    "screen": _role(_m(SOL, "high"), _m(SONNET, "high"), fallback=[_m(LUNA, "high"), _m(HAIKU, "high")]),
    "read.profile": _role(_m(OPUS, "high"), fallback=_FB_B_STRONG),
    "read.appraisal": _role(_m(ASTRA, "high"), _m(OPUS, "high"), fallback=[_m(SOL, "high"), _m(SONNET, "high")]),
    "read.claims": _role(_m(SOL, "high"), _m(OPUS, "high"), fallback=[_m(SONNET, "high"), _m(ASTRA, "high")]),
    "read.whole_paper": _role(_m(FABLE, "high"), fallback=[_m(ASTRA, "high"), _m(OPUS, "high")]),
    "read.reconcile": _role(_m(SOL, "high"), fallback=_FB_A_MID),
    "read.applicability": _role(_m(OPUS, "high"), fallback=_FB_B_STRONG),
    "verify": _role(_m(SONNET, "high"), _m(SOL, "high"), fallback=[_m(HAIKU, "high"), _m(LUNA, "high")]),
    "map": _role(_m(SOL, "high"), fallback=_FB_A_MID),
    "fit": _role(_m(OPUS, "high"), fallback=_FB_B_STRONG),
    "analysis": _role(_m(ASTRA, "high"), fallback=_FB_A_STRONG),
    "gaps": _role(_m(ASTRA, "high"), _m(FABLE, "high"), fallback=[_m(OPUS, "high"), _m(SOL, "high")]),
    "synthesis": _role(_m(FABLE, "xhigh"), fallback=[_m(ASTRA, "xhigh"), _m(OPUS, "high")]),
    "critic.evidence": _role(_m(ASTRA, "high"), fallback=_FB_A_STRONG),
    "critic.safety": _role(_m(OPUS, "high"), fallback=_FB_B_STRONG),
    "critic.reasoning": _role(_m(SOL, "high"), fallback=_FB_A_MID),
    "support_check": _role(_m(SOL, "high"), _m(SONNET, "high"), fallback=[_m(LUNA, "high"), _m(HAIKU, "high")]),
    "revise": _role(_m(FABLE, "xhigh"), fallback=[_m(ASTRA, "xhigh"), _m(OPUS, "high")]),
    "final_check": _role(_m(ASTRA, "xhigh"), fallback=[_m(OPUS, "high"), _m(SOL, "high")]),
    "figure_reader": _role(_m(ASTRA, "high"), _m(OPUS, "high")),
}

# "adjudicate" and "confirm" pick the strongest model of the *other* family at run time (B6).
QUALITY_STRONGEST = {"openai": _m(ASTRA, "xhigh"), "anthropic": _m(FABLE, "xhigh")}

QUALITY: dict[str, Any] = {
    "profile": "quality",
    "roles": QUALITY_ROLES,
    "strongest": QUALITY_STRONGEST,
    "providers": {
        "openai": {"max_output_tokens": 64000, "idle_timeout_seconds": 120, "deadline_seconds": 1800},
        "anthropic": {"version": "2026-10-21", "max_tokens": 64000, "idle_timeout_seconds": 120,
                      "deadline_seconds": 1800},
        "ollama": {"enabled": False, "num_ctx": 32768},
    },
    "ensembles": {"dual_screening": True, "dual_extraction": True, "whole_paper_pass": True, "verify_panel": 2,
                  "adjudicate": True, "appraisal_panel": True, "planners": 2, "gaps_panel": True,
                  "critics": ["evidence", "safety", "reasoning"], "final_check": True,
                  "dual_knowledge_extraction": True},
    "scope": {"readiness_min_cases": 10},
    "grounding": {"enabled": True, "max_complaints": 5, "max_sources_per_complaint": 6, "years": 10,
                  "kinds": ["guideline", "systematic_review", "narrative_review"], "max_per_organisation": 2,
                  "max_results_per_query": 25, "max_screened": 40, "pack_tokens": 32000,
                  "max_packs_per_source": None},
    "knowledge": {"curated_packs": "auto", "universal_floor_version": "latest"},
    "privacy": {"openai_project_shares_data": None, "store_prompts": False, "purge_payloads_after_days": None},
    "autonomy": "review_plan",
    "safety": {"allow_continue_after_urgent": True, "allow_continue_after_emergency": False},
    "questions": {"max_per_round": 10, "max_rounds": 2, "max_gap_rounds": 1},
    "plan": {"min_hypotheses": 5, "max_hypotheses": 14},
    "search": {"sources": ["europepmc", "pubmed", "openalex", "semantic_scholar"], "include_preprints": False,
               "max_queries_per_hypothesis": 12, "results_per_query": 100, "max_hits_per_query": 50000,
               "max_refinements": 3, "timeout_seconds": 90},
    "snowball": {"enabled": True, "max_per_seed": 100, "hops_per_round": 1},
    "fulltext": {"unpaywall": True, "pdf_parser": "auto", "grobid_url": None},
    "filters": {"languages": ["eng", "en"], "year_from": None, "year_to": None,
                "exclude_pub_types": ["Erratum", "Published Erratum", "Comment", "Editorial",
                                      "Retracted Publication", "Retraction of Publication"]},
    "screening": {"batch_size": 8, "abstract_chars": None, "recency_years": 15, "unknown_citation_score": 0.25,
                  "seed_bonus": 0.1, "population_bonus": 0.05,
                  "weights": {"relevance": 0.45, "design": 0.20, "recency": 0.10, "citations": 0.10,
                              "full_text": 0.10, "multi_query": 0.05}},
    "selection": {"min_relevance": 2, "min_papers_per_hypothesis": 3, "max_fulltext_papers_per_round": 25,
                  "full_text_tiebreak": 0.05},
    "extraction": {"sentence_id_format": "full", "max_pack_tokens": 32000, "max_packs_per_paper": None,
                   "max_claims_per_pack": 20, "max_rejected_ratio": 0.4},
    "confirm": {"per_hypothesis": 5, "max_claims": 100},
    "design_weights": {"guideline": 1.0, "systematic_review": 0.95, "rct": 0.85, "cohort": 0.7,
                       "diagnostic_accuracy": 0.7, "case_control": 0.6, "cross_sectional": 0.5,
                       "narrative_review": 0.45, "mechanistic": 0.4, "case_series": 0.35, "case_report": 0.25,
                       "opinion": 0.15, "other": 0.3},
    "weights": {"strength": {"weak": 0.5, "moderate": 1, "strong": 1.5},
                "bias": {"low": 0, "some": 0.25, "high": 0.5, "unclear": 0.35},
                "applicability": {"high": 1, "partial": 0.7, "low": 0.4},
                "case_match": {"high": 1, "partial": 0.7, "low": 0.4, "unknown": 0.6},
                "size": {"large": 1, "medium": 0.85, "small": 0.7, "unknown": 0.85},
                "type": {"background": 0.3, "mechanism": 0.8},
                "scope": {"full": 1, "partial": 0.85, "abstract_only": 0.6},
                "partial_verification": 0.6, "single_extractor": 0.9, "concern": 0.8},
    "independence": {"extra_claim_factor": 0.25, "paper_cap_factor": 1.5, "included_in_review_factor": 0.5},
    "direction": {"dominance_ratio": 2, "min_units": 2, "strong_unit_min": 0.5},
    "certainty": {"bias_share": 0.5, "inconsistency_ratio": 0.5, "indirectness_below": 0.6,
                  "imprecision_min_participants": 200},
    "outcome": {"min_units": 3},
    "review": {"max_iterations": 3},
    "figures": {"enabled": False},
    "loop": {"max_rounds": 5, "min_novelty": 1, "min_claims_per_hypothesis": 3, "max_new_hypotheses_per_round": 4},
    "guards": {"max_total_papers": 120, "max_screened": 6000, "max_wall_minutes": 480, "reserve": 0.2,
               "max_tokens": None, "max_cost_usd": None},
    "concurrency": {"max_parallel_tasks": 8, "openai": 16, "anthropic": 16, "ollama": 1},
}


def _balanced() -> dict:
    roles: dict[str, dict] = {}
    for name, spec in QUALITY_ROLES.items():
        members = [dict(m) for m in spec["members"]]
        for m in members:
            m["model"] = {ASTRA: SOL, FABLE: OPUS}.get(m["model"], m["model"])
            if m["effort"] == "xhigh":
                m["effort"] = "high"
        roles[name] = {"members": members}
    roles["read.claims"] = _role(_m(SOL, "high"))
    roles["read.appraisal"] = _role(_m(OPUS, "high"))
    return {
        "roles": roles,
        "strongest": {"openai": _m(SOL, "high"), "anthropic": _m(OPUS, "high")},
        "ensembles": {"dual_extraction": False, "whole_paper_pass": False, "adjudicate": False,
                      "appraisal_panel": False, "critics": ["safety"], "dual_knowledge_extraction": False},
        "grounding": {"max_sources_per_complaint": 3},
        "loop": {"max_rounds": 3},
        "selection": {"max_fulltext_papers_per_round": 15},
    }


def _economy() -> dict:
    single = _m(LUNA, "medium")
    roles = {name: {"members": [dict(single)], "fallback": [_m(HAIKU, "medium")]} for name in QUALITY_ROLES}
    # The safety panel always has two members, in every profile (B15).
    roles["safety"] = _role(_m(LUNA, "medium"), _m(HAIKU, "medium"), fallback=[_m(SOL, "medium")])
    return {
        "roles": roles,
        "strongest": {"openai": _m(LUNA, "medium"), "anthropic": _m(HAIKU, "medium")},
        "ensembles": {"dual_screening": False, "dual_extraction": False, "whole_paper_pass": False,
                      "verify_panel": 1, "adjudicate": False, "appraisal_panel": False, "planners": 1,
                      "gaps_panel": False, "critics": ["safety"], "final_check": False,
                      "dual_knowledge_extraction": False},
        "grounding": {"max_sources_per_complaint": 2},
        "loop": {"max_rounds": 1},
        "selection": {"max_fulltext_papers_per_round": 10},
        "extraction": {"max_packs_per_paper": 4},
        "snowball": {"enabled": False},
        "fulltext": {"unpaywall": False},
    }


DEV_OPENAI = "gpt-5.4-mini"
DEV_OLLAMA = "qwen3-coder:latest"


def _dev() -> dict:
    """Development setup (decision 2026-11-23): family A = gpt-5.4-mini, family B = a local Ollama model.

    Keeps the quality structure (same ensembles and cross-family checks) with cheaper models. Not for real
    cases: small/local models in critical roles have not passed E3, and pre-flight warns about it."""
    def swap(ref: dict) -> dict:
        family = provider_of(ref["model"])
        if family == "openai":
            effort = {"xhigh": "high", "max": "high", None: "medium"}.get(ref.get("effort"), ref.get("effort"))
            return _m(DEV_OPENAI, effort)
        return _m(DEV_OLLAMA, None)

    roles: dict[str, dict] = {}
    for name, spec in QUALITY_ROLES.items():
        members = [swap(m) for m in spec["members"]]
        fallback: list[dict] = []
        for ref in (swap(m) for m in spec.get("fallback", [])):
            if ref not in fallback:
                fallback.append(ref)
        roles[name] = {"members": members, "fallback": fallback}
    return {
        "roles": roles,
        "strongest": {"openai": _m(DEV_OPENAI, "high"), "ollama": _m(DEV_OLLAMA, None)},
        "providers": {"ollama": {"enabled": True, "num_ctx": 32768, "max_output_tokens": 8192,
                                 "idle_timeout_seconds": 300, "deadline_seconds": 1800}},
        "concurrency": {"ollama": 1, "openai": 8},
        # Ollama's 32k context and speed: smaller packs, fewer sources (development only).
        "grounding": {"max_sources_per_complaint": 3, "pack_tokens": 12000, "max_packs_per_source": 3,
                      "max_results_per_query": 15, "max_screened": 24},
    }


def _dev_openai() -> dict:
    """OpenAI-only development setup (decision 2026-11-25): every role on gpt-5.4-mini, family-A members at
    `high` effort and family-B members at `medium`, so panels and cross-checks keep two distinct members
    (independence `same_family`). Ollama returns in a later cycle. Not for real cases."""
    def swap(ref: dict, side: str) -> dict:
        return _m(DEV_OPENAI, "high" if side == "a" else "medium")

    roles: dict[str, dict] = {}
    for name, spec in QUALITY_ROLES.items():
        members = [swap(m, "a" if provider_of(m["model"]) == "openai" else "b") for m in spec["members"]]
        if len(members) == 2 and members[0] == members[1]:
            members[1] = _m(DEV_OPENAI, "medium" if members[0]["effort"] == "high" else "high")
        fallback: list[dict] = []
        for ref in (swap(m, "a" if provider_of(m["model"]) == "openai" else "b") for m in spec.get("fallback", [])):
            if ref not in fallback and ref not in members:
                fallback.append(ref)
        roles[name] = {"members": members, "fallback": fallback}
    return {
        "roles": roles,
        "strongest": {"openai": _m(DEV_OPENAI, "high")},
        "providers": {"ollama": {"enabled": False, "num_ctx": 32768}},
        "concurrency": {"openai": 8},
        "grounding": {"max_sources_per_complaint": 3, "max_packs_per_source": 4, "max_results_per_query": 15,
                      "max_screened": 24},
    }


def _dev_ollama() -> dict:
    """Local-only development setup (decision 2026-11-27): every role on the Ollama model, for comparing it with
    `dev_openai` on the same cases. One local model can't give two independent members (no effort levels, and the
    same request gets the same cached answer), so two-member roles run with one member (`reduced`). Not for real
    cases."""
    ref = _m(DEV_OLLAMA, None)
    roles = {name: {"members": [dict(ref) for _ in spec["members"]], "fallback": []}
             for name, spec in QUALITY_ROLES.items()}
    return {
        "roles": roles,
        "strongest": {"ollama": dict(ref)},
        "providers": {"ollama": {"enabled": True, "num_ctx": 32768, "max_output_tokens": 8192,
                                 "idle_timeout_seconds": 600, "deadline_seconds": 3600}},
        "concurrency": {"ollama": 1, "max_parallel_tasks": 2},
        # 32k context: packs of 12k tokens, as on `dev`.
        "grounding": {"max_sources_per_complaint": 3, "pack_tokens": 12000, "max_packs_per_source": 3,
                      "max_results_per_query": 15, "max_screened": 24},
        "extraction": {"max_pack_tokens": 12000},
    }


def local_model_overlay(model: str, *, thinking: bool = True, num_ctx: int = 32768, pack_tokens: int = 12000,
                        max_output_tokens: int = 12000) -> dict:
    """An overlay for `dev_ollama` that puts another local model in every role (model comparison, 2026-11-27).
    A model with a thinking mode gives two members (thinking on = `high`, off = `none`), like dev_openai's two
    efforts; without one, two-member roles run with one member (`reduced`). `num_ctx` sets the context that fits
    the GPU; packs and output must fit inside it."""
    a = _m(model, "high" if thinking else None)
    b = _m(model, "none" if thinking else None)
    roles = {name: {"members": [dict(a), dict(b)][:len(spec["members"])] if len(spec["members"]) > 1 else [dict(a)],
                    "fallback": []} for name, spec in QUALITY_ROLES.items()}
    return {"roles": roles, "strongest": {"ollama": dict(a)},
            "providers": {"ollama": {"num_ctx": num_ctx, "max_output_tokens": max_output_tokens}},
            "grounding": {"pack_tokens": pack_tokens}, "extraction": {"max_pack_tokens": pack_tokens}}


PROFILE_OVERLAYS: dict[str, Callable[[], dict]] = {"quality": dict, "balanced": _balanced, "economy": _economy,
                                                   "dev": _dev, "dev_openai": _dev_openai,
                                                   "dev_ollama": _dev_ollama}
PROFILE_DESCRIPTIONS = {
    "quality": "Strongest model for every judgment, full ensembles, up to 5 rounds (default).",
    "balanced": "sol and opus for most roles, single extraction, one critic, 3 rounds, 15 papers per round.",
    "economy": "One efficient model per role, no ensembles except the safety panel, 1 round, 10 papers.",
    "dev": "Development only: gpt-5.4-mini (family A) + a local Ollama model (family B), quality structure.",
    "dev_openai": "Development only: gpt-5.4-mini everywhere (high effort for family-A roles, medium for family-B roles), quality structure.",
    "dev_ollama": "Development only: the local Ollama model everywhere (one member per panel), quality structure.",
}


# ---------- validated model ----------

class ProvidersCfg(Strict):
    openai: dict[str, Any]
    anthropic: dict[str, Any]
    ollama: dict[str, Any]


class EnsemblesCfg(Strict):
    dual_screening: bool
    dual_extraction: bool
    whole_paper_pass: bool
    verify_panel: int = Field(ge=1, le=3)
    adjudicate: bool
    appraisal_panel: bool
    planners: int = Field(ge=1, le=3)
    gaps_panel: bool
    critics: list[Literal["evidence", "safety", "reasoning"]]
    final_check: bool
    dual_knowledge_extraction: bool

    @field_validator("critics")
    @classmethod
    def _safety_critic(cls, v: list[str]) -> list[str]:
        if "safety" not in v:
            raise ValueError("The safety critic always runs (B15).")
        return v


class PrivacyCfg(Strict):
    openai_project_shares_data: bool | None
    store_prompts: bool
    purge_payloads_after_days: int | None


class SafetyCfg(Strict):
    allow_continue_after_urgent: bool
    allow_continue_after_emergency: bool


class QuestionsCfg(Strict):
    max_per_round: int = Field(ge=1, le=30)
    max_rounds: int = Field(ge=0, le=5)
    max_gap_rounds: int = Field(ge=0, le=5)


class PlanCfg(Strict):
    min_hypotheses: int = Field(ge=1)
    max_hypotheses: int = Field(ge=1, le=40)

    @model_validator(mode="after")
    def _order(self) -> "PlanCfg":
        if self.min_hypotheses > self.max_hypotheses:
            raise ValueError("plan.min_hypotheses must not exceed plan.max_hypotheses.")
        return self


class GuardsCfg(Strict):
    max_total_papers: int = Field(ge=1)
    max_screened: int = Field(ge=1)
    max_wall_minutes: int = Field(ge=1)
    reserve: float = Field(ge=0, lt=1)
    max_tokens: int | None
    max_cost_usd: float | None


class ResearchSettings(Strict):
    profile: Literal["quality", "balanced", "economy", "dev", "dev_openai", "dev_ollama"]
    roles: dict[str, RoleSpec]
    strongest: dict[str, ModelRef]
    providers: ProvidersCfg
    ensembles: EnsemblesCfg
    scope: dict[str, Any]
    grounding: dict[str, Any]
    knowledge: dict[str, Any]
    privacy: PrivacyCfg
    autonomy: Literal["review_plan", "autonomous"]
    safety: SafetyCfg
    questions: QuestionsCfg
    plan: PlanCfg
    search: dict[str, Any]
    snowball: dict[str, Any]
    fulltext: dict[str, Any]
    filters: dict[str, Any]
    screening: dict[str, Any]
    selection: dict[str, Any]
    extraction: dict[str, Any]
    confirm: dict[str, Any]
    design_weights: dict[str, float]
    weights: dict[str, Any]
    independence: dict[str, float]
    direction: dict[str, float]
    certainty: dict[str, float]
    outcome: dict[str, int]
    review: dict[str, int]
    figures: dict[str, Any]
    loop: dict[str, Any]
    guards: GuardsCfg
    concurrency: dict[str, int]

    @model_validator(mode="after")
    def _floor(self) -> "ResearchSettings":
        missing = set(QUALITY_ROLES) - set(self.roles)
        if missing:
            raise ValueError(f"Missing roles: {', '.join(sorted(missing))}.")
        if len(self.roles["safety"].members) < 2:
            raise ValueError("The safety panel needs two models in every profile (B15).")
        families = {m.family for spec in self.roles.values() for m in spec.members}
        for family in families & {"openai", "anthropic", "ollama"}:
            if family not in self.strongest:
                raise ValueError(f"strongest.{family} is required (adjudication and confirmation).")
        return self


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge `overlay` into a copy of `base`. Lists and scalars replace; `roles.<name>` and `strongest` replace whole."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if key == "strongest" and isinstance(value, dict):
            out[key] = copy.deepcopy(value)  # a profile names its families' strongest models in full
        elif key == "roles" and isinstance(value, dict):
            roles = out.setdefault("roles", {})
            for name, spec in value.items():
                roles[name] = copy.deepcopy(spec)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def build(profile: str = "quality", overrides: dict | None = None) -> ResearchSettings:
    if profile not in PROFILE_OVERLAYS:
        raise ValueError(f"Unknown profile {profile!r}.")
    merged = deep_merge(QUALITY, PROFILE_OVERLAYS[profile]())
    merged["profile"] = profile
    if overrides:
        merged = deep_merge(merged, {k: v for k, v in overrides.items() if k != "profile"})
    return ResearchSettings.model_validate(merged)


def profiles() -> list[dict]:
    return [{"id": name, "description": PROFILE_DESCRIPTIONS[name], "settings": build(name).model_dump()}
            for name in PROFILE_OVERLAYS]


# ---------- role resolution ----------

class ResolvedRole(BaseModel):
    role: str
    members: list[ModelRef]
    independence: Literal["full", "same_family", "reduced", "single"]
    substitutions: list[str] = []


def _same_member(a: ModelRef, b: ModelRef) -> bool:
    """Two members are the same when model and effort match (one model at two efforts counts twice: dev_openai)."""
    return a.model == b.model and (a.effort or None) == (b.effort or None)


def resolve_role(settings: ResearchSettings, role: str, available: Callable[[ModelRef], bool]) -> ResolvedRole:
    """Pick models for a role: each unavailable member is replaced by the next unused available fallback.

    `independence` is `full` when the panel spans two families, `same_family` when every member is from one
    family (single-provider fallback, B6), `reduced` when members were lost, `single` for one-member roles."""
    spec = settings.roles[role]
    chosen: list[ModelRef] = []
    subs: list[str] = []
    pool = [m for m in spec.fallback]
    lost = 0
    for member in spec.members:
        if available(member) and not any(_same_member(c, member) for c in chosen):
            chosen.append(member)
            continue
        replacement = None
        while pool:
            candidate = pool.pop(0)
            if available(candidate) and not any(_same_member(c, candidate) for c in chosen):
                replacement = candidate
                break
        if replacement is None:
            lost += 1
            continue
        subs.append(f"{member.label()} → {replacement.label()}")
        chosen.append(replacement)
    if not chosen:
        raise LookupError(f"No available model for role {role!r}. Configure a provider key or change the role.")
    if len(spec.members) == 1:
        independence = "single"
    elif lost:
        independence = "reduced"
    elif len({m.family for m in chosen}) == 1:
        independence = "same_family"
    else:
        independence = "full"
    return ResolvedRole(role=role, members=chosen, independence=independence, substitutions=subs)


def strongest_other(settings: ResearchSettings, family: str, available: Callable[[ModelRef], bool]) -> ModelRef:
    """Strongest model of the family other than `family`; falls back to the same family if unavailable."""
    others = [f for f in settings.strongest if f != family]
    for fam in others + [family]:
        ref = settings.strongest.get(fam)
        if ref is not None and available(ref):
            return ref
    raise LookupError("No strongest model is available for adjudication.")


# ---------- store (data/research.json) ----------

class SettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def raw(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def load(self) -> ResearchSettings:
        data = self.raw()
        try:
            return build(data.get("profile", "quality"), data.get("overrides") or {})
        except (ValidationError, ValueError):
            return build("quality")

    def save(self, profile: str, overrides: dict | None) -> ResearchSettings:
        result = build(profile, overrides or {})  # raises on invalid input
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"profile": profile, "overrides": overrides or {}}, indent=2), "utf-8")
            tmp.replace(self.path)
        return result

    def reset(self) -> ResearchSettings:
        return self.save("quality", {})
