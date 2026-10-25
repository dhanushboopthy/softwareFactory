"""Scripted provider: replays a YAML script of tool calls, per agent, through the real harness.

Used to verify the factory end-to-end without an API key, and to build regression scenarios
(e.g. a coder that cheats, a test writer that writes weak tests). Each step may declare
``expect``: substrings that must appear in the newest message the harness sent. That proves the
harness actually fed check failures back, rather than the script just happening to line up.

Script format::

    agents:
      coder:
        - expect: ["test_total"]          # optional
          text: "Fixing the total."       # optional
          tool_calls:
            - name: write_file
              args: {path: app/services/orders.py, content_file: files/orders_v1.py}
        - text: "Done."
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from factory.llm.base import (
    AssistantMessage,
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    ToolResultsMessage,
    ToolSpec,
    UserMessage,
)
from factory.report.models import Usage


class ScriptMismatch(AssertionError):
    """The harness did not send what the script expected at this step."""


def _latest_text(messages: Sequence[Message]) -> str:
    for m in reversed(messages):
        if isinstance(m, UserMessage):
            return m.text
        if isinstance(m, ToolResultsMessage):
            return "\n".join(r.content for r in m.results)
    return ""


def _all_text(system: str, messages: Sequence[Message]) -> int:
    n = len(system)
    for m in messages:
        if isinstance(m, UserMessage):
            n += len(m.text)
        elif isinstance(m, ToolResultsMessage):
            n += sum(len(r.content) for r in m.results)
        elif isinstance(m, AssistantMessage):
            n += len(json.dumps(m.response.raw_assistant, default=str))
    return n


class ScriptedProvider(LLMProvider):
    name = "scripted"

    def __init__(self, script_path: Path) -> None:
        self.model = "scripted"
        self.script_path = script_path
        data = yaml.safe_load(script_path.read_text()) or {}
        self.steps: dict[str, list[dict[str, Any]]] = {
            agent: list(steps or []) for agent, steps in (data.get("agents") or {}).items()
        }
        self.cursor: dict[str, int] = {}
        self.call_seq = 0

    def _resolve_args(self, args: dict[str, Any]) -> dict[str, Any]:
        args = dict(args)
        if "content_file" in args:
            args["content"] = (self.script_path.parent / args.pop("content_file")).read_text()
        return args

    def complete(
        self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec], agent: str,
        max_tokens: int,
    ) -> LLMResponse:
        steps = self.steps.get(agent, [])
        idx = self.cursor.get(agent, 0)
        self.cursor[agent] = idx + 1
        if idx >= len(steps):
            step: dict[str, Any] = {"text": "(script exhausted; ending turn)"}
        else:
            step = steps[idx]
        latest = _latest_text(messages)
        for needle in step.get("expect", []):
            if needle not in latest:
                raise ScriptMismatch(
                    f"{agent} step {idx}: expected {needle!r} in the harness message; got:\n"
                    f"{latest[:1500]}"
                )
        calls = []
        for call in step.get("tool_calls", []):
            self.call_seq += 1
            calls.append(ToolCall(id=f"call_{self.call_seq}", name=call["name"],
                                  arguments=self._resolve_args(call.get("args", {}))))
        text = str(step.get("text", ""))
        out_chars = len(text) + sum(len(json.dumps(c.arguments)) for c in calls)
        usage = Usage(llm_calls=1, input_tokens=_all_text(system, messages) // 4,
                      output_tokens=out_chars // 4)
        raw = {"text": text, "tool_calls": [c.__dict__ for c in calls]}
        return LLMResponse(text=text, tool_calls=calls,
                           stop_reason="tool_use" if calls else "end_turn",
                           usage=usage, model=self.model, raw_assistant=raw)
