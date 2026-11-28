"""OpenAI Responses API structured calls (PLAN B5.2, C3, C4).

POST /v1/responses with stream: true, store: false, text.format json_schema strict. The adapter streams
internally and returns every `output_text` item; the model layer uses the last one that validates.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ...config import ProviderKeys
from .. import transport
from .base import KNOWN_MODELS, ModelError, ModelInfo, ProviderResult, StructuredRequest, classify_http, sse_events

BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider:
    name = "openai"
    quota_counted = True  # real calls go through the daily ledger (quota.py)

    def __init__(self, *, base_url: str = BASE_URL, key: str | None = None, capabilities: dict | None = None):
        self.base_url = base_url
        self._key = key
        # model → {"efforts": [...], "temperature": bool}; filled by probes
        self.capabilities = capabilities or {}

    def key(self) -> str:
        return (self._key if self._key is not None else ProviderKeys().openai_api_key).strip()

    def has_key(self) -> bool:
        return bool(self.key())

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key()}", "Content-Type": "application/json"}

    def body(self, req: StructuredRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "instructions": req.system,
            "input": [{"role": "user", "content": req.user}],
            "stream": True,
            "store": False,
            "max_output_tokens": req.max_output_tokens,
            "text": {"format": {"type": "json_schema", "name": req.schema_name, "strict": True, "schema": req.schema}},
        }
        caps = self.capabilities.get(req.model, {})
        reasoning = caps.get("reasoning", KNOWN_MODELS.get(req.model, {}).get("reasoning", False))
        efforts = caps.get("efforts")
        if req.effort and reasoning and req.effort != "none" and (efforts is None or req.effort in efforts):
            body["reasoning"] = {"effort": req.effort}
        if req.temperature is not None and caps.get("temperature"):
            body["temperature"] = req.temperature
        return body

    async def complete(self, req: StructuredRequest) -> ProviderResult:
        if not self.has_key():
            raise ModelError("config", "Add OPENAI_API_KEY to backend/.env.")
        timeout = httpx.Timeout(connect=15, read=req.idle_timeout, write=60, pool=30)
        try:
            return await asyncio.wait_for(self._stream(req, timeout), timeout=req.deadline)
        except asyncio.TimeoutError:
            raise ModelError("transient", f"OpenAI did not finish within {req.deadline:.0f} s.")
        except httpx.TimeoutException:
            raise ModelError("transient", f"OpenAI sent nothing for {req.idle_timeout:.0f} s.")
        except httpx.TransportError as exc:
            raise ModelError("transient", f"OpenAI connection error: {exc.__class__.__name__}")

    async def _stream(self, req: StructuredRequest, timeout: httpx.Timeout) -> ProviderResult:
        texts: list[str] = []
        refusal: str | None = None
        result = ProviderResult(texts=texts, model=req.model)
        async with transport.client(base_url=self.base_url, headers=self._headers(), timeout=timeout) as client:
            async with client.stream("POST", "/responses", json=self.body(req)) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    raise classify_http("OpenAI", response.status_code, text, response)
                final: dict | None = None
                async for kind, event in sse_events(response):
                    if kind == "response.output_item.done":
                        item = event.get("item") or {}
                        if item.get("type") == "message":
                            for part in item.get("content") or []:
                                if part.get("type") == "output_text" and part.get("text"):
                                    texts.append(part["text"])
                                elif part.get("type") == "refusal":
                                    refusal = part.get("refusal") or "refused"
                    elif kind in ("response.completed", "response.incomplete"):
                        final = event.get("response") or {}
                        if kind == "response.incomplete":
                            # max_output_tokens, or content filtering mid-answer: either way the JSON is cut off.
                            result.stop = "truncated"
                    elif kind in ("error", "response.failed"):
                        error = event.get("error") or (event.get("response") or {}).get("error") or {}
                        message = error.get("message") if isinstance(error, dict) else str(error)
                        code = (error.get("code") or "") if isinstance(error, dict) else ""
                        if "rate_limit" in code or "server" in code or "overloaded" in str(message).lower():
                            raise ModelError("transient", f"OpenAI: {message}")
                        if "context" in code:
                            raise ModelError("overflow", f"OpenAI: {message}")
                        raise ModelError("config", f"OpenAI: {message or 'the response failed'}")
                if final is None:
                    raise ModelError("transient", "The OpenAI stream ended before the response completed.")
                if not texts:
                    # Some responses only carry output in the final object.
                    for item in final.get("output") or []:
                        for part in item.get("content") or []:
                            if part.get("type") == "output_text" and part.get("text"):
                                texts.append(part["text"])
                            elif part.get("type") == "refusal":
                                refusal = part.get("refusal") or "refused"
                usage = final.get("usage") or {}
                result.input_tokens = usage.get("input_tokens")
                result.cached_input_tokens = (usage.get("input_tokens_details") or {}).get("cached_tokens")
                result.output_tokens = usage.get("output_tokens")
                result.model = final.get("model") or req.model
        if refusal and not texts:
            result.stop = "refusal"
            result.refusal = refusal
        return result

    async def list_models(self) -> list[ModelInfo]:
        if not self.has_key():
            raise ModelError("config", "Add OPENAI_API_KEY to backend/.env.")
        async with transport.client(base_url=self.base_url, headers=self._headers(), timeout=20) as client:
            response = await client.get("/models")
            if response.status_code >= 400:
                raise classify_http("OpenAI", response.status_code, response.text, response)
            data = response.json().get("data") or []
        out = []
        for item in data:
            mid = item.get("id", "")
            known = KNOWN_MODELS.get(mid, {})
            out.append(ModelInfo(id=mid, provider="openai", context_window=known.get("context_window"),
                                 max_output_tokens=known.get("max_output_tokens")))
        return out
