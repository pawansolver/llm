from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class NormalizedToolSchema:
    """Provider-neutral representation of a tool exposed by an MCP server or builtin."""
    name: str
    description: str
    input_schema: dict[str, Any]
    mcp_server: Optional[str] = None
    mcp_tool: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema),
            "mcp_server": self.mcp_server,
            "mcp_tool": self.mcp_tool,
        }


@dataclass
class NormalizedToolCall:
    """Provider-neutral representation of an invoked tool call."""
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: Optional[str] = None
    extra_content: Optional[dict[str, Any]] = None  # E.g. Gemini thought_signature

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "arguments": copy.deepcopy(self.arguments),
        }
        if self.raw_arguments is not None:
            res["raw_arguments"] = self.raw_arguments
        if self.extra_content:
            res["extra_content"] = copy.deepcopy(self.extra_content)
        return res


@dataclass
class NormalizedToolResult:
    """Provider-neutral result of executing a tool."""
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False
    files: Optional[list[dict[str, Any]]] = None
    embeds: Optional[list[dict[str, Any]]] = None

    def to_dict(self) -> dict[str, Any]:
        res: dict[str, Any] = {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "content": self.content,
            "is_error": self.is_error,
        }
        if self.files:
            res["files"] = copy.deepcopy(self.files)
        if self.embeds:
            res["embeds"] = copy.deepcopy(self.embeds)
        return res


@dataclass
class NormalizedStreamEvent:
    """Provider-neutral event emitted during streaming."""
    type: str  # "text_delta", "reasoning_delta", "tool_call_delta", "tool_call_complete", "finish", "error"
    content: Optional[str] = None
    id: Optional[str] = None
    name: Optional[str] = None
    arguments_delta: Optional[str] = None
    arguments: Optional[dict[str, Any]] = None
    extra: Optional[dict[str, Any]] = None
    finish_reason: Optional[str] = None
