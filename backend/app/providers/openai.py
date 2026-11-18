"""OpenAI Responses API adapter for Helix chat.

The API key is read from ``OPENAI_API_KEY`` in the process environment or
``backend/.env`` and is never included in responses sent to the browser.
"""

import json
from typing import Any, AsyncIterator, Callable, Literal

import httpx
from pydantic import BaseModel, Field

from ..config import ProviderKeys
from ..store import integration_store

DEFAULT_URL = "https://api.openai.com/v1"
SUPPORTED_MODELS = (
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o-mini",
    "o3-mini",
    "o4-mini",
)


class OpenAIConfig(BaseModel):
    default_model: str = "gpt-5.4-mini"
    timeout_seconds: int = Field(default=120, ge=5, le=3600)


def load_config() -> OpenAIConfig:
    return integration_store.load("openai", OpenAIConfig)


def save_config(config: OpenAIConfig) -> OpenAIConfig:
    if config.default_model not in SUPPORTED_MODELS:
        raise ValueError("Choose one of the supported OpenAI models.")
    integration_store.save("openai", config)
    return config


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model: str = ""
    messages: list[ChatMessage] = Field(min_length=1)
    # Kept for the provider-neutral chat contract. OpenAI reasoning is not
    # exposed in Helix, and hidden chain-of-thought is never requested.
    think: bool | None = None


def _key() -> str:
    return ProviderKeys().openai_api_key.strip()


def _headers() -> dict[str, str]:
    key = _key()
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"} if key else {}


def _error_text(body: str) -> str:
    try:
        error = json.loads(body).get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error)
        return str(error or body)
    except (ValueError, AttributeError):
        return body.strip()[:500] or "no details"


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "Could not connect to the OpenAI API."
    if isinstance(exc, httpx.TimeoutException):
        return "The OpenAI API did not respond in time."
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "OpenAI rejected the API key. Check OPENAI_API_KEY in backend/.env."
        if code == 429:
            return f"OpenAI rate limit or quota reached: {_error_text(exc.response.text)}"
        return f"OpenAI returned an error ({code}): {_error_text(exc.response.text)}"
    return str(exc) or exc.__class__.__name__


def _http_error(code: int, body: str) -> str:
    if code in (401, 403):
        return "OpenAI rejected the API key. Check OPENAI_API_KEY in backend/.env."
    if code == 429:
        return f"OpenAI rate limit or quota reached: {_error_text(body)}"
    return f"OpenAI returned an error ({code}): {_error_text(body)}"


async def status(config: OpenAIConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "reachable": False,
        "api_key_present": bool(_key()),
        "models": [{"name": name} for name in SUPPORTED_MODELS],
        "error": None,
    }
    if not result["api_key_present"]:
        result["error"] = "Add OPENAI_API_KEY to backend/.env, then restart the backend."
        return result
    try:
        async with httpx.AsyncClient(base_url=DEFAULT_URL, headers=_headers(), timeout=10) as client:
            response = await client.get("/models")
            response.raise_for_status()
            response.json()  # Validate that the authenticated endpoint returned JSON.
        result["reachable"] = True
    except (httpx.HTTPError, ValueError) as exc:
        result["error"] = _describe(exc)
    return result


async def chat_stream(
    config: OpenAIConfig,
    request: ChatRequest,
    *,
    tools: list[dict[str, Any]] | None = None,
    execute_tool: Callable[[str, str | dict | None], dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield Helix's normalized start/content/done/error events."""
    model = request.model or config.default_model
    if model not in SUPPORTED_MODELS:
        yield {"type": "error", "message": "Choose one of the supported OpenAI models."}
        return
    if not _key():
        yield {"type": "error", "message": "Add OPENAI_API_KEY to backend/.env, then restart the backend."}
        return

    system = "\n\n".join(m.content for m in request.messages if m.role == "system").strip()
    input_messages = [m.model_dump() for m in request.messages if m.role != "system"]
    body: dict[str, Any] = {"model": model, "input": input_messages, "stream": True, "store": False}
    if system:
        body["instructions"] = system
    if tools and execute_tool:
        # The Responses API uses the function fields at the top level, while
        # Ollama uses the Chat Completions-style nested `function` object.
        body["tools"] = [{"type": "function", **tool["function"]} for tool in tools]

    timeout = httpx.Timeout(connect=10, read=config.timeout_seconds, write=30, pool=10)
    try:
        async with httpx.AsyncClient(base_url=DEFAULT_URL, headers=_headers(), timeout=timeout) as client:
            yield {"type": "start", "model": model}
            for _round in range(6):
                output_items: list[dict[str, Any]] = []
                completed: dict[str, Any] | None = None
                async with client.stream("POST", "/responses", json=body) as response:
                    if response.status_code >= 400:
                        text = (await response.aread()).decode("utf-8", "replace")
                        yield {"type": "error", "message": _http_error(response.status_code, text)}
                        return
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                            continue
                        if line.strip() or not data_lines:
                            continue
                        raw = "\n".join(data_lines)
                        data_lines.clear()
                        if raw == "[DONE]":
                            continue
                        event = json.loads(raw)
                        kind = event.get("type")
                        if kind == "response.output_text.delta":
                            yield {"type": "content", "text": event.get("delta", "")}
                        elif kind == "response.output_item.done" and isinstance(event.get("item"), dict):
                            output_items.append(event["item"])
                        elif kind == "response.completed":
                            completed = event.get("response") or {}
                        elif kind in ("error", "response.failed", "response.incomplete"):
                            error = event.get("error") or (event.get("response") or {}).get("error") or event
                            message = error.get("message") if isinstance(error, dict) else str(error)
                            yield {"type": "error", "message": f"OpenAI: {message or 'The response failed.'}"}
                            return

                if completed is None:
                    yield {"type": "error", "message": "The OpenAI stream ended before the response completed."}
                    return
                calls = [item for item in output_items if item.get("type") == "function_call"]
                if not calls:
                    yield {"type": "done", "stats": completed.get("usage")}
                    return
                if not execute_tool:
                    yield {"type": "error", "message": "OpenAI requested a tool that Helix cannot execute."}
                    return

                body["input"].extend(output_items)
                for call in calls:
                    name = call.get("name", "")
                    result = execute_tool(name, call.get("arguments"))
                    yield {"type": "tool", "name": name, "scope": result.get("scope"),
                           "error": result.get("error"), "audit": result.get("audit"), "sources": result.get("sources", [])}
                    body["input"].append({
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": json.dumps(result, ensure_ascii=False),
                    })
            yield {"type": "error", "message": "The model used too many consecutive literature tools. Try a more specific request."}
    except httpx.ReadTimeout:
        yield {"type": "error", "message": f"OpenAI sent no response for {config.timeout_seconds} seconds."}
    except (httpx.HTTPError, ValueError) as exc:
        yield {"type": "error", "message": _describe(exc)}
