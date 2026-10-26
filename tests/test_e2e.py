"""End-to-end factory builds with the scripted provider (slow; opt in with FACTORY_E2E=1).

These run the real pipeline: codegen, uv-managed project env, pytest, mypy, ruff, bandit and
Schemathesis against a live server. Only the model is replaced by a script.
"""

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from factory.config import FactoryConfig
from factory.llm.scripted import ScriptedProvider
from factory.pipeline import BuildOptions, build

from .conftest import FIXTURES, ROOT, SPECS

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


TASKS = ROOT / "examples" / "tasks" / "entities.yaml"


def entities_cfg(tmp_path: Path, **overrides: Any) -> FactoryConfig:
    """Config for an entities build; FACTORY_POSTGRES_BACKEND=local runs it without Docker."""
    backend = os.environ.get("FACTORY_POSTGRES_BACKEND", "docker")
    # The deploy check needs Docker, which the "local" backend exists to avoid.
    settings: dict[str, Any] = {"postgres_backend": backend, "deploy_check": backend == "docker"}
    return replace(FactoryConfig(runs_dir=tmp_path), **{**settings, **overrides})


def test_tasks_build_from_entities_passes(tmp_path: Path) -> None:
    run = build(BuildOptions(TASKS, run_id="tasks"), entities_cfg(tmp_path),
                ScriptedProvider(SCRIPTS / "tasks_v1" / "script.yaml"))
    assert run.status == "passed", (run.status_reason, [
        (s.name, s.status, s.detail[:300]) for s in run.stations if s.status != "passed"])
    assert run.profile == "entities" and run.approved is True
    assert run.spec_name == "tasks" and run.run_id == "tasks"
    assert run.budgets["llm_items"] == 3.0 and run.budgets["max_iters"] == 10.0
    assert [s.name for s in run.stations] == [
        "lint", "scaffold", "setup", "postgres", "test_writer", "baseline_checks", "coder",
        "contract", "sdk", "deploy_check"]
    assert run.contract is not None and run.contract.passed
    if os.environ.get("FACTORY_POSTGRES_BACKEND", "docker") == "docker":  # needs a Docker daemon
        assert run.deploy is not None and run.deploy.passed, run.deploy
    else:
        assert run.deploy is None and run.stations[-1].status == "skipped"
    # the writer tried to read the hooks; the coder tried to edit generated tests and CRUD
    assert {v.rule for v in run.violations} == {"read_denied", "read_only"}
    assert {v.agent for v in run.violations} == {"test_writer", "coder"}
    writer = [it for it in run.iterations if it.agent == "test_writer"]
    assert [it.all_passed for it in writer] == [False, True]  # one-sided suite, then a good one
    assert [c.name for c in writer[0].checks if not c.passed] == ["cheat_always_reject"]
    assert [(c.name, c.passed) for c in run.cheat_probes] == [
        ("cheat_never_reject", True), ("cheat_always_reject", True)]
    coder = [it for it in run.iterations if it.agent == "coder"]
    assert [it.all_passed for it in coder] == [False, True]  # off-by-one limit, then fixed
    failing_first = [c for c in coder[0].checks if c.name == "pytest"]
    assert failing_first and not failing_first[0].passed
    project = tmp_path / "tasks" / "project"
    assert "RuleViolation" in (project / "app/rules/project.py").read_text()
    assert (project / "client" / "__init__.py").exists()  # the SDK was generated


def test_tasks_with_rules_that_accept_everything_fails_the_build(tmp_path: Path) -> None:
    cfg = entities_cfg(tmp_path, budget_iters_cap=2)
    run = build(BuildOptions(TASKS, run_id="tasks-cheat"), cfg,
                ScriptedProvider(SCRIPTS / "tasks_cheat" / "script.yaml"))
    assert run.status == "failed"
    writer = [it for it in run.iterations if it.agent == "test_writer"]
    assert [it.all_passed for it in writer] == [True]  # the suite itself was good
    coder = [it for it in run.iterations if it.agent == "coder"]
    assert len(coder) == 2
    pytest_result = [c for c in coder[-1].checks if c.name == "pytest"]
    assert pytest_result and not pytest_result[0].passed
    assert "test_the_21st_active_project_is_rejected" in "\n".join(pytest_result[0].failures)


def test_tasks_no_llm_build_reports_the_unimplemented_rules(tmp_path: Path) -> None:
    run = build(BuildOptions(TASKS, use_llm=False, run_id="tasks-det"), entities_cfg(tmp_path),
                None)
    assert run.status == "failed" and "stubs return 501" in run.status_reason
    statuses = {s.name: s.status for s in run.stations}
    assert statuses["postgres"] == "passed" and statuses["sdk"] == "passed"
    assert statuses["test_writer"] == "skipped" and statuses["coder"] == "skipped"
    assert statuses["baseline_checks"] == "failed"
    baseline = next(s for s in run.stations if s.name == "baseline_checks")
    pytest_check = next(c for c in baseline.checks if c.name == "pytest")
    assert not pytest_check.passed  # the generated tests that reach an unimplemented hook fail
    ruff = next(c for c in baseline.checks if c.name == "ruff")
    mypy = next(c for c in baseline.checks if c.name == "mypy")
    assert ruff.passed and mypy.passed  # the generated code itself is clean


def test_no_llm_build_reports_contract_failures(tmp_path: Path) -> None:
    run = build(BuildOptions(SPECS / "orders.yaml", use_llm=False, run_id="det"),
                FactoryConfig(runs_dir=tmp_path), None)
    assert run.status == "failed"
    assert run.contract is not None and not run.contract.passed
    by_type = run.contract.details["failures_by_type"]
    assert isinstance(by_type, dict) and "Server error" in by_type
    assert (tmp_path / "det" / "project" / "client" / "__init__.py").exists()
