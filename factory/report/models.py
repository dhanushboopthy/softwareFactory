"""Typed run record. Serialised to runs/<run_id>/run.json after every station and iteration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

RunStatus = Literal["running", "passed", "failed", "budget_exhausted", "error", "cancelled"]
StationStatus = Literal["passed", "failed", "skipped", "error"]


def now() -> datetime:
    return datetime.now(UTC)


class CheckResult(BaseModel):
    name: str
    passed: bool
    summary: str
    failures: list[str] = Field(default_factory=list)
    duration_s: float = 0.0
    returncode: int | None = None
    timed_out: bool = False
    skipped: bool = False
    log_path: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Violation(BaseModel):
    rule: str
    agent: str
    path: str | None = None
    detail: str
    iteration: int | None = None
    at: datetime = Field(default_factory=now)


class Usage(BaseModel):
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = 0.0

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    def add(self, other: Usage) -> None:
        self.llm_calls += other.llm_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        if self.cost_usd is None or other.cost_usd is None:
            self.cost_usd = None
        else:
            self.cost_usd = round(self.cost_usd + other.cost_usd, 6)


class Iteration(BaseModel):
    agent: Literal["test_writer", "coder"]
    index: int
    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
    tool_calls: int = 0
    files_changed: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    diff_path: str | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    all_passed: bool = False
    failure_summary: str = ""
    usage: Usage = Field(default_factory=Usage)
    violations: list[Violation] = Field(default_factory=list)
    rejected: bool = False
    reject_reason: str | None = None
    agent_message: str = ""

    @property
    def lines_changed(self) -> int:
        return self.lines_added + self.lines_removed


class StationResult(BaseModel):
    name: str
    status: StationStatus
    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
    detail: str = ""
    checks: list[CheckResult] = Field(default_factory=list)


class FileChange(BaseModel):
    path: str
    change: Literal["added", "removed", "modified"]
    lines_added: int = 0
    lines_removed: int = 0


class IncrementalInfo(BaseModel):
    previous_run: str
    spec_changes: list[str] = Field(default_factory=list)
    regenerated: list[str] = Field(default_factory=list)
    preserved: list[str] = Field(default_factory=list)
    project_diff: list[FileChange] = Field(default_factory=list)


class RunRecord(BaseModel):
    run_id: str
    spec_name: str
    spec_path: str
    spec_sha256: str
    provider: str
    model: str
    started_at: datetime = Field(default_factory=now)
    ended_at: datetime | None = None
    status: RunStatus = "running"
    status_reason: str = ""
    profile: str = "openapi"
    # Entities profile: whether the input file carried `approved: true`, and the limits that were
    # computed from the number of rules and actions (R1, R18).
    approved: bool | None = None
    budgets: dict[str, float] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    stations: list[StationResult] = Field(default_factory=list)
    iterations: list[Iteration] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    contract: CheckResult | None = None
    # Entities profile: the two fake-rules runs of the accepted (or last) test-writer attempt.
    cheat_probes: list[CheckResult] = Field(default_factory=list)
    # Entities profile: `docker compose up` of the generated project plus a round trip (None when
    # the check did not run).
    deploy: CheckResult | None = None
    incremental: IncrementalInfo | None = None
    error: str | None = None

    def station(self, name: str) -> StationResult | None:
        return next((s for s in self.stations if s.name == name), None)
