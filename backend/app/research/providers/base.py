"""Provider adapter contract for structured calls (PLAN B5.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

ErrorKind = Literal["transient", "config", "overflow", "format", "refusal", "truncated", "quota"]


class ModelError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, retry_after: float | None = None, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after
        self.status = status


@dataclass
class StructuredRequest:
    model: str
    system: str
    user: str
    schema: dict
    schema_name: str
    effort: str | None = None
    max_output_tokens: int = 64000
    idle_timeout: float = 120
    deadline: float = 1800
    temperature: float | None = None


@dataclass
class ProviderResult:
    texts: list[str]  # candidate JSON texts, in output order (C4: use the last one that validates)
    stop: Literal["complete", "truncated", "refusal"] = "complete"
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    refusal: str | None = None
    model: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class ModelInfo:
    id: str
    provider: str
    context_window: int | None = None
    max_output_tokens: int | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def has_key(self) -> bool: ...

    async def complete(self, request: StructuredRequest) -> ProviderResult: ...

    async def list_models(self) -> list[ModelInfo]: ...


# Known limits (C3, C28). Model listing and probes refine these at run time.
KNOWN_MODELS: dict[str, dict[str, Any]] = {
    "gpt-5.4-mini": {"context_window": 400_000, "max_output_tokens": 128_000, "reasoning": True},
    "gpt-6.2-astra": {"context_window": 1_050_000, "max_output_tokens": 128_000, "reasoning": True},
    "gpt-6.2-sol": {"context_window": 1_050_000, "max_output_tokens": 128_000, "reasoning": True},
    "gpt-6.1-terra": {"context_window": 400_000, "max_output_tokens": 128_000, "reasoning": True},
    "gpt-6-luna": {"context_window": 400_000, "max_output_tokens": 128_000, "reasoning": True},
    "claude-fable-5-2": {"context_window": 1_000_000, "max_output_tokens": 128_000},
    "claude-opus-5-5": {"context_window": 1_000_000, "max_output_tokens": 128_000},
    "claude-sonnet-5-5": {"context_window": 1_000_000, "max_output_tokens": 128_000},
    "claude-haiku-5-5": {"context_window": 200_000, "max_output_tokens": 64_000},
}


def limits(model: str) -> tuple[int, int]:
    info = KNOWN_MODELS.get(model, {})
    return int(info.get("context_window", 128_000)), int(info.get("max_output_tokens", 16_000))


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def error_message(body: str) -> str:
    try:
        error = json.loads(body).get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)[:500]
        return str(error or body)[:500]
    except (ValueError, AttributeError):
        return body.strip()[:500] or "no details"


def classify_http(provider: str, status: int, body: str, response: httpx.Response | None = None) -> ModelError:
    message = error_message(body)
    after = retry_after(response) if response is not None else None
    lowered = message.lower()
    if status in (401, 403):
        return ModelError("config", f"{provider} rejected the API key ({status}): {message}", status=status)
    if status == 429 and ("quota" in lowered or "billing" in lowered or "insufficient" in lowered):
        return ModelError("config", f"{provider} quota exhausted: {message}", status=status)
    if status in (429, 529) or status >= 500:
        return ModelError("transient", f"{provider} is busy ({status}): {message}", retry_after=after, status=status)
    if status == 404 or ("model" in lowered and ("not found" in lowered or "does not exist" in lowered)):
        return ModelError("config", f"{provider}: unknown model ({status}): {message}", status=status)
    if "context" in lowered and ("too long" in lowered or "exceed" in lowered or "maximum" in lowered):
        return ModelError("overflow", f"{provider}: context exceeded: {message}", status=status)
    return ModelError("config", f"{provider} refused the request ({status}): {message}", status=status)


async def sse_events(response: httpx.Response):
    """Yield (event_name, data_dict) from a server-sent-events stream."""
    event_name = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line.strip() or not data_lines:
            continue
        raw = "\n".join(data_lines)
        data_lines = []
        name, event_name = event_name, None
        if raw == "[DONE]":
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        yield name or data.get("type"), data
    if data_lines:
        try:
            data = json.loads("\n".join(data_lines))
            yield event_name or data.get("type"), data
        except ValueError:
            pass
