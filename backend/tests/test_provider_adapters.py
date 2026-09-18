import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from open_webui.providers.base import BaseProviderAdapter
from open_webui.providers.openai import OpenAIAdapter
from open_webui.providers.gemini import GeminiAdapter
from open_webui.providers.groq import GroqAdapter
from open_webui.providers.models import (
    NormalizedToolSchema,
    NormalizedToolCall,
    NormalizedToolResult,
    NormalizedStreamEvent,
)
from open_webui.providers.json_repair import parse_and_repair_json
from open_webui.providers.mcp_executor import MCPExecutor, resolve_mcp_tool_name
from open_webui.providers.registry import ModelCapabilityRegistry, registry
from open_webui.utils.misc import convert_output_to_messages


# ---------------------------------------------------------------------------
# 1. OpenAI normal response
# ---------------------------------------------------------------------------
def test_openai_normal_response():
    adapter = OpenAIAdapter()
    response_data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Hello world!"},
                "finish_reason": "stop",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert content == "Hello world!"
    assert len(tool_calls) == 0


# ---------------------------------------------------------------------------
# 2. OpenAI MCP tool call
# ---------------------------------------------------------------------------
def test_openai_mcp_tool_call():
    adapter = OpenAIAdapter()
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "diffy__list_skills",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "call_123"
    assert tool_calls[0].name == "diffy__list_skills"
    assert tool_calls[0].arguments == {}


# ---------------------------------------------------------------------------
# 3. OpenAI multiple tool calls
# ---------------------------------------------------------------------------
def test_openai_multiple_tool_calls():
    adapter = OpenAIAdapter()
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "diffy__list_skills",
                                "arguments": "{}",
                            },
                        },
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {
                                "name": "diffy__get_skill",
                                "arguments": '{"name": "github-knowledge"}',
                            },
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert len(tool_calls) == 2
    assert tool_calls[0].name == "diffy__list_skills"
    assert tool_calls[1].name == "diffy__get_skill"
    assert tool_calls[1].arguments == {"name": "github-knowledge"}


# ---------------------------------------------------------------------------
# 4. Gemini normal response
# ---------------------------------------------------------------------------
def test_gemini_normal_response():
    adapter = GeminiAdapter()
    # Test OpenAI-compatible format
    response_data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Gemini answer."},
                "finish_reason": "stop",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert content == "Gemini answer."
    assert len(tool_calls) == 0

    # Test native candidates format
    native_data = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Gemini native answer."}],
                    "role": "model",
                }
            }
        ]
    }
    content_native, tool_calls_native = adapter.parse_response(native_data)
    assert content_native == "Gemini native answer."
    assert len(tool_calls_native) == 0


# ---------------------------------------------------------------------------
# 5. Gemini MCP tool call
# ---------------------------------------------------------------------------
def test_gemini_mcp_tool_call():
    adapter = GeminiAdapter()
    # Gemini sometimes provides an empty ID or no ID
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "",
                            "type": "function",
                            "function": {
                                "name": "diffy__list_skills",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "diffy__list_skills"
    # Stable non-empty ID must be synthesized
    assert tool_calls[0].id.startswith("call_gemini_")


# ---------------------------------------------------------------------------
# 6. Gemini MCP tool result continuation
# ---------------------------------------------------------------------------
def test_gemini_mcp_tool_result_continuation():
    adapter = GeminiAdapter()
    base_messages = [{"role": "user", "content": "List my skills"}]
    tool_call = NormalizedToolCall(
        id="call_gemini_123",
        name="diffy__list_skills",
        arguments={},
        extra_content={"thought_signature": "sig_abc"},
    )
    tool_result = NormalizedToolResult(
        tool_call_id="call_gemini_123",
        tool_name="diffy__list_skills",
        content='{"skills": ["auth", "database"]}',
    )

    continuation = adapter.format_continuation_messages(
        base_messages=base_messages,
        tool_calls=[tool_call],
        tool_results=[tool_result],
    )

    # Must contain base_message, assistant message, and tool message
    assert len(continuation) == 3
    assistant_msg = continuation[1]
    assert assistant_msg["role"] == "assistant"
    # content MUST be None (null) and not empty string ""
    assert assistant_msg["content"] is None
    assert len(assistant_msg["tool_calls"]) == 1
    assert assistant_msg["tool_calls"][0]["id"] == "call_gemini_123"
    assert assistant_msg["tool_calls"][0]["extra_content"] == {"thought_signature": "sig_abc"}

    tool_msg = continuation[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_gemini_123"
    assert "skills" in tool_msg["content"]


# ---------------------------------------------------------------------------
# 7. Gemini multiple tool calls
# ---------------------------------------------------------------------------
def test_gemini_multiple_tool_calls():
    adapter = GeminiAdapter()
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_g1",
                            "type": "function",
                            "function": {"name": "diffy__list_skills", "arguments": "{}"},
                        },
                        {
                            "id": "call_g2",
                            "type": "function",
                            "function": {"name": "diffy__get_skill", "arguments": '{"name": "auth"}'},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert len(tool_calls) == 2
    assert tool_calls[0].id == "call_g1"
    assert tool_calls[1].id == "call_g2"
    assert tool_calls[1].arguments == {"name": "auth"}


# ---------------------------------------------------------------------------
# 8. Groq normal response
# ---------------------------------------------------------------------------
def test_groq_normal_response():
    adapter = GroqAdapter()
    response_data = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Groq fast reply."},
                "finish_reason": "stop",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert content == "Groq fast reply."
    assert len(tool_calls) == 0


# ---------------------------------------------------------------------------
# 9. Groq MCP tool call
# ---------------------------------------------------------------------------
def test_groq_mcp_tool_call():
    adapter = GroqAdapter()
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_groq_01",
                            "type": "function",
                            "function": {
                                "name": "diffy__list_skills",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "call_groq_01"
    assert tool_calls[0].name == "diffy__list_skills"
    assert tool_calls[0].arguments == {}


# ---------------------------------------------------------------------------
# 10. Groq MCP tool result continuation
# ---------------------------------------------------------------------------
def test_groq_mcp_tool_result_continuation():
    adapter = GroqAdapter()
    base_messages = [{"role": "user", "content": "Fetch skill"}]
    tool_call = NormalizedToolCall(
        id="call_groq_abc",
        name="diffy__get_skill",
        arguments={"name": "python-fastapi"},
    )
    tool_result = NormalizedToolResult(
        tool_call_id="call_groq_abc",
        tool_name="diffy__get_skill",
        content='{"instructions": "FastAPI rules"}',
    )

    continuation = adapter.format_continuation_messages(
        base_messages=base_messages,
        tool_calls=[tool_call],
        tool_results=[tool_result],
    )

    assert len(continuation) == 3
    assistant_msg = continuation[1]
    # In Groq, content MUST be None so that JSON serialization produces `null`,
    # NOT empty string `""` which causes HTTP 400 rejection from Groq.
    assert assistant_msg["content"] is None
    assert assistant_msg["tool_calls"][0]["id"] == "call_groq_abc"

    tool_msg = continuation[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_groq_abc"


# ---------------------------------------------------------------------------
# 11. Groq multiple tool calls
# ---------------------------------------------------------------------------
def test_groq_multiple_tool_calls():
    adapter = GroqAdapter()
    response_data = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_gr1",
                            "type": "function",
                            "function": {"name": "diffy__list_skills", "arguments": "{}"},
                        },
                        {
                            "id": "call_gr2",
                            "type": "function",
                            "function": {"name": "diffy__list_skill_files", "arguments": '{"name": "auth"}'},
                        },
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    content, tool_calls = adapter.parse_response(response_data)
    assert len(tool_calls) == 2
    assert tool_calls[0].name == "diffy__list_skills"
    assert tool_calls[1].name == "diffy__list_skill_files"


# ---------------------------------------------------------------------------
# 12. Malformed tool arguments repair
# ---------------------------------------------------------------------------
def test_malformed_tool_arguments_repair():
    # Single quotes
    parsed, err = parse_and_repair_json("{'name': 'auth', 'enabled': True}")
    assert err is None
    assert parsed == {"name": "auth", "enabled": True}

    # Markdown code blocks
    markdown_wrapped = "```json\n{\"name\": \"auth\"}\n```"
    parsed, err = parse_and_repair_json(markdown_wrapped)
    assert err is None
    assert parsed == {"name": "auth"}

    # Unclosed bracket
    unclosed = '{"name": "github-knowledge"'
    parsed, err = parse_and_repair_json(unclosed)
    assert err is None
    assert parsed == {"name": "github-knowledge"}

    # Trailing commas
    trailing = '{"name": "auth", "limit": 10,}'
    parsed, err = parse_and_repair_json(trailing)
    assert err is None
    assert parsed == {"name": "auth", "limit": 10}


# ---------------------------------------------------------------------------
# 13. Fragmented streaming tool arguments
# ---------------------------------------------------------------------------
def test_fragmented_streaming_tool_arguments():
    adapter = GroqAdapter()
    accumulator = {}

    # Chunk 1: Tool call intro
    chunk1 = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_chunk_1",
                            "function": {"name": "diffy__get_skill", "arguments": ""},
                        }
                    ]
                }
            }
        ]
    }
    events1, completed1 = adapter.parse_stream_chunk(chunk1, accumulator)
    assert len(completed1) == 0

    # Chunk 2: Fragmented argument start
    chunk2 = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"arguments": '{"na'},
                        }
                    ]
                }
            }
        ]
    }
    events2, completed2 = adapter.parse_stream_chunk(chunk2, accumulator)
    assert len(completed2) == 0

    # Chunk 3: Fragmented argument end
    chunk3 = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"arguments": 'me": "auth"}'},
                        }
                    ]
                }
            }
        ]
    }
    events3, completed3 = adapter.parse_stream_chunk(chunk3, accumulator)
    assert len(completed3) == 0

    # Chunk 4: Finish reason
    chunk4 = {
        "choices": [
            {
                "delta": {},
                "finish_reason": "tool_calls",
            }
        ]
    }
    events4, completed4 = adapter.parse_stream_chunk(chunk4, accumulator)
    assert len(completed4) == 1
    assert completed4[0].name == "diffy__get_skill"
    assert completed4[0].arguments == {"name": "auth"}
    assert completed4[0].id == "call_chunk_1"


from functools import wraps

def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))
    return run


# ---------------------------------------------------------------------------
# 14. Unknown MCP tool
# ---------------------------------------------------------------------------
@async_test
async def test_unknown_mcp_tool():
    executor = MCPExecutor()
    available_tools = {
        "diffy__list_skills": {"callable": AsyncMock(return_value="[]")},
    }
    call = NormalizedToolCall(
        id="call_unknown",
        name="diffy__non_existent_tool",
        arguments={},
    )
    result = await executor.execute(call, available_tools)
    assert result.is_error is True
    assert "not found" in result.content


# ---------------------------------------------------------------------------
# 15. MCP timeout handling
# ---------------------------------------------------------------------------
@async_test
async def test_mcp_timeout_handling():
    executor = MCPExecutor(timeout_seconds=0.05)

    async def slow_tool(**kwargs):
        await asyncio.sleep(0.2)
        return "finished"

    available_tools = {
        "diffy__slow_tool": {"callable": slow_tool},
    }
    call = NormalizedToolCall(
        id="call_slow",
        name="diffy__slow_tool",
        arguments={},
    )
    result = await executor.execute(call, available_tools)
    assert result.is_error is True
    assert "timed out" in result.content.lower()


# ---------------------------------------------------------------------------
# 16. MCP error result handling
# ---------------------------------------------------------------------------
@async_test
async def test_mcp_error_result_handling():
    executor = MCPExecutor()

    async def failing_tool(**kwargs):
        raise RuntimeError("Database connection dropped")

    available_tools = {
        "diffy__failing_tool": {"callable": failing_tool},
    }
    call = NormalizedToolCall(
        id="call_fail",
        name="diffy__failing_tool",
        arguments={},
    )
    result = await executor.execute(call, available_tools)
    assert result.is_error is True
    assert "Database connection dropped" in result.content


# ---------------------------------------------------------------------------
# 17. Maximum tool iterations limit
# ---------------------------------------------------------------------------
def test_maximum_tool_iterations_limit():
    max_iterations = 5
    iteration_count = 0
    simulated_tool_calls = [["call1"], ["call2"], ["call3"], ["call4"], ["call5"], ["call6"]]

    while simulated_tool_calls and iteration_count < max_iterations:
        iteration_count += 1
        simulated_tool_calls.pop(0)

    assert iteration_count == 5
    # Loop safely terminates at max_iterations
    assert len(simulated_tool_calls) == 1


# ---------------------------------------------------------------------------
# 18. Voice response does not speak tool metadata
# ---------------------------------------------------------------------------
def test_voice_response_does_not_speak_tool_metadata():
    # In Open WebUI output format, tool calls and tool results are structured as
    # distinct non-message output items ('function_call' and 'function_call_output').
    # Only items of type 'message' contain human-readable text meant for TTS.
    output = [
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "diffy__list_skills",
            "arguments": "{}",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco_1",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": '{"skills": ["auth"]}'}],
            "status": "completed",
        },
        {
            "type": "message",
            "id": "msg_1",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Here are your available skills: auth."}],
        },
    ]

    # Extract text that would be passed to speech / TTS
    spoken_text = "".join(
        part.get("text", "")
        for item in output
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )

    # Verify tool metadata is completely excluded from spoken text
    assert "diffy__list_skills" not in spoken_text
    assert "fc_1" not in spoken_text
    assert "call_1" not in spoken_text
    assert spoken_text == "Here are your available skills: auth."


# ---------------------------------------------------------------------------
# Extra: Name resolution compatibility (diffy__list_skills, diffy_list_skills, list_skills)
# ---------------------------------------------------------------------------
def test_mcp_tool_name_resolution_compatibility():
    available_tools = {
        "diffy__list_skills": {"spec": {"name": "diffy__list_skills"}},
        "diffy__get_skill": {"spec": {"name": "diffy__get_skill"}},
    }

    # 1. Exact match
    res1, _ = resolve_mcp_tool_name("diffy__list_skills", available_tools)
    assert res1 == "diffy__list_skills"

    # 2. Legacy single underscore
    res2, _ = resolve_mcp_tool_name("diffy_list_skills", available_tools)
    assert res2 == "diffy__list_skills"

    # 3. Unprefixed bare name
    res3, _ = resolve_mcp_tool_name("list_skills", available_tools)
    assert res3 == "diffy__list_skills"


# ---------------------------------------------------------------------------
# Extra: convert_output_to_messages null content on tool calls
# ---------------------------------------------------------------------------
def test_convert_output_to_messages_null_content_on_tool_calls():
    output = [
        {
            "type": "function_call",
            "id": "fc_test",
            "call_id": "call_test_123",
            "name": "diffy__list_skills",
            "arguments": "{}",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "id": "fco_test",
            "call_id": "call_test_123",
            "output": [{"type": "input_text", "text": '["skill1"]'}],
            "status": "completed",
        },
    ]

    messages = convert_output_to_messages(output)
    assert len(messages) == 2
    assistant_msg = messages[0]
    assert assistant_msg["role"] == "assistant"
    # Must be None, NOT "" (vital for Groq & Gemini)
    assert assistant_msg["content"] is None
    assert len(assistant_msg["tool_calls"]) == 1
    assert assistant_msg["tool_calls"][0]["id"] == "call_test_123"

    tool_msg = messages[1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_test_123"
