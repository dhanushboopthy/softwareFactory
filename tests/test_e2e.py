"""End-to-end factory builds with the scripted provider (slow; opt in with FACTORY_E2E=1).

These run the real pipeline: codegen, uv-managed project env, pytest, mypy, ruff, bandit and
Schemathesis against a live server. Only the model is replaced by a script.
"""

import os
from pathlib import Path

import pytest

from factory.config import FactoryConfig
from factory.llm.scripted import ScriptedProvider
from factory.pipeline import BuildOptions, build

from .conftest import FIXTURES, SPECS

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("FACTORY_E2E") != "1", reason="set FACTORY_E2E=1"),
]
SCRIPTS = FIXTURES / "scripted"


def test_full_build_then_incremental_build(tmp_path: Path) -> None:
    cfg = FactoryConfig(runs_dir=tmp_path)
    v1 = build(BuildOptions(SPECS / "orders.yaml", run_id="v1"), cfg,
               ScriptedProvider(SCRIPTS / "orders_v1" / "script.yaml"))
    assert v1.status == "passed", v1.status_reason
    assert v1.contract is not None and v1.contract.passed
    assert {v.rule for v in v1.violations} == {"read_denied", "read_only"}
    tw = [it for it in v1.iterations if it.agent == "test_writer"]
    assert [it.all_passed for it in tw] == [False, True]  # weak suite rejected, then accepted

    v2 = build(BuildOptions(SPECS / "orders_v2.yaml", previous_run="v1", run_id="v2"), cfg,
               ScriptedProvider(SCRIPTS / "orders_v2" / "script.yaml"))
    assert v2.status == "passed", v2.status_reason
    assert v2.incremental is not None
    changed = {c.path for c in v2.incremental.project_diff}
    assert "app/services/orders.py" in changed
    assert not any(p.startswith((".hypothesis", ".schemathesis", ".ruff_cache")) for p in changed)


def test_hardcoded_implementation_fails_the_build(tmp_path: Path) -> None:
    cfg = FactoryConfig(runs_dir=tmp_path, max_iters=2)
    run = build(BuildOptions(SPECS / "orders.yaml", run_id="cheat"), cfg,
                ScriptedProvider(SCRIPTS / "orders_cheat" / "script.yaml"))
    assert run.status == "failed"
    coder_iters = [it for it in run.iterations if it.agent == "coder"]
    pytest_results = [c for c in coder_iters[-1].checks if c.name == "pytest"]
    assert pytest_results and not pytest_results[0].passed


def test_no_llm_build_reports_contract_failures(tmp_path: Path) -> None:
    run = build(BuildOptions(SPECS / "orders.yaml", use_llm=False, run_id="det"),
                FactoryConfig(runs_dir=tmp_path), None)
    assert run.status == "failed"
    assert run.contract is not None and not run.contract.passed
    by_type = run.contract.details["failures_by_type"]
    assert isinstance(by_type, dict) and "Server error" in by_type
    assert (tmp_path / "det" / "project" / "client" / "__init__.py").exists()
