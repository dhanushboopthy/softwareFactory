from collections.abc import Sequence
from pathlib import Path

import pytest

from factory.config import FactoryConfig
from factory.harness.guardrails import Guardrails, Role
from factory.harness.loop import Budget, BudgetExceeded, run_turn
from factory.harness.tools import ToolExecutor
from factory.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    StopReason,
    ToolCall,
    ToolResultsMessage,
    ToolSpec,
)
from factory.report.models import Usage, Violation


class FakeProvider(LLMProvider):
    """Returns queued responses; records what the harness sent."""

    name = "fake"
    model = "claude-opus-5-5"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.seen: list[Sequence[Message]] = []

    def complete(self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec],
                 agent: str, max_tokens: int) -> LLMResponse:
        self.seen.append(list(messages))
        return self.responses.pop(0)


def resp(*calls: ToolCall, text: str = "", stop: StopReason | None = None,
         tokens: int = 100) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls),
                       stop_reason=stop or ("tool_use" if calls else "end_turn"),
                       usage=Usage(llm_calls=1, input_tokens=tokens, output_tokens=tokens),
                       model="m")


def write(i: int) -> ToolCall:
    return ToolCall(f"c{i}", "write_file", {"path": "app/db.py", "content": f"x = {i}\n"})


def setup(project: Path, cfg: FactoryConfig) -> tuple[ToolExecutor, list[Violation], Budget]:
    violations: list[Violation] = []
    ex = ToolExecutor(project, Role.CODER, Guardrails(cfg, project), violations.append, 1, 1000)
    return ex, violations, Budget(cfg, Usage())


def test_turn_runs_tools_until_model_stops(project: Path) -> None:
    cfg = FactoryConfig()
    ex, violations, budget = setup(project, cfg)
    provider = FakeProvider([resp(write(1)), resp(write(2)), resp(text="done")])
    result = run_turn(provider=provider, system="s", user="u", executor=ex, cfg=cfg,
                      budget=budget, agent="coder", record_violation=violations.append)
    assert (result.stop, result.tool_calls, result.final_text) == ("end_turn", 2, "done")
    assert (project / "app/db.py").read_text() == "x = 2\n"
    assert result.usage.llm_calls == 3
    assert budget.spent.total_tokens == 600
    assert budget.spent.cost_usd == pytest.approx((300 * 4 + 300 * 20) / 1e6)
    # The second call saw the first call's tool result.
    last = provider.seen[1][-1]
    assert isinstance(last, ToolResultsMessage) and "Wrote app/db.py" in last.results[0].content


def test_tool_call_cap_ends_turn_and_is_recorded(project: Path) -> None:
    cfg = FactoryConfig(max_tool_calls_per_iter=3)
    ex, violations, budget = setup(project, cfg)
    provider = FakeProvider([resp(write(1), write(2)), resp(write(3), write(4), write(5))])
    result = run_turn(provider=provider, system="s", user="u", executor=ex, cfg=cfg,
                      budget=budget, agent="coder", record_violation=violations.append)
    assert result.stop == "tool_cap"
    assert result.tool_calls == 3
    assert (project / "app/db.py").read_text() == "x = 3\n"  # calls 4 and 5 never executed
    assert [v.rule for v in violations] == ["tool_call_cap"]


def test_budget_stops_before_the_next_call(project: Path) -> None:
    cfg = FactoryConfig(max_tokens_per_run=250)
    ex, violations, budget = setup(project, cfg)
    provider = FakeProvider([resp(write(1), tokens=100), resp(write(2), tokens=100)])
    with pytest.raises(BudgetExceeded, match="token budget"):
        run_turn(provider=provider, system="s", user="u", executor=ex, cfg=cfg, budget=budget,
                 agent="coder", record_violation=violations.append)
    assert len(provider.seen) == 2  # the third call was never made


def test_truncated_response_is_not_executed(project: Path) -> None:
    cfg = FactoryConfig()
    ex, violations, budget = setup(project, cfg)
    original = (project / "app/db.py").read_text()
    provider = FakeProvider([resp(stop="max_tokens")])
    result = run_turn(provider=provider, system="s", user="u", executor=ex, cfg=cfg,
                      budget=budget, agent="coder", record_violation=violations.append)
    assert result.stop == "max_tokens"
    assert (project / "app/db.py").read_text() == original
