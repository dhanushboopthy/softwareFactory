import json
from pathlib import Path

import pytest

from factory.config import ENTITIES_DDL_PATTERNS, ENTITIES_READ_ONLY, FactoryConfig


def test_defaults_match_guardrail_table() -> None:
    cfg = FactoryConfig()
    assert cfg.max_iters == 8
    assert cfg.max_tool_calls_per_iter == 20
    assert cfg.max_lines_changed_per_iter == 400
    assert cfg.command_timeout_s == 120
    assert "app/models.py" in cfg.read_only
    assert "openapi.yaml" in cfg.read_only
    assert "tests/**" in cfg.coder_read_only
    assert "tests/**" not in cfg.coder_writable
    assert cfg.test_writer_writable == ("tests/test_*.py",)


def test_env_overrides_scalars() -> None:
    cfg = FactoryConfig.from_env(
        {
            "FACTORY_MAX_ITERS": "3",
            "FACTORY_MAX_COST_USD_PER_RUN": "0.5",
            "FACTORY_RUNS_DIR": "out/runs",
        }
    )
    assert cfg.max_iters == 3
    assert cfg.max_cost_usd_per_run == 0.5
    assert cfg.runs_dir == Path("out/runs")


@pytest.mark.parametrize(("raw", "expected"), [
    ("0", False), ("false", False), ("no", False), ("", False),
    ("1", True), ("true", True), ("YES", True), ("on", True),
])
def test_boolean_env_overrides(raw: str, expected: bool) -> None:
    assert FactoryConfig().deploy_check is True
    assert FactoryConfig.from_env({"FACTORY_DEPLOY_CHECK": raw}).deploy_check is expected


def test_env_ignores_unrelated_and_tuple_fields() -> None:
    cfg = FactoryConfig.from_env({"FACTORY_READ_ONLY": "nothing", "OTHER": "x"})
    assert cfg == FactoryConfig()


def test_to_dict_is_json_serialisable() -> None:
    json.dumps(FactoryConfig().to_dict())


def test_openapi_profile_is_exactly_the_defaults() -> None:
    cfg = FactoryConfig()
    assert cfg.profile == "openapi"
    assert cfg.for_profile("openapi") == cfg


def test_entities_profile_rules() -> None:
    base = FactoryConfig()
    cfg = base.for_profile("entities")
    assert cfg.profile == "entities"
    assert cfg.coder_writable == ("app/rules/**",)
    assert set(base.read_only) < set(cfg.read_only)
    assert set(ENTITIES_READ_ONLY) <= set(cfg.read_only)
    assert {"entities.yaml", "tests/generated/**", "app/routes/**", "app/crud/**", "app/main.py",
            "app/orm.py", "app/auth.py", "app/settings.py", "app/db.py", "app/errors.py",
            "migrations/**", "alembic.ini", "Dockerfile", "compose.yaml",
            ".dockerignore"} == set(ENTITIES_READ_ONLY)
    assert {"openapi.yaml", "app/models.py", "tests/conftest.py"} <= set(cfg.read_only)
    assert "app/rules/**" in cfg.test_writer_unreadable
    assert set(ENTITIES_DDL_PATTERNS) <= set(cfg.forbidden_patterns)
    assert cfg.coder_read_only == base.coder_read_only
    assert cfg.test_writer_writable == base.test_writer_writable
    assert (cfg.max_iters, cfg.max_lines_changed_per_iter, cfg.max_cost_usd_per_run) == (
        base.max_iters, base.max_lines_changed_per_iter, base.max_cost_usd_per_run)


@pytest.mark.parametrize(("items", "iters", "lines", "cost"), [
    (0, 4, 150, 1.0),
    (1, 6, 250, 1.75),
    (3, 10, 450, 3.25),
    (6, 16, 600, 5.5),      # iterations and lines hit their caps
    (50, 16, 600, 10.0),    # so does cost
])
def test_budgets_scale_with_the_llm_workload(items: int, iters: int, lines: int,
                                             cost: float) -> None:
    scaled = FactoryConfig().for_profile("entities").scaled_for(items)
    assert (scaled.max_iters, scaled.max_lines_changed_per_iter, scaled.max_cost_usd_per_run) == (
        iters, lines, cost)


def test_scaling_changes_only_the_budgets() -> None:
    base = FactoryConfig().for_profile("entities")
    scaled = base.scaled_for(3)
    assert scaled.profile == "entities" and scaled.coder_writable == base.coder_writable
    assert scaled.command_timeout_s == base.command_timeout_s
    assert base.max_iters == 8  # the original is untouched (frozen)


def test_the_caps_are_configurable_and_scaling_rejects_nonsense() -> None:
    cfg = FactoryConfig.from_env(
        {"FACTORY_BUDGET_ITERS_CAP": "2", "FACTORY_BUDGET_COST_CAP": "0.5"})
    scaled = cfg.scaled_for(10)
    assert scaled.max_iters == 2 and scaled.max_cost_usd_per_run == 0.5
    with pytest.raises(ValueError, match="negative"):
        FactoryConfig().scaled_for(-1)


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        FactoryConfig().for_profile("graphql")


def test_profile_cannot_come_from_the_environment() -> None:
    assert FactoryConfig.from_env({"FACTORY_PROFILE": "entities"}).profile == "openapi"
