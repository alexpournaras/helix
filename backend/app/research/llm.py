"""complete_json(): the one way research steps call models (PLAN B5.1).

Routing by role → context guard → cache → provider schema → streamed provider call (adaptive
concurrency) → Pydantic + step validation → one repair call → truncation retry → logging of every attempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from . import strict, tokens
from .db import ResearchDB
from .prices import PriceTable
from .providers.base import ModelError, ProviderResult, StructuredRequest, limits
from .ratelimit import Limiters
from .settings import ModelRef, ResearchSettings, resolve_role

T = TypeVar("T", bound=BaseModel)

PROMPTS = Path(__file__).resolve().parent / "prompts"
SYSTEM_VERSION = "v1"
IN_CALL_RETRIES = 2
MAX_IN_CALL_WAIT = 30.0
EXCERPT_CHARS = 4000


class ContextOverflow(ModelError):
    def __init__(self, message: str):
        super().__init__("overflow", message)


@lru_cache(maxsize=None)
def base_system(version: str = SYSTEM_VERSION) -> str:
    return (PROMPTS / f"_system.{version}.md").read_text("utf-8").strip()


@lru_cache(maxsize=None)
def prompt(name: str) -> str:
    """Load `prompts/{name}.md`, e.g. prompt("intake.generate.v1")."""
    return (PROMPTS / f"{name}.md").read_text("utf-8").strip()


def material(text: str, **attrs: Any) -> str:
    """Wrap user or paper text as data (B1.14). Closing tags inside the text are neutralised."""
    attr = "".join(f' {k}="{v}"' for k, v in attrs.items() if v is not None)
    safe = text.replace("</material>", "</ material>")
    return f"<material{attr}>\n{safe}\n</material>"


@dataclass
class CallContext:
    db: ResearchDB
    settings: ResearchSettings
    providers: dict[str, Any]
    limiters: Limiters
    prices: PriceTable
    investigation_id: int | None = None
    task_id: int | None = None
    step: str | None = None
    available: Callable[[ModelRef], bool] | None = None
    sleep: Callable[[float], Any] = asyncio.sleep
    literature: Any = None  # LiteratureDB for fetches (None → the app's library); eval uses a temp one
    sources: Any = None  # research.sources.Sources override (tests)

    def is_available(self, ref: ModelRef) -> bool:
        if self.available is not None:
            return self.available(ref)
        provider = self.providers.get(ref.family)
        return provider is not None and provider.has_key()

    def with_task(self, *, task_id: int | None, step: str | None, investigation_id: int | None = None) -> "CallContext":
        return CallContext(self.db, self.settings, self.providers, self.limiters, self.prices,
                           investigation_id if investigation_id is not None else self.investigation_id,
                           task_id, step, self.available, self.sleep, self.literature, self.sources)


@dataclass
class CallResult:
    value: BaseModel
    model: ModelRef
    cached: bool
    text: str


def cache_key(ref: ModelRef, prompt_version: str, system: str, user: str, schema: dict) -> str:
    h = hashlib.sha256()
    for part in (ref.family, ref.model, prompt_version, ref.effort or "", system, user,
                 json.dumps(schema, sort_keys=True)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _max_output(ctx: CallContext, ref: ModelRef) -> int:
    cfg = getattr(ctx.settings.providers, ref.family, None) if ref.family in ("openai", "anthropic", "ollama") else None
    configured = 64000
    if isinstance(cfg, dict):
        configured = int(cfg.get("max_output_tokens") or cfg.get("max_tokens") or configured)
    if ref.family in ("fake", "ollama"):
        return configured
    _, cap = limits(ref.model)
    return min(configured, cap)


def _timeouts(ctx: CallContext, ref: ModelRef) -> tuple[float, float]:
    cfg = getattr(ctx.settings.providers, ref.family, None) if ref.family in ("openai", "anthropic", "ollama") else None
    if isinstance(cfg, dict):
        return float(cfg.get("idle_timeout_seconds", 120)), float(cfg.get("deadline_seconds", 1800))
    return 120.0, 1800.0


def parse(schema: type[T], texts: list[str], validate: Callable[[T], None] | None) -> tuple[T | None, str, str]:
    """Try candidate texts from last to first (C4). Returns (value, chosen_text, error)."""
    error = "The model returned no output."
    for text in reversed(texts):
        try:
            data = json.loads(text)
        except ValueError as exc:
            error = f"Invalid JSON: {exc}"
            continue
        try:
            value = schema.model_validate(strict.prepare(schema, data))
        except ValidationError as exc:
            error = "Schema validation failed: " + "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:12])
            continue
        if validate is not None:
            try:
                validate(value)
            except ValueError as exc:
                error = f"Check failed: {exc}"
                continue
        return value, text, ""
    return None, texts[-1] if texts else "", error


async def complete_json(ctx: CallContext, role: str, schema: type[T], system: str, user: str, *,
                        prompt_version: str, model: ModelRef | None = None,
                        validate: Callable[[T], None] | None = None) -> T:
    return (await complete_json_meta(ctx, role, schema, system, user, prompt_version=prompt_version, model=model,
                                     validate=validate)).value  # type: ignore[return-value]


async def complete_json_meta(ctx: CallContext, role: str, schema: type[T], system: str, user: str, *,
                             prompt_version: str, model: ModelRef | None = None,
                             validate: Callable[[T], None] | None = None) -> CallResult:
    ref = model or resolve_role(ctx.settings, role, ctx.is_available).members[0]
    provider = ctx.providers.get(ref.family)
    if provider is None:
        raise ModelError("config", f"No adapter for provider {ref.family!r}.")

    provider_schema = strict.to_provider_schema(schema)
    notes = strict.constraint_notes(schema)
    full_system = base_system()
    if system:
        full_system += "\n\n" + system
    if notes:
        full_system += "\n\nOutput constraints (enforced by the checker):\n" + "\n".join(f"- {n}" for n in notes)

    # 2. Context guard
    max_out = _max_output(ctx, ref)
    context_window, _ = limits(ref.model)
    if ref.family == "ollama":
        context_window = int(ctx.settings.providers.ollama.get("num_ctx") or context_window)
    estimate = tokens.estimate_many(full_system, user, json.dumps(provider_schema))
    if ref.family != "fake" and estimate > context_window - max_out:
        raise ContextOverflow(f"Input of about {estimate} tokens exceeds {ref.model}'s budget "
                              f"({context_window} − {max_out} output).")

    # 3. Cache
    key = cache_key(ref, prompt_version, full_system, user, provider_schema)
    cached_text = await asyncio.to_thread(ctx.db.cache_get, key)
    if cached_text is not None:
        value, text, _ = parse(schema, [cached_text], validate)
        if value is not None:
            await _log(ctx, role, ref, prompt_version, key, attempt="cache", cached=True, status="ok",
                       result=None, latency=0, response=text, user=user)
            return CallResult(value=value, model=ref, cached=True, text=text)

    idle, deadline = _timeouts(ctx, ref)
    request = StructuredRequest(model=ref.model, system=full_system, user=user, schema=provider_schema,
                                schema_name=strict.schema_name(schema), effort=ref.effort, max_output_tokens=max_out,
                                idle_timeout=idle, deadline=deadline)

    result = await _call(ctx, provider, request, role, ref, prompt_version, key, "first")
    if result.stop == "truncated":
        _, cap = limits(ref.model)
        bigger = max_out * 2 if ref.family in ("fake", "ollama") else min(max_out * 2, cap)
        if bigger <= max_out:
            raise ModelError("overflow", f"{ref.model} output was truncated at {max_out} tokens.")
        request.max_output_tokens = bigger
        result = await _call(ctx, provider, request, role, ref, prompt_version, key, "truncation_retry")
        if result.stop == "truncated":
            raise ModelError("overflow", f"{ref.model} output was truncated twice; split the input.")

    value, text, error = parse(schema, result.texts, validate)
    if value is None:
        repair_user = (f"{user}\n\n<previous_output>\n{text[:20000]}\n</previous_output>\n"
                       f"Your previous output was rejected. {error}\n"
                       "Return the complete corrected JSON object only, exactly matching the schema.")
        request.user = repair_user
        result = await _call(ctx, provider, request, role, ref, prompt_version, key, "repair")
        if result.stop == "truncated":
            raise ModelError("overflow", f"{ref.model} repair output was truncated.")
        value, text, error = parse(schema, result.texts, validate)
        if value is None:
            raise ModelError("format", f"{ref.model} output still invalid after repair: {error}")

    await asyncio.to_thread(ctx.db.cache_put, key, text)
    return CallResult(value=value, model=ref, cached=False, text=text)


async def _call(ctx: CallContext, provider: Any, request: StructuredRequest, role: str, ref: ModelRef,
                prompt_version: str, key: str, attempt: str) -> ProviderResult:
    limiter = ctx.limiters.get(ref.family)
    for retry in range(IN_CALL_RETRIES + 1):
        started = time.monotonic()
        await limiter.acquire()
        try:
            result = await provider.complete(request)
        except ModelError as exc:
            latency = int((time.monotonic() - started) * 1000)
            await _log(ctx, role, ref, prompt_version, key, attempt=attempt, cached=False, status="error",
                       result=None, latency=latency, error=exc, user=request.user)
            if exc.kind == "transient" and exc.status in (429, 529):
                limiter.on_throttle(exc.retry_after)
            wait = exc.retry_after if exc.retry_after is not None else 2.0 * (retry + 1)
            if exc.kind == "transient" and retry < IN_CALL_RETRIES and wait <= MAX_IN_CALL_WAIT:
                await ctx.sleep(wait)
                continue
            raise
        finally:
            await limiter.release()
        limiter.on_success()
        latency = int((time.monotonic() - started) * 1000)
        status = {"complete": "ok", "truncated": "truncated", "refusal": "refusal"}[result.stop]
        await _log(ctx, role, ref, prompt_version, key, attempt=attempt, cached=False, status=status, result=result,
                   latency=latency, response=result.texts[-1] if result.texts else None, user=request.user)
        if result.stop == "refusal":
            raise ModelError("refusal", f"{ref.model} refused: {result.refusal or 'no reason given'}")
        return result
    raise ModelError("transient", "Retries exhausted.")  # pragma: no cover


async def _log(ctx: CallContext, role: str, ref: ModelRef, prompt_version: str, key: str, *, attempt: str,
               cached: bool, status: str, result: ProviderResult | None, latency: int, response: str | None = None,
               error: ModelError | None = None, user: str = "") -> None:
    values: dict[str, Any] = {
        "investigation_id": ctx.investigation_id, "task_id": ctx.task_id, "step": ctx.step, "role": role,
        "provider": ref.family, "model": ref.model, "prompt_version": prompt_version, "cache_key": key,
        "cached": cached, "attempt_kind": attempt, "latency_ms": latency, "status": status,
        "response": response,
    }
    if result is not None:
        values.update(input_tokens=result.input_tokens, cached_input_tokens=result.cached_input_tokens,
                      output_tokens=result.output_tokens,
                      cost=ctx.prices.cost(ref.model, result.input_tokens, result.output_tokens,
                                           result.cached_input_tokens))
    elif cached:
        values.update(input_tokens=0, output_tokens=0, cost=0.0)
    if error is not None:
        values.update(error_kind=error.kind, error=error.message[:1000])
    if ctx.settings.privacy.store_prompts:
        values["request_excerpt"] = user[:EXCERPT_CHARS]
    await asyncio.to_thread(ctx.db.log_call, values)
