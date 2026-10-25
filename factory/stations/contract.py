"""Station 5: final contract test. Schemathesis against the live server, on a fresh database."""

from __future__ import annotations

from pathlib import Path

from factory.config import FactoryConfig
from factory.harness import checks
from factory.report.models import CheckResult


def run_contract(project: Path, cfg: FactoryConfig, run_dir: Path) -> CheckResult:
    return checks.run_schemathesis(project, cfg, run_dir / "contract", name="contract")
