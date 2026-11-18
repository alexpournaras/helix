"""Ollama integration: config, status and streaming chat.

API reference: https://docs.ollama.com/api
- GET  /api/tags     installed models
- GET  /api/ps       models currently loaded in memory (local servers only)
- GET  /api/version  server version
- POST /api/chat     chat; streams NDJSON, one JSON object per line. A failure mid-stream
                     arrives as a final line with an "error" field while the status stays 200.
The same API is served by ollama.com for cloud models, authenticated with
`Authorization: Bearer $OLLAMA_API_KEY`.
"""

import asyncio
import json
from typing import Any, AsyncIterator, Literal

import httpx
from pydantic import BaseModel, Field, field_validator

from ..config import ProviderKeys
from ..store import integration_store, store

DEFAULT_URL = "http://127.0.0.1:11434"


class OllamaConfig(BaseModel):
    base_url: str = DEFAULT_URL
    default_model: str = ""
    keep_alive: str = "5m"  # how long a model stays loaded after a request; "0" unloads immediately
    temperature: float | None = Field(default=None, ge=0, le=2)
    num_ctx: int | None = Field(default=None, ge=256, le=1_048_576)  # context window, tokens
    timeout_seconds: int = Field(default=120, ge=5, le=3600)  # max wait between streamed chunks

    @field_validator("base_url")
    @classmethod
    def _clean_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value.endswith("/api"):
            value = value[: -len("/api")]
        if not value.startswith(("http://", "https://")):
            raise ValueError("The address must start with http:// or https://")
        return value

    @field_validator("keep_alive")
    @classmethod
    def _clean_keep_alive(cls, value: str) -> str:
        value = value.strip()
        return value or "5m"


def load_config() -> OllamaConfig:
    # Older versions kept the address in workspace settings; carry it over once.
    legacy = store.raw().get("ollama_base_url")
    return integration_store.load("ollama", OllamaConfig, {"base_url": legacy} if legacy else None)


def save_config(config: OllamaConfig) -> OllamaConfig:
    integration_store.save("ollama", config)
    return config


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model: str = ""  # empty: use the configured default
    messages: list[ChatMessage] = Field(min_length=1)
    think: bool | None = None


def _headers() -> dict[str, str]:
    key = ProviderKeys().ollama_api_key.strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _describe(exc: Exception, url: str) -> str:
    if isinstance(exc, httpx.ConnectError):
        return f"Nothing is answering at {url}. Start Ollama, or check the address."
    if isinstance(exc, httpx.TimeoutException):
        return f"{url} didn't respond in time."
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return f"{url} refused the request ({code}). Check OLLAMA_API_KEY in backend/.env."
        return f"{url} answered with an error ({code}): {_error_text(exc.response.text)}"
    if isinstance(exc, httpx.InvalidURL):
        return f"{url} isn't a valid address."
    return str(exc) or exc.__class__.__name__


def _error_text(body: str) -> str:
    try:
        return json.loads(body).get("error") or body
    except (ValueError, AttributeError):
        return body.strip()[:300] or "no details"


def _model_summary(m: dict[str, Any]) -> dict[str, Any]:
    details = m.get("details") or {}
    return {
        "name": m.get("name") or m.get("model", ""),
        "size": m.get("size"),
        "modified_at": m.get("modified_at"),
        "family": details.get("family"),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "remote": bool(m.get("remote_host")),
    }


async def status(config: OllamaConfig) -> dict[str, Any]:
    url = config.base_url
    result: dict[str, Any] = {
        "url": url,
        "reachable": False,
        "version": None,
        "models": [],
        "running": [],
        "api_key_present": bool(_headers()),
        "error": None,
    }
    try:
        async with httpx.AsyncClient(base_url=url, headers=_headers(), timeout=4) as client:
            tags = await client.get("/api/tags")
            tags.raise_for_status()
            result["reachable"] = True
            result["models"] = sorted(
                (_model_summary(m) for m in tags.json().get("models", [])), key=lambda m: m["name"]
            )
            # Optional extras: ollama.com doesn't serve every endpoint a local server does.
            version, ps = await asyncio.gather(client.get("/api/version"), client.get("/api/ps"), return_exceptions=True)
            if isinstance(version, httpx.Response) and version.is_success:
                v = version.json().get("version")
                result["version"] = v if v and v != "0.0.0" else None  # ollama.com reports a placeholder
            if isinstance(ps, httpx.Response) and ps.is_success:
                result["running"] = [
                    {"name": m.get("name") or m.get("model"), "size_vram": m.get("size_vram"), "expires_at": m.get("expires_at")}
                    for m in ps.json().get("models", [])
                ]
    except (httpx.HTTPError, ValueError) as exc:
        result["error"] = _describe(exc, url)
    return result


async def chat_stream(config: OllamaConfig, request: ChatRequest) -> AsyncIterator[dict[str, Any]]:
    """Yield normalised events: thinking / content / done / error."""
    model = request.model or config.default_model
    if not model:
        yield {"type": "error", "message": "Choose a model first."}
        return

    body: dict[str, Any] = {
        "model": model,
        "messages": [m.model_dump() for m in request.messages],
        "stream": True,
        "keep_alive": int(config.keep_alive) if config.keep_alive.lstrip("-").isdigit() else config.keep_alive,
    }
    options = {k: v for k, v in {"temperature": config.temperature, "num_ctx": config.num_ctx}.items() if v is not None}
    if options:
        body["options"] = options
    if request.think is not None:
        body["think"] = request.think

    timeout = httpx.Timeout(connect=5, read=config.timeout_seconds, write=30, pool=5)
    try:
        async with httpx.AsyncClient(base_url=config.base_url, headers=_headers(), timeout=timeout) as client:
            async with client.stream("POST", "/api/chat", json=body) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    yield {"type": "error", "message": f"Ollama: {_error_text(text)} ({response.status_code})"}
                    return
                yield {"type": "start", "model": model}
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        yield {"type": "error", "message": f"Ollama: {chunk['error']}"}
                        return
                    message = chunk.get("message") or {}
                    if message.get("thinking"):
                        yield {"type": "thinking", "text": message["thinking"]}
                    if message.get("content"):
                        yield {"type": "content", "text": message["content"]}
                    if chunk.get("done"):
                        yield {
                            "type": "done",
                            "done_reason": chunk.get("done_reason"),
                            "stats": {
                                k: chunk.get(k)
                                for k in ("total_duration", "load_duration", "prompt_eval_count", "eval_count", "eval_duration")
                            },
                        }
                        return
    except httpx.ReadTimeout:
        yield {"type": "error", "message": (
            f"Ollama sent no response for {config.timeout_seconds} seconds. "
            "The model may still be loading or processing your message. "
            "Open Configure and increase the timeout, or try a smaller model."
        )}
    except (httpx.HTTPError, ValueError) as exc:
        yield {"type": "error", "message": _describe(exc, config.base_url)}
