"""State shared by the stations of one run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc
from factory.harness.check_env import CheckEnv
from factory.harness.guardrails import Guardrails
from factory.harness.loop import Budget
from factory.llm.base import LLMProvider
from factory.report.models import Iteration, RunRecord, Violation
from factory.report.writer import write_report
from factory.spec import ApiSpec


def _noop(_: str) -> None:
    return None


@dataclass
class RunContext:
    cfg: FactoryConfig
    spec: ApiSpec
    project: Path
    run_dir: Path
    record: RunRecord
    provider: LLMProvider | None
    guard: Guardrails
    budget: Budget
    spec_changes: list[str] = field(default_factory=list)
    log: Callable[[str], None] = _noop
    # Entities profile only: the approved document, and the database/secret checks run with.
    entities: EntitiesDoc | None = None
    check_env: CheckEnv | None = None

    def save(self) -> None:
        write_report(self.record, self.run_dir)

    def violation_recorder(self, it: Iteration) -> Callable[[Violation], None]:
        def record(v: Violation) -> None:
            it.violations.append(v)
            self.record.violations.append(v)
            self.log(f"    guardrail: {v.rule} {v.path or ''} — {v.detail}")

        return record

    def write_diff(self, name: str, diff: str) -> str:
        path = self.run_dir / "iterations" / f"{name}.diff"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(diff)
        return str(path.relative_to(self.run_dir))
