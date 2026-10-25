"""One agent turn: call the model, execute its tool calls, repeat until it stops or hits a cap.

This is the hand-written inner loop shared by the test writer and the coder. It never decides
whether the work is good; that is the outer loop's job, using real check output.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from factory.config import FactoryConfig
from factory.harness.tools import TOOLS, ToolExecutor
from factory.llm.base import (
    AssistantMessage,
    LLMProvider,
    Message,
    ToolResult,
    ToolResultsMessage,
    UserMessage,
)
from factory.llm.pricing import estimate_cost
from factory.report.models import Usage, Violation


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Run-wide token/cost budget. `spent` is the run record's usage object, updated in place."""

    def __init__(self, cfg: FactoryConfig, spent: Usage) -> None:
        self.cfg = cfg
        self.spent = spent

    def exceeded(self) -> str | None:
        tokens, cost = self.spent.total_tokens, self.spent.cost_usd
        if tokens >= self.cfg.max_tokens_per_run:
            return f"token budget exhausted ({tokens} >= {self.cfg.max_tokens_per_run})"
        if cost is not None and cost >= self.cfg.max_cost_usd_per_run:
            return f"cost budget exhausted (${cost:.4f} >= ${self.cfg.max_cost_usd_per_run})"
        return None

    def check(self) -> None:
        reason = self.exceeded()
        if reason:
            raise BudgetExceeded(reason)


@dataclass
class TurnResult:
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0
    final_text: str = ""
    stop: str = "end_turn"  # end_turn | tool_cap | max_tokens | refusal | budget


def run_turn(
    *,
    provider: LLMProvider,
    system: str,
    user: str,
    executor: ToolExecutor,
    cfg: FactoryConfig,
    budget: Budget,
    agent: str,
    record_violation: Callable[[Violation], None],
) -> TurnResult:
    result = TurnResult()
    messages: list[Message] = [UserMessage(user)]
    max_llm_calls = cfg.max_tool_calls_per_iter + 5
    for _ in range(max_llm_calls):
        budget.check()
        response = provider.complete(system=system, messages=messages, tools=TOOLS, agent=agent,
                                     max_tokens=cfg.llm_max_output_tokens)
        response.usage.cost_usd = estimate_cost(provider.model, response.usage)
        result.usage.add(response.usage)
        budget.spent.add(response.usage)
        if response.text:
            result.final_text = response.text
        if response.stop_reason in ("max_tokens", "refusal"):
            result.stop = response.stop_reason
            break
        if not response.tool_calls:
            result.stop = "end_turn"
            break
        results: list[ToolResult] = []
        for call in response.tool_calls:
            if result.tool_calls >= cfg.max_tool_calls_per_iter:
                results.append(ToolResult(call.id, "Tool-call cap reached for this iteration; "
                                                   "end your turn.", is_error=True))
                continue
            result.tool_calls += 1
            results.append(executor.execute(call))
        messages.append(AssistantMessage(response))
        messages.append(ToolResultsMessage(tuple(results)))
        if result.tool_calls >= cfg.max_tool_calls_per_iter:
            record_violation(Violation(
                rule="tool_call_cap", agent=agent, iteration=executor.iteration,
                detail=f"reached {cfg.max_tool_calls_per_iter} tool calls; turn ended by harness",
            ))
            result.stop = "tool_cap"
            break
    return result
