from __future__ import annotations

import copy
import json
import logging
import uuid
from typing import Any, Optional

from open_webui.providers.base import BaseProviderAdapter
from open_webui.providers.json_repair import parse_and_repair_json
from open_webui.providers.models import (
    NormalizedStreamEvent,
    NormalizedToolCall,
    NormalizedToolResult,
    NormalizedToolSchema,
)

log = logging.getLogger(__name__)


class OpenAIAdapter(BaseProviderAdapter):
    """Adapter for native OpenAI and standard OpenAI-compatible endpoints."""

    def __init__(self, provider_name: str = "openai"):
        super().__init__(provider_name)

    def normalize_tools(self, tools: list[NormalizedToolSchema]) -> list[dict[str, Any]]:
        normalized = []
        for tool in tools:
            schema = tool.input_schema if tool.input_schema else {"type": "object", "properties": {}}
            normalized.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": schema,
                    },
                }
            )
        return normalized

    def prepare_request_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        prepared = copy.deepcopy(payload)
        model = str(prepared.get("model", "")).lower()
        if model.startswith("o1") or model.startswith("o3") or "reasoning" in model:
            if "max_tokens" in prepared and "max_completion_tokens" not in prepared:
                prepared["max_completion_tokens"] = prepared.pop("max_tokens")
        return prepared

    def parse_response(self, response_data: dict[str, Any]) -> tuple[str, list[NormalizedToolCall]]:
        choices = response_data.get("choices", [])
        if not choices:
            return "", []

        message = choices[0].get("message", {})
        content = message.get("content") or ""
        tool_calls_data = message.get("tool_calls", [])
        normalized_calls: list[NormalizedToolCall] = []

        for tc in tool_calls_data:
            call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            func = tc.get("function", {})
            name = func.get("name", "")
            raw_args = func.get("arguments", "{}")
            parsed_args, _ = parse_and_repair_json(raw_args)
            if parsed_args is None:
                parsed_args = {}

            normalized_calls.append(
                NormalizedToolCall(
                    id=call_id,
                    name=name,
                    arguments=parsed_args,
                    raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args),
                )
            )

        return content, normalized_calls

    def parse_stream_chunk(
        self,
        chunk: dict[str, Any],
        tool_calls_accumulator: dict[str, Any],
    ) -> tuple[list[NormalizedStreamEvent], list[NormalizedToolCall]]:
        events: list[NormalizedStreamEvent] = []
        completed_calls: list[NormalizedToolCall] = []

        choices = chunk.get("choices", [])
        if not choices:
            return events, completed_calls

        choice = choices[0]
        delta = choice.get("delta", {})
        finish_reason = choice.get("finish_reason")

        # Text delta
        if delta.get("content"):
            events.append(
                NormalizedStreamEvent(
                    type="text_delta",
                    content=delta["content"],
                )
            )

        # Reasoning / Thinking delta
        reasoning = (
            delta.get("reasoning_content")
            or delta.get("reasoning")
            or delta.get("thinking")
        )
        if reasoning:
            events.append(
                NormalizedStreamEvent(
                    type="reasoning_delta",
                    content=reasoning,
                )
            )

        # Tool calls delta
        delta_tool_calls = delta.get("tool_calls")
        if delta_tool_calls:
            for dtc in delta_tool_calls:
                idx = dtc.get("index", 0)
                call_id = dtc.get("id")
                func = dtc.get("function", {})
                name = func.get("name")
                args_delta = func.get("arguments")

                if idx not in tool_calls_accumulator:
                    tool_calls_accumulator[idx] = {
                        "id": call_id or f"call_{uuid.uuid4().hex[:8]}",
                        "name": name or "",
                        "arguments_chunks": [],
                        "extra_content": dtc.get("extra_content"),
                    }
                else:
                    acc = tool_calls_accumulator[idx]
                    if call_id and not acc["id"]:
                        acc["id"] = call_id
                    if name:
                        acc["name"] = name
                    if dtc.get("extra_content"):
                        acc["extra_content"] = dtc["extra_content"]

                if args_delta:
                    if not isinstance(args_delta, str):
                        args_delta = json.dumps(args_delta)
                    tool_calls_accumulator[idx]["arguments_chunks"].append(args_delta)

                events.append(
                    NormalizedStreamEvent(
                        type="tool_call_delta",
                        id=tool_calls_accumulator[idx]["id"],
                        name=tool_calls_accumulator[idx]["name"],
                        arguments_delta=args_delta,
                    )
                )

        if finish_reason in ("tool_calls", "function_call") or (finish_reason == "stop" and tool_calls_accumulator):
            events.append(NormalizedStreamEvent(type="finish", finish_reason=finish_reason))
            # Complete all accumulated tool calls
            for idx in sorted(tool_calls_accumulator.keys()):
                acc = tool_calls_accumulator[idx]
                raw_args = "".join(acc["arguments_chunks"]) or "{}"
                parsed_args, _ = parse_and_repair_json(raw_args)
                if parsed_args is None:
                    parsed_args = {}

                call_id = acc["id"] or f"call_{uuid.uuid4().hex[:8]}"
                completed_call = NormalizedToolCall(
                    id=call_id,
                    name=acc["name"],
                    arguments=parsed_args,
                    raw_arguments=raw_args,
                    extra_content=acc.get("extra_content"),
                )
                completed_calls.append(completed_call)
                events.append(
                    NormalizedStreamEvent(
                        type="tool_call_complete",
                        id=call_id,
                        name=acc["name"],
                        arguments=parsed_args,
                    )
                )
            tool_calls_accumulator.clear()

        return events, completed_calls

    def format_continuation_messages(
        self,
        base_messages: list[dict[str, Any]],
        tool_calls: list[NormalizedToolCall],
        tool_results: list[NormalizedToolResult],
    ) -> list[dict[str, Any]]:
        # Assistant message with tool_calls
        assistant_tool_calls = []
        for tc in tool_calls:
            call_dict = {
                "id": tc.id or f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else (tc.raw_arguments or "{}"),
                },
            }
            if tc.extra_content:
                call_dict["extra_content"] = tc.extra_content
            assistant_tool_calls.append(call_dict)

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": assistant_tool_calls,
        }

        tool_messages: list[dict[str, Any]] = []
        for tr in tool_results:
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tr.tool_call_id,
                    "content": tr.content if tr.content is not None else "",
                }
            )

        return [*base_messages, assistant_message, *tool_messages]
