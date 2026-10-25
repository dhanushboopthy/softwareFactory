import json
from pathlib import Path

from factory.config import FactoryConfig


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
