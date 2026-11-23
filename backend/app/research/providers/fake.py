"""Deterministic fake provider for tests and dry eval runs.

A handler receives the StructuredRequest and returns a dict (serialised as the JSON output), a string
(raw output text), a ProviderResult, or raises ModelError. Without a handler, it returns a minimal
object that satisfies the schema.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Callable

from .base import ModelInfo, ProviderResult, StructuredRequest

Handler = Callable[[StructuredRequest], Any]


def minimal(schema: dict) -> Any:
    """Smallest value that matches a provider schema (first enum value, empty arrays, nulls)."""
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            if option.get("type") == "null":
                return None
        return minimal(schema["anyOf"][0])
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {name: minimal(sub) for name, sub in schema.get("properties", {}).items()}
    if kind == "array":
        return []
    if kind == "string":
        return ""
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    return None


class FakeProvider:
    def __init__(self, name: str = "fake", handler: Handler | None = None, *, has_key: bool = True):
        self.name = name
        self.handler = handler
        self._has_key = has_key
        self.requests: list[StructuredRequest] = []

    def has_key(self) -> bool:
        return self._has_key

    async def complete(self, request: StructuredRequest) -> ProviderResult:
        self.requests.append(dataclasses.replace(request))
        out = self.handler(request) if self.handler else minimal(request.schema)
        if isinstance(out, ProviderResult):
            return out
        text = out if isinstance(out, str) else json.dumps(out)
        return ProviderResult(texts=[text], input_tokens=len(request.system + request.user) // 4,
                              cached_input_tokens=0, output_tokens=max(1, len(text) // 4), model=request.model)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id=f"{self.name}-model", provider=self.name, context_window=200_000, max_output_tokens=32_000)]
