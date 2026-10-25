import json
from pathlib import Path

import pytest

from factory.config import ENTITIES_DDL_PATTERNS, FactoryConfig


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
    assert {"entities.yaml", "tests/generated/**"} <= set(cfg.read_only)
    assert "app/rules/**" in cfg.test_writer_unreadable
    assert set(ENTITIES_DDL_PATTERNS) <= set(cfg.forbidden_patterns)
    assert cfg.coder_read_only == base.coder_read_only
    assert cfg.test_writer_writable == base.test_writer_writable
    assert (cfg.max_iters, cfg.max_lines_changed_per_iter, cfg.max_cost_usd_per_run) == (
        base.max_iters, base.max_lines_changed_per_iter, base.max_cost_usd_per_run)


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        FactoryConfig().for_profile("graphql")


def test_profile_cannot_come_from_the_environment() -> None:
    assert FactoryConfig.from_env({"FACTORY_PROFILE": "entities"}).profile == "openapi"
