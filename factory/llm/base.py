"""Provider-neutral LLM interface used by the agent harness.

The harness speaks in these types only; each provider translates to its native tool-calling API.
Assistant turns keep the provider's raw message so the same provider can replay it verbatim
within one conversation (required e.g. for Anthropic thinking blocks).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from factory.report.models import Usage

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


class LLMError(RuntimeError):
    """The provider failed after its own retries (network, auth, quota, bad request)."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: Usage
    model: str
    raw_assistant: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    response: LLMResponse


@dataclass(frozen=True)
class ToolResultsMessage:
    results: tuple[ToolResult, ...]


Message = UserMessage | AssistantMessage | ToolResultsMessage


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def complete(
        self,
        *,
        system: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
        agent: str,
        max_tokens: int,
    ) -> LLMResponse:
        """One model call. ``agent`` tags the caller (test_writer/coder) for logging and replay."""
