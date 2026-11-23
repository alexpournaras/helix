"""Ollama structured calls (PLAN B5.2, C5, C6).

POST /api/chat with stream: true, format: <schema>, and always options.num_ctx (Ollama's timeout is per
chunk, and its default context is small). `think` is sent only when /api/show lists the capability.
A failure mid-stream arrives as a final NDJSON line with an "error" field while the status stays 200.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from ...config import ProviderKeys
from .. import transport
from .base import ModelError, ModelInfo, ProviderResult, StructuredRequest, classify_http

DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaProvider:
    name = "ollama"

    def __init__(self, *, base_url: str | None = None, num_ctx: int = 32768, keep_alive: str = "5m",
                 capabilities: dict | None = None):
        self._base_url = base_url
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.capabilities = capabilities if capabilities is not None else {}  # model → ["completion", "thinking", …]

    @property
    def base_url(self) -> str:
        if self._base_url:
            return self._base_url
        try:
            from ...providers.ollama import load_config
            return load_config().base_url
        except Exception:  # noqa: BLE001 — config file unreadable: use the default address
            return DEFAULT_URL

    def has_key(self) -> bool:
        return True  # a local server needs no key; reachability is checked by probes

    def _headers(self) -> dict[str, str]:
        key = ProviderKeys().ollama_api_key.strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    async def _capabilities(self, client: httpx.AsyncClient, model: str) -> list[str]:
        if model not in self.capabilities:
            try:
                response = await client.post("/api/show", json={"model": model})
                caps = response.json().get("capabilities") if response.status_code < 400 else None
                self.capabilities[model] = list(caps or [])
            except (httpx.HTTPError, ValueError):
                self.capabilities[model] = []
        return self.capabilities[model]

    def body(self, req: StructuredRequest, capabilities: list[str]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}],
            "stream": True,
            "format": req.schema,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx, "num_predict": req.max_output_tokens},
        }
        if "thinking" in capabilities:
            body["think"] = req.effort not in (None, "none", "low")
        if req.temperature is not None:
            body["options"]["temperature"] = req.temperature
        return body

    async def complete(self, req: StructuredRequest) -> ProviderResult:
        timeout = httpx.Timeout(connect=10, read=req.idle_timeout, write=60, pool=30)
        try:
            return await asyncio.wait_for(self._stream(req, timeout), timeout=req.deadline)
        except asyncio.TimeoutError:
            raise ModelError("transient", f"Ollama did not finish within {req.deadline:.0f} s.")
        except httpx.TimeoutException:
            raise ModelError("transient", f"Ollama sent nothing for {req.idle_timeout:.0f} s.")
        except httpx.ConnectError:
            raise ModelError("config", f"Nothing is answering at {self.base_url}. Start Ollama.")
        except httpx.TransportError as exc:
            raise ModelError("transient", f"Ollama connection error: {exc.__class__.__name__}")

    async def _stream(self, req: StructuredRequest, timeout: httpx.Timeout) -> ProviderResult:
        parts: list[str] = []
        result = ProviderResult(texts=[], model=req.model)
        done = False
        async with transport.client(base_url=self.base_url, headers=self._headers(), timeout=timeout) as client:
            caps = await self._capabilities(client, req.model)
            async with client.stream("POST", "/api/chat", json=self.body(req, caps)) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    if response.status_code == 404:
                        raise ModelError("config", f"Ollama has no model {req.model!r}. Run: ollama pull {req.model}",
                                         status=404)
                    raise classify_http("Ollama", response.status_code, text, response)
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        message = str(chunk["error"])
                        kind = "overflow" if "context" in message.lower() else "transient"
                        raise ModelError(kind, f"Ollama: {message}")
                    content = (chunk.get("message") or {}).get("content")
                    if content:
                        parts.append(content)
                    if chunk.get("done"):
                        done = True
                        result.input_tokens = chunk.get("prompt_eval_count")
                        result.output_tokens = chunk.get("eval_count")
                        result.cached_input_tokens = 0
                        if chunk.get("done_reason") == "length":
                            result.stop = "truncated"
        if not done:
            raise ModelError("transient", "The Ollama stream ended before the reply completed.")
        text = "".join(parts)
        if text.strip():
            result.texts.append(text)
        return result

    async def list_models(self) -> list[ModelInfo]:
        try:
            async with transport.client(base_url=self.base_url, headers=self._headers(), timeout=10) as client:
                response = await client.get("/api/tags")
                response.raise_for_status()
                models = response.json().get("models") or []
        except httpx.HTTPError as exc:
            raise ModelError("config", f"Ollama is not reachable at {self.base_url}: {exc.__class__.__name__}")
        return [ModelInfo(id=m.get("name", ""), provider="ollama") for m in models]
