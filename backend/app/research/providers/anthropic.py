"""Anthropic Messages API structured calls (PLAN B5.2, C28–C30).

POST /v1/messages with stream: true and output_config {format: json_schema, effort}. Thinking is adaptive
and counts toward max_tokens; thinking blocks are ignored and the JSON is read from text blocks.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ...config import ProviderKeys
from .. import transport
from .base import KNOWN_MODELS, ModelError, ModelInfo, ProviderResult, StructuredRequest, classify_http, sse_events

BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_VERSION = "2026-10-21"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class AnthropicProvider:
    name = "anthropic"
    quota_counted = True  # counted when a ceiling is configured for it (quota.providers)

    def __init__(self, *, base_url: str = BASE_URL, key: str | None = None, version: str = DEFAULT_VERSION,
                 capabilities: dict | None = None):
        self.base_url = base_url
        self._key = key
        self.version = version
        self.capabilities = capabilities or {}

    def key(self) -> str:
        return (self._key if self._key is not None else ProviderKeys().anthropic_api_key).strip()

    def has_key(self) -> bool:
        return bool(self.key())

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.key(), "anthropic-version": self.version, "content-type": "application/json"}

    def body(self, req: StructuredRequest) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": req.schema}}
        efforts = self.capabilities.get(req.model, {}).get("efforts", EFFORTS)
        if req.effort and req.effort in efforts:
            output_config["effort"] = req.effort
        max_tokens = req.max_output_tokens
        if req.effort in ("high", "xhigh", "max"):
            max_tokens = max(max_tokens, 64000)  # thinking counts toward max_tokens (C30)
        cap = KNOWN_MODELS.get(req.model, {}).get("max_output_tokens")
        if cap:
            max_tokens = min(max_tokens, cap)
        return {
            "model": req.model,
            "max_tokens": max_tokens,
            "system": req.system,
            "messages": [{"role": "user", "content": req.user}],
            "stream": True,
            "output_config": output_config,
        }

    async def complete(self, req: StructuredRequest) -> ProviderResult:
        if not self.has_key():
            raise ModelError("config", "Add ANTHROPIC_API_KEY to backend/.env.")
        timeout = httpx.Timeout(connect=15, read=req.idle_timeout, write=60, pool=30)
        try:
            return await asyncio.wait_for(self._stream(req, timeout), timeout=req.deadline)
        except asyncio.TimeoutError:
            raise ModelError("transient", f"Anthropic did not finish within {req.deadline:.0f} s.")
        except httpx.TimeoutException:
            raise ModelError("transient", f"Anthropic sent nothing for {req.idle_timeout:.0f} s.")
        except httpx.TransportError as exc:
            raise ModelError("transient", f"Anthropic connection error: {exc.__class__.__name__}")

    async def _stream(self, req: StructuredRequest, timeout: httpx.Timeout) -> ProviderResult:
        blocks: dict[int, dict[str, Any]] = {}
        result = ProviderResult(texts=[], model=req.model)
        stop_reason = None
        async with transport.client(base_url=self.base_url, headers=self._headers(), timeout=timeout) as client:
            async with client.stream("POST", "/messages", json=self.body(req)) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    raise classify_http("Anthropic", response.status_code, text, response)
                finished = False
                async for kind, event in sse_events(response):
                    if kind == "message_start":
                        message = event.get("message") or {}
                        usage = message.get("usage") or {}
                        result.input_tokens = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) \
                            + (usage.get("cache_creation_input_tokens") or 0)
                        result.cached_input_tokens = usage.get("cache_read_input_tokens") or 0
                        result.model = message.get("model") or req.model
                    elif kind == "content_block_start":
                        block = event.get("content_block") or {}
                        blocks[event.get("index", len(blocks))] = {"type": block.get("type"), "text": block.get("text") or ""}
                    elif kind == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            block = blocks.setdefault(event.get("index", 0), {"type": "text", "text": ""})
                            block["text"] += delta.get("text", "")
                    elif kind == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                        usage = event.get("usage") or {}
                        if usage.get("output_tokens") is not None:
                            result.output_tokens = usage["output_tokens"]
                    elif kind == "message_stop":
                        finished = True
                    elif kind == "error":
                        error = event.get("error") or {}
                        etype = error.get("type", "")
                        message = error.get("message", etype)
                        if etype in ("overloaded_error", "rate_limit_error", "api_error"):
                            raise ModelError("transient", f"Anthropic: {message}")
                        raise ModelError("config", f"Anthropic: {message}")
                if not finished:
                    raise ModelError("transient", "The Anthropic stream ended before the message completed.")
        text = "".join(b["text"] for _, b in sorted(blocks.items()) if b.get("type") == "text")
        if text.strip():
            result.texts.append(text)
        if stop_reason == "max_tokens":
            result.stop = "truncated"
        elif stop_reason == "refusal":
            result.stop = "refusal"
            result.refusal = "refused"
        return result

    async def list_models(self) -> list[ModelInfo]:
        if not self.has_key():
            raise ModelError("config", "Add ANTHROPIC_API_KEY to backend/.env.")
        async with transport.client(base_url=self.base_url, headers=self._headers(), timeout=20) as client:
            response = await client.get("/models", params={"limit": 100})
            if response.status_code >= 400:
                raise classify_http("Anthropic", response.status_code, response.text, response)
            data = response.json().get("data") or []
        out = []
        for item in data:
            mid = item.get("id", "")
            known = KNOWN_MODELS.get(mid, {})
            out.append(ModelInfo(
                id=mid, provider="anthropic",
                context_window=item.get("max_input_tokens") or known.get("context_window"),
                max_output_tokens=item.get("max_tokens") or known.get("max_output_tokens"),
                capabilities=item.get("capabilities") or {},
            ))
        return out
