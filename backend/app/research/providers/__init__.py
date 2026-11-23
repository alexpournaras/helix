"""Structured-call adapters for research (PLAN B5.2). The chat adapters live in app/providers/."""

from .anthropic import AnthropicProvider
from .base import ModelError, ModelInfo, ProviderResult, StructuredRequest
from .fake import FakeProvider
from .openai import OpenAIProvider


def default_providers(settings=None) -> dict:
    version = "2026-10-21"
    if settings is not None:
        version = settings.providers.anthropic.get("version", version)
    return {"openai": OpenAIProvider(), "anthropic": AnthropicProvider(version=version)}


__all__ = ["AnthropicProvider", "FakeProvider", "ModelError", "ModelInfo", "OpenAIProvider", "ProviderResult",
           "StructuredRequest", "default_providers"]
