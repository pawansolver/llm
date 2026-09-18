from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


def _strip_markdown_fences(text: str) -> str:
    """Strip markdown code fence blocks if present."""
    text = text.strip()
    if text.startswith("```"):
        # Strip opening ```...
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _balance_json_brackets(text: str) -> str:
    """Attempt to balance unclosed quotes, braces, and brackets in truncated JSON."""
    in_string = False
    escape = False
    stack = []

    for char in text:
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if not in_string:
            if char in "{[":
                stack.append(char)
            elif char == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
            elif char == "]":
                if stack and stack[-1] == "[":
                    stack.pop()

    # If string was left open, close it
    if in_string:
        text += '"'

    # Close remaining open brackets in reverse order
    while stack:
        opener = stack.pop()
        if opener == "{":
            text += "}"
        elif opener == "[":
            text += "]"

    return text


def _clean_trailing_commas(text: str) -> str:
    """Remove trailing commas before closing braces/brackets."""
    return re.sub(r",\s*([\]}])", r"\1", text)


def parse_and_repair_json(raw: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Robust JSON parsing with multi-stage recovery for tool arguments.

    Returns:
        (parsed_dict, None) if successful.
        (None, error_description) if parsing and all repair strategies failed.
    """
    if raw is None:
        return {}, None

    if isinstance(raw, dict):
        return raw, None

    if not isinstance(raw, str):
        try:
            return dict(raw), None
        except Exception:
            return None, f"Expected string or dict, got {type(raw).__name__}"

    text = raw.strip()
    if not text:
        return {}, None

    # Fast path: standard json.loads
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val, None
        elif isinstance(val, list):
            # Sometimes a tool call returns a list of arguments or parameters
            return {"items": val}, None
        elif isinstance(val, (str, int, float, bool)):
            return {"value": val}, None
    except Exception:
        pass

    # Stage 1: Strip markdown code fences
    cleaned = _strip_markdown_fences(text)
    if cleaned != text:
        try:
            val = json.loads(cleaned)
            if isinstance(val, dict):
                return val, None
        except Exception:
            pass

    # Stage 2: Python literal evaluation (handles single quotes: {'name': 'foo'}, True, False, None)
    try:
        val = ast.literal_eval(cleaned)
        if isinstance(val, dict):
            return val, None
    except Exception:
        pass

    # Stage 3: Remove trailing commas and balance brackets
    repaired = _clean_trailing_commas(cleaned)
    repaired = _balance_json_brackets(repaired)
    try:
        val = json.loads(repaired)
        if isinstance(val, dict):
            return val, None
    except Exception:
        pass

    # Stage 4: Replace single quotes with double quotes (outside existing strings)
    try:
        # Simple heuristic for JSON with single quotes
        single_quote_replaced = re.sub(r"(?<!\\)'", '"', cleaned)
        single_quote_replaced = _clean_trailing_commas(single_quote_replaced)
        single_quote_replaced = _balance_json_brackets(single_quote_replaced)
        val = json.loads(single_quote_replaced)
        if isinstance(val, dict):
            return val, None
    except Exception:
        pass

    log.warning("Failed to parse tool call arguments after all repair attempts: %s", raw[:300])
    return None, f"Malformed JSON arguments could not be parsed: {raw[:200]}"
