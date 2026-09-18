from __future__ import annotations

import copy
import json
import logging
import re
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


def _extract_qwen_xml_tool_calls(text: str) -> tuple[str, list[NormalizedToolCall]]:
    """Extract tool calls emitted inside <tool_call> tags by Qwen or Hermes models on Groq."""
    tool_calls: list[NormalizedToolCall] = []
    cleaned_text = text

    pattern = r"<tool_call>\s*(.*?)\s*</tool_call>"
    matches = list(re.finditer(pattern, text, re.DOTALL))
    if not matches:
        return cleaned_text, tool_calls

    for m in matches:
        raw_body = m.group(1).strip()
        parsed, _ = parse_and_repair_json(raw_body)
        if parsed and isinstance(parsed, dict) and "name" in parsed:
            name = parsed["name"]
            arguments = parsed.get("arguments", {})
            if not isinstance(arguments, dict):
                arguments, _ = parse_and_repair_json(arguments)
                if arguments is None:
                    arguments = {}

            call_id = f"call_groq_{uuid.uuid4().hex[:8]}"
            tool_calls.append(
                NormalizedToolCall(
                    id=call_id,
                    name=name,
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments),
                )
            )

    cleaned_text = re.sub(pattern, "", text, flags=re.DOTALL).strip()
    return cleaned_text, tool_calls


class GroqAdapter(BaseProviderAdapter):
    """Adapter for Groq Cloud LPU inference."""

    def __init__(self, provider_name: str = "groq"):
        super().__init__(provider_name)

    def normalize_tools(self, tools: list[NormalizedToolSchema]) -> list[dict[str, Any]]:
        normalized = []
        for tool in tools:
            schema = copy.deepcopy(tool.input_schema) if tool.input_schema else {"type": "object", "properties": {}}
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
        # Groq does not support stream_options
        prepared.pop("stream_options", None)
        return prepared

    def parse_response(self, response_data: dict[str, Any]) -> tuple[str, list[NormalizedToolCall]]:
        choices = response_data.get("choices", [])
        if not choices:
            return "", []

        message = choices[0].get("message", {})
        content = message.get("content") or ""
        tool_calls_data = message.get("tool_calls", [])
        normalized_calls: list[NormalizedToolCall] = []

        if tool_calls_data:
            for tc in tool_calls_data:
                call_id = tc.get("id") or f"call_groq_{uuid.uuid4().hex[:8]}"
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

        # If no native tool_calls, check for XML tool calls (e.g. Qwen on Groq)
        if "<tool_call>" in content:
            cleaned_text, extracted_calls = _extract_qwen_xml_tool_calls(content)
            if extracted_calls:
                return cleaned_text, extracted_calls

        return content, []

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

        # Reasoning delta
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

        # Native tool calls in delta
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
                        "id": call_id or f"call_groq_{uuid.uuid4().hex[:8]}",
                        "name": name or "",
                        "arguments_chunks": [],
                    }
                else:
                    acc = tool_calls_accumulator[idx]
                    if call_id and not acc["id"]:
                        acc["id"] = call_id
                    if name:
                        acc["name"] = name

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
            for idx in sorted(tool_calls_accumulator.keys()):
                acc = tool_calls_accumulator[idx]
                raw_args = "".join(acc["arguments_chunks"]) or "{}"
                parsed_args, _ = parse_and_repair_json(raw_args)
                if parsed_args is None:
                    parsed_args = {}

                call_id = acc["id"] or f"call_groq_{uuid.uuid4().hex[:8]}"
                completed_call = NormalizedToolCall(
                    id=call_id,
                    name=acc["name"],
                    arguments=parsed_args,
                    raw_arguments=raw_args,
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
        assistant_tool_calls = []
        for tc in tool_calls:
            call_id = tc.id or f"call_groq_{uuid.uuid4().hex[:8]}"
            assistant_tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else (tc.raw_arguments or "{}"),
                    },
                }
            )

        # CRUCIAL FOR GROQ:
        # If content is "" (empty string), Groq returns:
        # HTTP 400 Bad Request: Invalid content: expected null when tool_calls is provided.
        # It MUST be None so it serializes to `null`.
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
