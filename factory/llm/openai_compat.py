"""OpenAI Chat Completions provider (function calling); also serves DeepSeek's compatible API."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import openai

from factory.llm.base import (
    AssistantMessage,
    LLMError,
    LLMProvider,
    LLMResponse,
    Message,
    StopReason,
    ToolCall,
    ToolResultsMessage,
    ToolSpec,
    UserMessage,
)
from factory.report.models import Usage

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MAX_OUTPUT = 8192

_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def to_native_messages(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if isinstance(m, UserMessage):
            out.append({"role": "user", "content": m.text})
        elif isinstance(m, AssistantMessage):
            out.append(m.response.raw_assistant)
        elif isinstance(m, ToolResultsMessage):
            out.extend({"role": "tool", "tool_call_id": r.call_id,
                        "content": ("ERROR: " if r.is_error else "") + r.content}
                       for r in m.results)
    return out


def to_native_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [{"type": "function",
             "function": {"name": t.name, "description": t.description,
                          "parameters": t.input_schema}}
            for t in tools]


def parse_completion(completion: Any) -> LLMResponse:
    choice = completion.choices[0]
    msg = choice.message
    calls: list[ToolCall] = []
    raw_calls: list[dict[str, Any]] = []
    for tc in msg.tool_calls or []:
        try:
            args = json.loads(tc.function.arguments or "{}")
            if not isinstance(args, dict):
                args = {"__invalid__": tc.function.arguments}
        except json.JSONDecodeError:
            args = {"__invalid__": tc.function.arguments}
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        raw_calls.append({"id": tc.id, "type": "function",
                          "function": {"name": tc.function.name,
                                       "arguments": tc.function.arguments}})
    stop = _STOP.get(str(choice.finish_reason), "other")
    if stop == "max_tokens":
        calls, raw_calls = [], []
    raw: dict[str, Any] = {"role": "assistant", "content": msg.content}
    if raw_calls:
        raw["tool_calls"] = raw_calls
    u = completion.usage
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
    usage = Usage(
        llm_calls=1,
        input_tokens=(u.prompt_tokens or 0) - cached if u else 0,
        output_tokens=(u.completion_tokens or 0) if u else 0,
        cache_read_tokens=cached,
    )
    return LLMResponse(text=(msg.content or "").strip(), tool_calls=calls, stop_reason=stop,
                       usage=usage, model=str(completion.model), raw_assistant=raw)


class OpenAICompatProvider(LLMProvider):
    def __init__(self, name: str, model: str, api_key: str, base_url: str | None = None) -> None:
        self.name = name
        self.model = model
        self.client = openai.OpenAI(api_key=api_key, base_url=base_url)

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec], agent: str,
        max_tokens: int,
    ) -> LLMResponse:
        req: dict[str, Any] = {
            "model": self.model,
            "messages": to_native_messages(system, messages),
        }
        if tools:  # the API rejects an empty `tools` list
            req["tools"] = to_native_tools(tools)
        if self.name == "deepseek":
            req["max_tokens"] = min(max_tokens, DEEPSEEK_MAX_OUTPUT)
        else:
            req["max_completion_tokens"] = max_tokens
        try:
            completion = self.client.chat.completions.create(**req)
        except openai.APIStatusError as exc:
            raise LLMError(f"{self.name} {exc.status_code}: {exc.message}") from exc
        except openai.APIConnectionError as exc:
            raise LLMError(f"{self.name} connection error: {exc}") from exc
        return parse_completion(completion)
