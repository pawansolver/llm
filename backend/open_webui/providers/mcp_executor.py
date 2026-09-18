from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from open_webui.providers.models import NormalizedToolCall, NormalizedToolResult

log = logging.getLogger(__name__)


def resolve_mcp_tool_name(tool_name: str, available_tools: dict[str, Any]) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Resolve an incoming tool name to its registered entry in available_tools.

    Supports:
    1. Exact match: 'diffy__list_skills' -> available_tools['diffy__list_skills']
    2. Legacy match: 'diffy_list_skills' -> available_tools['diffy__list_skills']
    3. Unprefixed match: 'list_skills' -> matches any tool ending with '__list_skills' or '_list_skills'
    4. Suffix match: 'list_skills' matches 'diffy__list_skills'
    """
    if tool_name in available_tools:
        return tool_name, available_tools[tool_name]

    # Try mapping double to single underscore
    single_under = tool_name.replace("__", "_")
    if single_under in available_tools:
        return single_under, available_tools[single_under]

    # Try mapping single to double underscore
    if "_" in tool_name and "__" not in tool_name:
        parts = tool_name.split("_", 1)
        double_under = f"{parts[0]}__{parts[1]}"
        if double_under in available_tools:
            return double_under, available_tools[double_under]

    # Suffix / unprefixed match
    for reg_name, tool_entry in available_tools.items():
        if reg_name.endswith(f"__{tool_name}") or reg_name.endswith(f"_{tool_name}"):
            return reg_name, tool_entry
        if tool_name.endswith(f"__{reg_name}") or tool_name.endswith(f"_{reg_name}"):
            return reg_name, tool_entry

    return None, None


class MCPExecutor:
    """Provider-neutral executor for Model Context Protocol (MCP) tools."""

    def __init__(self, timeout_seconds: float = 30.0):
        self.timeout_seconds = timeout_seconds

    async def execute(
        self,
        tool_call: NormalizedToolCall,
        available_tools: dict[str, Any],
        extra_context: Optional[dict[str, Any]] = None,
    ) -> NormalizedToolResult:
        """Execute a normalized tool call and return a normalized result."""
        call_id = tool_call.id
        raw_name = tool_call.name
        params = tool_call.arguments or {}

        resolved_name, tool_entry = resolve_mcp_tool_name(raw_name, available_tools)
        if not tool_entry:
            log.warning("Tool '%s' not found in available tools.", raw_name)
            return NormalizedToolResult(
                tool_call_id=call_id,
                tool_name=raw_name,
                content=f"Error: Tool '{raw_name}' not found. Available tools: {list(available_tools.keys())}",
                is_error=True,
            )

        spec = tool_entry.get("spec", {})
        allowed_params = spec.get("parameters", {}).get("properties", {}).keys()
        if allowed_params:
            filtered_params = {k: v for k, v in params.items() if k in allowed_params}
        else:
            filtered_params = params

        callable_func = tool_entry.get("callable")
        direct = tool_entry.get("direct", False)
        client = tool_entry.get("client")

        try:
            async with asyncio.timeout(self.timeout_seconds):
                if callable_func:
                    result = await callable_func(**filtered_params)
                elif client:
                    # Fallback to direct client call if tool name can be extracted
                    mcp_tool_name = tool_entry.get("mcp_tool") or (
                        resolved_name.split("__", 1)[-1] if "__" in resolved_name else resolved_name
                    )
                    result = await client.call_tool(mcp_tool_name, filtered_params)
                else:
                    return NormalizedToolResult(
                        tool_call_id=call_id,
                        tool_name=raw_name,
                        content=f"Error: Tool '{raw_name}' has no executable handler.",
                        is_error=True,
                    )

            # Format result content string
            if isinstance(result, (dict, list)):
                content_str = json.dumps(result, ensure_ascii=False)
            elif result is None:
                content_str = "Success (no output)"
            else:
                content_str = str(result)

            return NormalizedToolResult(
                tool_call_id=call_id,
                tool_name=raw_name,
                content=content_str,
                is_error=False,
            )

        except asyncio.TimeoutError:
            log.error("MCP tool execution timed out after %ss: %s", self.timeout_seconds, raw_name)
            return NormalizedToolResult(
                tool_call_id=call_id,
                tool_name=raw_name,
                content=f"Error: Tool '{raw_name}' execution timed out after {self.timeout_seconds}s.",
                is_error=True,
            )
        except Exception as exc:
            log.exception("MCP tool execution failed for '%s': %s", raw_name, exc)
            return NormalizedToolResult(
                tool_call_id=call_id,
                tool_name=raw_name,
                content=f"Error executing tool '{raw_name}': {str(exc)}",
                is_error=True,
            )
