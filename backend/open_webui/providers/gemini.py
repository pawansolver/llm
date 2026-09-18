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


def _sanitize_schema_for_gemini(schema: dict[str, Any]) -> dict[str, Any]:
    """Sanitize JSON schema for Google Gemini OpenAPI requirements.

    Gemini function declarations are strict about OpenAPI 3.0 schema rules:
    - Must specify type: 'object'
    - Disallows $schema, additionalProperties
    - Disallows empty type definitions
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    cleaned = copy.deepcopy(schema)
    cleaned.pop("$schema", None)
    cleaned.pop("additionalProperties", None)

    if "type" not in cleaned:
        cleaned["type"] = "object"

    if cleaned.get("type") == "object":
        properties = cleaned.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        cleaned_props = {}
        for prop_name, prop_def in properties.items():
            if isinstance(prop_def, dict):
                prop_copy = copy.deepcopy(prop_def)
                prop_copy.pop("$schema", None)
                prop_copy.pop("additionalProperties", None)
                if "type" not in prop_copy and "properties" in prop_copy:
                    prop_copy["type"] = "object"
                cleaned_props[prop_name] = prop_copy
            else:
                cleaned_props[prop_name] = {"type": "string"}
        cleaned["properties"] = cleaned_props

    return cleaned


class GeminiAdapter(BaseProviderAdapter):
    """Adapter for Google Gemini models via OpenAI-compatible or native endpoints."""

    def __init__(self, provider_name: str = "gemini"):
        super().__init__(provider_name)

    def normalize_tools(self, tools: list[NormalizedToolSchema]) -> list[dict[str, Any]]:
        normalized = []
        for tool in tools:
            # Gemini tool name regex: ^[a-zA-Z_][a-zA-Z0-9_]*$, max 63 characters
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", tool.name)[:63]
            sanitized_schema = _sanitize_schema_for_gemini(tool.input_schema)

            normalized.append(
                {
                    "type": "function",
                    "function": {
                        "name": safe_name,
                        "description": tool.description or "",
                        "parameters": sanitized_schema,
                    },
                }
            )
        return normalized

    def prepare_request_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        prepared = copy.deepcopy(payload)

        # Normalize model ID: strip provider prefixes, but DO NOT overwrite user model choice
        model = prepared.get("model", "")
        if model.startswith("openai/"):
            model = model[len("openai/"):]
        prepared["model"] = model

        return prepared

    def parse_response(self, response_data: dict[str, Any]) -> tuple[str, list[NormalizedToolCall]]:
        # Check standard OpenAI format (Gemini's v1beta/openai endpoint)
        choices = response_data.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content") or ""
            tool_calls_data = message.get("tool_calls", [])
            normalized_calls: list[NormalizedToolCall] = []

            for tc in tool_calls_data:
                # Gemini sometimes omits tool call id or provides empty string
                call_id = tc.get("id")
                if not call_id or not call_id.strip():
                    call_id = f"call_gemini_{uuid.uuid4().hex[:8]}"

                func = tc.get("function", {})
                name = func.get("name", "")
                raw_args = func.get("arguments", "{}")
                parsed_args, _ = parse_and_repair_json(raw_args)
                if parsed_args is None:
                    parsed_args = {}

                extra = tc.get("extra_content") or {}
                # Capture thought_signature from Gemini message if present
                if message.get("extra_content"):
                    extra.update(message["extra_content"])

                normalized_calls.append(
                    NormalizedToolCall(
                        id=call_id,
                        name=name,
                        arguments=parsed_args,
                        raw_arguments=raw_args if isinstance(raw_args, str) else json.dumps(raw_args),
                        extra_content=extra or None,
                    )
                )

            return content, normalized_calls

        # Fallback: Check Gemini native candidates format
        candidates = response_data.get("candidates", [])
        if candidates:
            cand = candidates[0]
            parts = cand.get("content", {}).get("parts", [])
            text_parts = []
            normalized_calls = []

            for part in parts:
                if "text" in part:
                    text_parts.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    call_id = f"call_gemini_{uuid.uuid4().hex[:8]}"
                    name = fc.get("name", "")
                    args = fc.get("args", {})
                    if not isinstance(args, dict):
                        args, _ = parse_and_repair_json(args)
                        if args is None:
                            args = {}
                    normalized_calls.append(
                        NormalizedToolCall(
                            id=call_id,
                            name=name,
                            arguments=args,
                            raw_arguments=json.dumps(args),
                            extra_content={"thought_signature": part.get("thought_signature")}
                            if part.get("thought_signature")
                            else None,
                        )
                    )

            return "".join(text_parts), normalized_calls

        return "", []

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

        # Content / text delta
        if delta.get("content"):
            events.append(
                NormalizedStreamEvent(
                    type="text_delta",
                    content=delta["content"],
                )
            )

        # Gemini thinking / reasoning
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
                    # Assign a stable non-empty ID if missing
                    stable_id = call_id if (call_id and call_id.strip()) else f"call_gemini_{uuid.uuid4().hex[:8]}"
                    tool_calls_accumulator[idx] = {
                        "id": stable_id,
                        "name": name or "",
                        "arguments_chunks": [],
                        "extra_content": dtc.get("extra_content"),
                    }
                else:
                    acc = tool_calls_accumulator[idx]
                    if call_id and call_id.strip() and not acc["id"]:
                        acc["id"] = call_id
                    if name:
                        acc["name"] = name
                    if dtc.get("extra_content"):
                        acc["extra_content"] = dtc["extra_content"]

                if args_delta is not None:
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

                call_id = acc["id"] or f"call_gemini_{uuid.uuid4().hex[:8]}"
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
        assistant_tool_calls = []
        for tc in tool_calls:
            call_id = tc.id or f"call_gemini_{uuid.uuid4().hex[:8]}"
            call_dict: dict[str, Any] = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else (tc.raw_arguments or "{}"),
                },
            }
            if tc.extra_content:
                call_dict["extra_content"] = tc.extra_content
            assistant_tool_calls.append(call_dict)

        # Crucial for Gemini: content MUST be None or omitted when tool_calls is present, NOT empty string ""
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
