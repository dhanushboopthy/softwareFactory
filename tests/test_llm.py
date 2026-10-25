"""Provider tests run the real SDK request/response code against a mock HTTP transport.

This is the keyless "dry run": it proves the factory builds valid native tool-calling requests,
replays assistant turns correctly, and parses tool calls and usage from real SDK objects.
"""

import json
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from factory.harness.tools import TOOLS
from factory.llm import ProviderConfigError, get_provider
from factory.llm.anthropic import FALLBACK_BETA, AnthropicProvider
from factory.llm.base import AssistantMessage, ToolResult, ToolResultsMessage, UserMessage
from factory.llm.openai_compat import OpenAICompatProvider
from factory.llm.pricing import estimate_cost
from factory.llm.scripted import ScriptedProvider, ScriptMismatch
from factory.report.models import Usage


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


ANTHROPIC_STREAM = _sse([
    {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 1, "cache_read_input_tokens": 30,
                  "cache_creation_input_tokens": 10}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "Listing files."}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {
        "type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {}}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": "{\"directory\": \"app\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
])


def test_anthropic_provider_round_trip() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=ANTHROPIC_STREAM,
                               headers={"content-type": "text/event-stream"})

    client = anthropic.Anthropic(
        api_key="test-key", max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    provider = AnthropicProvider(model="claude-opus-5-5", effort="high", client=client)
    first = provider.complete(system="sys", messages=[UserMessage("go")], tools=TOOLS,
                              agent="coder", max_tokens=1000)

    assert first.text == "Listing files."
    assert first.stop_reason == "tool_use"
    assert [(c.id, c.name, c.arguments) for c in first.tool_calls] == [
        ("toolu_1", "list_files", {"directory": "app"})]
    assert first.usage == Usage(llm_calls=1, input_tokens=120, output_tokens=42,
                                cache_read_tokens=30, cache_write_tokens=10)

    body = json.loads(requests[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["stream"] is True
    assert body["system"] == "sys"
    assert [t["name"] for t in body["tools"]] == ["read_file", "list_files", "write_file"]
    assert body["output_config"] == {"effort": "high"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in requests[0].headers["anthropic-beta"]

    # Second turn: the assistant turn is replayed verbatim and tool results are one user message.
    provider.complete(
        system="sys", tools=TOOLS, agent="coder", max_tokens=1000,
        messages=[UserMessage("go"), AssistantMessage(first),
                  ToolResultsMessage((ToolResult("toolu_1", "app/main.py"),))],
    )
    msgs = json.loads(requests[1].content)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][1]["type"] == "tool_use"
    assert msgs[2]["content"] == [{"type": "tool_result", "tool_use_id": "toolu_1",
                                   "content": "app/main.py", "is_error": False}]


def test_anthropic_fallbacks_only_for_supported_models() -> None:
    from factory.llm.anthropic import build_request

    req = build_request(model="claude-haiku-4-5", system="s", messages=[], tools=[],
                        max_tokens=10, effort=None, fallbacks=True)
    assert "fallbacks" not in req and "betas" not in req and "output_config" not in req


OPENAI_RESPONSE = {
    "id": "c1", "object": "chat.completion", "created": 0, "model": "deepseek-chat",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "write_file", "arguments": "{\"path\": \"app/db.py\", \"content\": \"x\"}"}},
            {"id": "call_2", "type": "function", "function": {
                "name": "read_file", "arguments": "{not json"}}]}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
              "prompt_tokens_details": {"cached_tokens": 40}},
}


def test_openai_compatible_provider_round_trip() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=OPENAI_RESPONSE)

    provider = OpenAICompatProvider(name="deepseek", model="deepseek-chat", api_key="k",
                                    base_url="https://api.deepseek.com")
    provider.client = openai.OpenAI(
        api_key="k", base_url="https://api.deepseek.com", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    first = provider.complete(system="sys", messages=[UserMessage("go")], tools=TOOLS,
                              agent="coder", max_tokens=32_000)
    assert first.stop_reason == "tool_use"
    assert first.tool_calls[0].arguments == {"path": "app/db.py", "content": "x"}
    assert first.tool_calls[1].arguments == {"__invalid__": "{not json"}
    assert (first.usage.input_tokens, first.usage.cache_read_tokens,
            first.usage.output_tokens) == (60, 40, 20)
    body = json.loads(requests[0].content)
    assert body["max_tokens"] == 8192  # DeepSeek output cap
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["tools"][0]["function"]["name"] == "read_file"

    provider.complete(
        system="sys", tools=TOOLS, agent="coder", max_tokens=1000,
        messages=[UserMessage("go"), AssistantMessage(first), ToolResultsMessage((
            ToolResult("call_1", "Wrote app/db.py"),
            ToolResult("call_2", "bad args", is_error=True)))],
    )
    msgs = json.loads(requests[1].content)["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "tool"]
    assert msgs[4] == {"role": "tool", "tool_call_id": "call_2", "content": "ERROR: bad args"}


def test_scripted_provider_checks_expectations(tmp_path: Path) -> None:
    (tmp_path / "f.py").write_text("x = 1\n")
    script = tmp_path / "s.yaml"
    script.write_text(
        "agents:\n"
        "  coder:\n"
        "    - tool_calls: [{name: write_file, args: {path: a.py, content_file: f.py}}]\n"
        "    - expect: ['test_total']\n"
        "      text: done\n"
    )
    provider = ScriptedProvider(script)
    first = provider.complete(system="s", messages=[UserMessage("go")], tools=TOOLS,
                              agent="coder", max_tokens=10)
    assert first.tool_calls[0].arguments == {"path": "a.py", "content": "x = 1\n"}
    with pytest.raises(ScriptMismatch):
        provider.complete(system="s", messages=[UserMessage("no failures here")], tools=TOOLS,
                          agent="coder", max_tokens=10)
    exhausted = provider.complete(system="s", messages=[UserMessage("x")], tools=TOOLS,
                                  agent="coder", max_tokens=10)
    assert exhausted.tool_calls == [] and "exhausted" in exhausted.text


def test_get_provider_validation(tmp_path: Path) -> None:
    with pytest.raises(ProviderConfigError, match="LLM_PROVIDER must be one of"):
        get_provider({})
    with pytest.raises(ProviderConfigError, match="DEEPSEEK_API_KEY"):
        get_provider({"LLM_PROVIDER": "deepseek"})
    with pytest.raises(ProviderConfigError, match="LLM_MODEL is required"):
        get_provider({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"})
    anth = get_provider({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"})
    assert (anth.name, anth.model) == ("anthropic", "claude-opus-5-5")
    script = tmp_path / "s.yaml"
    script.write_text("agents: {}\n")
    assert get_provider({"LLM_PROVIDER": "scripted", "LLM_SCRIPT": str(script)}).name == "scripted"


def test_estimate_cost() -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)
    # 1M in @ $4 + 0.1M out @ $20 + 1M cache-read @ $0.40
    assert estimate_cost("claude-opus-5-5", usage, env={}) == pytest.approx(6.4)
    assert estimate_cost("unknown-model", usage, env={}) is None
    assert estimate_cost("unknown-model", usage,
                         env={"LLM_PRICE_IN": "1", "LLM_PRICE_OUT": "2"}) == pytest.approx(1.3)
