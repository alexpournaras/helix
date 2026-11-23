"""Structured-call adapters for research (PLAN B5.2). The chat adapters live in app/providers/."""

from .anthropic import AnthropicProvider
from .base import ModelError, ModelInfo, ProviderResult, StructuredRequest
from .fake import FakeProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider


def default_providers(settings=None) -> dict:
    version = "2026-10-21"
    num_ctx = 32768
    if settings is not None:
        version = settings.providers.anthropic.get("version", version)
        num_ctx = int(settings.providers.ollama.get("num_ctx", num_ctx))
    return {"openai": OpenAIProvider(), "anthropic": AnthropicProvider(version=version),
            "ollama": OllamaProvider(num_ctx=num_ctx)}


__all__ = ["AnthropicProvider", "FakeProvider", "ModelError", "ModelInfo", "OllamaProvider", "OpenAIProvider", "ProviderResult",
           "StructuredRequest", "default_providers"]
