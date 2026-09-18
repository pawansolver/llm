from __future__ import annotations

import abc
import copy
import json
import logging
from typing import Any, Optional

from open_webui.providers.models import (
    NormalizedStreamEvent,
    NormalizedToolCall,
    NormalizedToolResult,
    NormalizedToolSchema,
)

log = logging.getLogger(__name__)


class BaseProviderAdapter(abc.ABC):
    """Abstract base adapter for LLM providers."""

    def __init__(self, provider_name: str):
        self.provider_name = provider_name

    @abc.abstractmethod
    def normalize_tools(self, tools: list[NormalizedToolSchema]) -> list[dict[str, Any]]:
        """Convert normalized tool schemas into provider-specific tool declarations."""
        raise NotImplementedError

    @abc.abstractmethod
    def prepare_request_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Adjust or sanitize the request payload for this specific provider."""
        raise NotImplementedError

    @abc.abstractmethod
    def parse_response(self, response_data: dict[str, Any]) -> tuple[str, list[NormalizedToolCall]]:
        """Parse non-streaming response into assistant text content and tool calls."""
        raise NotImplementedError

    @abc.abstractmethod
    def parse_stream_chunk(
        self,
        chunk: dict[str, Any],
        tool_calls_accumulator: dict[str, Any],
    ) -> tuple[list[NormalizedStreamEvent], list[NormalizedToolCall]]:
        """Parse a single streaming chunk.

        Returns:
            (events_to_emit, completed_tool_calls_if_any)
        """
        raise NotImplementedError

    @abc.abstractmethod
    def format_continuation_messages(
        self,
        base_messages: list[dict[str, Any]],
        tool_calls: list[NormalizedToolCall],
        tool_results: list[NormalizedToolResult],
    ) -> list[dict[str, Any]]:
        """Format Turn 2 continuation messages with assistant tool calls and tool responses."""
        raise NotImplementedError

    def supports_tool_calling(self, model: str) -> bool:
        """Check if model supports native function/tool calling."""
        return True

    def supports_streaming_tool_calls(self, model: str) -> bool:
        """Check if model supports streaming tool calls."""
        return True
