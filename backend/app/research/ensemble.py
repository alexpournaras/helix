"""Ensemble policies (PLAN B6, B1.5): dual(), panel(), adjudicate(), vote recording.

Members run in parallel. If a member fails, the ensemble continues with the others and is marked
`independence: reduced` (B10); with no member left, the first error is raised and the step's own failure
rule applies. Votes are returned in a uniform shape so steps can store them (claim_votes or JSON columns).
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence, TypeVar

from pydantic import BaseModel

from .db import ResearchDB, now
from .llm import CallContext, complete_json_meta
from .providers.base import ModelError
from .settings import ModelRef, resolve_role, strongest_other

T = TypeVar("T", bound=BaseModel)


@dataclass
class Member:
    model: ModelRef
    value: BaseModel | None = None
    error: ModelError | None = None
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.value is not None


@dataclass
class EnsembleResult:
    role: str
    members: list[Member]
    independence: str
    substitutions: list[str] = field(default_factory=list)

    @property
    def values(self) -> list[BaseModel]:
        return [m.value for m in self.members if m.value is not None]

    @property
    def ok_members(self) -> list[Member]:
        return [m for m in self.members if m.ok]

    def votes(self, extract: Callable[[BaseModel], Any] | None = None) -> list[dict]:
        """Uniform vote records: {model, family, ok, value | error}."""
        out = []
        for m in self.members:
            vote: dict[str, Any] = {"model": m.model.model, "family": m.model.family, "effort": m.model.effort,
                                    "ok": m.ok}
            if m.ok:
                vote["value"] = extract(m.value) if extract else m.value.model_dump(mode="json")
            else:
                vote["error"] = f"{m.error.kind}: {m.error.message}" if m.error else "failed"
            out.append(vote)
        return out


async def _one(ctx: CallContext, role: str, ref: ModelRef, schema: type[T], system: str, user: str,
               prompt_version: str, validate) -> Member:
    try:
        result = await complete_json_meta(ctx, role, schema, system, user, prompt_version=prompt_version,
                                          model=ref, validate=validate)
        return Member(model=ref, value=result.value, cached=result.cached)
    except ModelError as exc:
        if exc.kind == "quota":
            raise  # the day's ceiling stops the whole task; it must not look like a failed member (B17.1)
        return Member(model=ref, error=exc)


async def panel(ctx: CallContext, role: str, schema: type[T], system: str, user: str | Sequence[str], *,
                prompt_version: str, validate: Callable[[T], None] | None = None,
                members: Sequence[ModelRef] | None = None, avoid_models: Iterable[str] = ()) -> EnsembleResult:
    """Run every member of `role` (or `members`) on the same input. `user` may be one string per member.

    `avoid_models` (e.g. a claim's extractor) are swapped for the role's next available fallback when possible."""
    if members is None:
        resolved = resolve_role(ctx.settings, role, ctx.is_available)
        chosen = list(resolved.members)
        independence, subs = resolved.independence, list(resolved.substitutions)
        avoid = set(avoid_models)
        if avoid:
            pool = [f for f in ctx.settings.roles[role].fallback
                    if ctx.is_available(f) and f.model not in avoid and all(c.model != f.model for c in chosen)]
            for i, ref in enumerate(chosen):
                if ref.model in avoid and pool:
                    replacement = pool.pop(0)
                    subs.append(f"{ref.label()} → {replacement.label()} (avoid extractor)")
                    chosen[i] = replacement
            if len(chosen) > 1 and len({c.family for c in chosen}) == 1 and independence == "full":
                independence = "same_family"
    else:
        chosen = list(members)
        independence = "single" if len(chosen) == 1 else (
            "full" if len({c.family for c in chosen}) > 1 else "same_family")
        subs = []
    users = list(user) if not isinstance(user, str) else [user] * len(chosen)
    if len(users) != len(chosen):
        raise ValueError("One user message per member is required.")
    results = await asyncio.gather(*[
        _one(ctx, role, ref, schema, system, u, prompt_version, validate) for ref, u in zip(chosen, users)])
    ok = [m for m in results if m.ok]
    if not ok:
        raise results[0].error or ModelError("format", f"Every member of {role} failed.")
    if len(ok) < len(results):
        independence = "reduced"
    return EnsembleResult(role=role, members=list(results), independence=independence, substitutions=subs)


async def dual(ctx: CallContext, role: str, schema: type[T], system: str, user: str | Sequence[str], *,
               prompt_version: str, validate: Callable[[T], None] | None = None) -> EnsembleResult:
    """Two independent members (e.g. dual screening, dual extraction)."""
    return await panel(ctx, role, schema, system, user, prompt_version=prompt_version, validate=validate)


async def single(ctx: CallContext, role: str, schema: type[T], system: str, user: str, *, prompt_version: str,
                 validate: Callable[[T], None] | None = None) -> Member:
    resolved = resolve_role(ctx.settings, role, ctx.is_available)
    result = await complete_json_meta(ctx, role, schema, system, user, prompt_version=prompt_version,
                                      model=resolved.members[0], validate=validate)
    return Member(model=result.model, value=result.value, cached=result.cached)


async def adjudicate(ctx: CallContext, schema: type[T], system: str, user: str, *, extractor_family: str,
                     prompt_version: str, role: str = "adjudicate",
                     validate: Callable[[T], None] | None = None) -> Member:
    """The strongest model of the family that did not extract (B6). Its verdict is final."""
    ref = strongest_other(ctx.settings, extractor_family, ctx.is_available)
    result = await complete_json_meta(ctx, role, schema, system, user, prompt_version=prompt_version, model=ref,
                                      validate=validate)
    return Member(model=ref, value=result.value, cached=result.cached)


# ---------- combination policies ----------

SAFETY_ORDER = ("none", "routine_review", "urgent", "emergency")
RISK_ORDER = ("low", "some", "unclear", "high")
VERDICT_CAUTION = ("supported", "partial", "unsupported")


def most_cautious(values: Iterable[str], order: Sequence[str]) -> str:
    """The value latest in `order` (unknown values are treated as the most cautious)."""
    best_idx, best = -1, None
    for value in values:
        idx = order.index(value) if value in order else len(order)
        if idx > best_idx:
            best_idx, best = idx, value
    if best is None:
        raise ValueError("No values to combine.")
    return best


def include_if_any(decisions: Iterable[str]) -> str:
    return "include" if any(d == "include" for d in decisions) else "exclude"


def union(lists: Iterable[Iterable[Any]]) -> list[Any]:
    out: list[Any] = []
    for items in lists:
        for item in items:
            if item not in out:
                out.append(item)
    return out


# ---------- vote recording ----------

def record_claim_votes(db: ResearchDB, conn: sqlite3.Connection, claim_id: int, stage: str,
                       votes: Iterable[dict]) -> None:
    for vote in votes:
        db.insert(conn, "claim_votes", {
            "claim_id": claim_id, "stage": stage, "model": vote["model"], "verdict": vote["verdict"],
            "checks": vote.get("checks"), "reason": vote.get("reason"), "created_at": now(),
        })
