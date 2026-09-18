from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from open_webui.providers.base import BaseProviderAdapter
from open_webui.providers.gemini import GeminiAdapter
from open_webui.providers.groq import GroqAdapter
from open_webui.providers.openai import OpenAIAdapter

log = logging.getLogger(__name__)

# Check if debug logging is enabled via environment variable
ENABLE_DEBUG_LOGGING = os.getenv("LLM_ADAPTER_DEBUG", "false").lower() in ("true", "1", "yes")


def debug_trace(tag: str, **kwargs):
    """Sanitized debug logger that never exposes keys or sensitive auth data."""
    if not ENABLE_DEBUG_LOGGING:
        return

    # Sanitize any accidental sensitive keys
    safe_info = []
    for k, v in kwargs.items():
        if any(secret_term in k.lower() for secret_term in ("key", "auth", "token", "secret", "cookie")):
            safe_info.append(f"{k}=[REDACTED]")
        elif isinstance(v, (dict, list)):
            safe_info.append(f"{k}={len(v)} items")
        else:
            safe_info.append(f"{k}={v}")

    msg = f"[{tag}] " + " | ".join(safe_info)
    log.info(msg)
    print(msg)


@dataclass
class ModelCapabilities:
    tool_calling: bool = True
    streaming_tool_calls: bool = True
    reasoning: bool = False


class ModelCapabilityRegistry:
    """Registry mapping models and base URLs to provider adapters and capability definitions."""

    def __init__(self):
        self._openai_adapter = OpenAIAdapter()
        self._gemini_adapter = GeminiAdapter()
        self._groq_adapter = GroqAdapter()

        self._capabilities: dict[str, ModelCapabilities] = {}

    def get_adapter(self, base_url: Optional[str] = None, model_id: Optional[str] = None) -> BaseProviderAdapter:
        """Resolve the appropriate provider adapter given base_url and model_id.

        Does not replace user-selected model names.
        """
        url_lower = (base_url or "").lower()
        model_lower = (model_id or "").lower()

        # Check for Gemini endpoints or model identifiers
        if "generativelanguage.googleapis.com" in url_lower or "googleapis.com" in url_lower:
            return self._gemini_adapter
        if model_lower.startswith("gemini-") or model_lower.startswith("models/gemini-"):
            return self._gemini_adapter

        # Check for Groq endpoints or models
        if "api.groq.com" in url_lower or "groq" in url_lower:
            return self._groq_adapter
        if any(groq_model in model_lower for groq_model in ("gpt-oss-120b", "gpt-oss-20b", "qwen3.8", "llama-3", "mixtral")):
            if "openai.com" not in url_lower:
                return self._groq_adapter

        # Default to standard OpenAI adapter
        return self._openai_adapter

    def get_capabilities(self, model_id: str) -> ModelCapabilities:
        model_lower = model_id.lower()
        if model_lower in self._capabilities:
            return self._capabilities[model_lower]

        # Non-tool calling models (e.g. embeddings, whisper, prompt-guard)
        if any(unsupported in model_lower for unsupported in ("embedding", "whisper", "prompt-guard", "tts", "dall-e")):
            return ModelCapabilities(tool_calling=False, streaming_tool_calls=False)

        is_reasoning = any(r in model_lower for r in ("o1", "o3", "r1", "reasoning"))
        return ModelCapabilities(tool_calling=True, streaming_tool_calls=True, reasoning=is_reasoning)


# Global registry singleton
registry = ModelCapabilityRegistry()
