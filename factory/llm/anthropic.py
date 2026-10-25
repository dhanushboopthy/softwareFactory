"""Anthropic Messages API provider (native tool use)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anthropic

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

DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def to_native_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, UserMessage):
            out.append({"role": "user", "content": m.text})
        elif isinstance(m, AssistantMessage):
            # Replay the provider's own content blocks unchanged (thinking blocks included).
            out.append({"role": "assistant", "content": m.response.raw_assistant})
        elif isinstance(m, ToolResultsMessage):
            out.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content,
                     "is_error": r.is_error}
                    for r in m.results
                ],
            })
    return out


def to_native_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools]


def build_request(
    *, model: str, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec],
    max_tokens: int, effort: str | None, fallbacks: bool,
) -> dict[str, Any]:
    req: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": to_native_messages(messages),
        "tools": to_native_tools(tools),
        # Auto-caching: the growing conversation prefix is reused across tool-call turns.
        "cache_control": {"type": "ephemeral"},
    }
    if effort:
        req["output_config"] = {"effort": effort}
    if fallbacks and model in FALLBACK_MODELS:
        req["fallbacks"] = "default"
        req["betas"] = [FALLBACK_BETA]
    return req


def parse_message(msg: Any) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in msg.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            args = block.input if isinstance(block.input, dict) else {"__invalid__": block.input}
            calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
    stop: StopReason = _STOP.get(str(msg.stop_reason), "other")
    if stop == "refusal":
        category = getattr(getattr(msg, "stop_details", None), "category", None)
        texts.append(f"[refusal: {category or 'unspecified'}]")
    if stop == "max_tokens":
        calls = []  # tool inputs may be truncated; never execute them
    u = msg.usage
    usage = Usage(
        llm_calls=1,
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
    )
    return LLMResponse(text="\n".join(texts).strip(), tool_calls=calls, stop_reason=stop,
                       usage=usage, model=str(msg.model), raw_assistant=msg.content)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self, model: str = DEFAULT_MODEL, effort: str | None = "high", fallbacks: bool = True,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self.client = client or anthropic.Anthropic()

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec], agent: str,
        max_tokens: int,
    ) -> LLMResponse:
        req = build_request(model=self.model, system=system, messages=messages, tools=tools,
                            max_tokens=max_tokens, effort=self.effort, fallbacks=self.fallbacks)
        try:
            # Streaming avoids HTTP timeouts on long tool inputs (whole files).
            with self.client.beta.messages.stream(**req) as stream:
                msg = stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise LLMError(f"anthropic {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"anthropic connection error: {exc}") from exc
        return parse_message(msg)
