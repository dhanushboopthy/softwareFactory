from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from factory import __version__, pipeline
from factory.cli import app
from factory.config import FactoryConfig
from factory.entities import load_entities_raw
from factory.pipeline import BuildOptions
from factory.report.models import RunRecord

from .conftest import ROOT, SPECS

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "build" in result.output
    assert "report" in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_build_rejects_missing_spec(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build", str(tmp_path / "nope.yaml")])
    assert result.exit_code != 0


def test_build_without_provider_explains_how_to_configure(tmp_path: Path) -> None:
    spec = tmp_path / "s.yaml"
    spec.write_text("openapi: 3.1.0\n")
    result = runner.invoke(app, ["build", str(spec)], env={"LLM_PROVIDER": ""})
    assert result.exit_code == 2
    assert "--no-llm" in result.output


def entities_file(tmp_path: Path, approved: bool = True) -> Path:
    raw = load_entities_raw(ROOT / "examples" / "tasks" / "entities.yaml")
    raw["approved"] = approved
    path = tmp_path / "tasks.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def test_incremental_flags_are_refused_for_entities_files(tmp_path: Path) -> None:
    path = entities_file(tmp_path)
    for flags in (["--from", "earlier-run"], ["--incremental"]):
        result = runner.invoke(app, ["build", str(path), "--no-llm", *flags],
                               env={"FACTORY_RUNS_DIR": str(tmp_path / "runs")})
        assert result.exit_code == 2, flags
        assert "Open Question 2" in result.output and "full build" in result.output
    assert not (tmp_path / "runs").exists()


def test_incremental_flags_still_work_for_openapi_specs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str | None] = {}

    def fake_build(opts: BuildOptions, cfg: FactoryConfig, provider: object,
                   log: object) -> RunRecord:
        seen["previous"] = opts.previous_run
        return RunRecord(run_id="x", spec_name="orders.yaml", spec_path="p", spec_sha256="s",
                         provider="none", model="none", status="passed")

    monkeypatch.setattr(pipeline, "build", fake_build)
    result = runner.invoke(app, ["build", str(SPECS / "orders.yaml"), "--from", "earlier",
                                 "--no-llm"], env={"FACTORY_RUNS_DIR": str(tmp_path / "runs")})
    assert result.exit_code == 0, result.output
    assert seen == {"previous": "earlier"}  # an OpenAPI build is not refused


def test_an_unapproved_entities_file_exits_1_with_a_report(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build", str(entities_file(tmp_path, approved=False)),
                                 "--no-llm", "--run-id", "unapproved"],
                           env={"FACTORY_RUNS_DIR": str(tmp_path / "runs")})
    assert result.exit_code == 1
    assert "not approved" in result.output and "approved: true" in result.output
    assert (tmp_path / "runs" / "unapproved" / "report.md").exists()


def test_llm_ping_with_scripted_provider() -> None:
    script = Path(__file__).parent / "fixtures" / "scripted" / "ping.yaml"
    result = runner.invoke(app, ["llm-ping"],
                           env={"LLM_PROVIDER": "scripted", "LLM_SCRIPT": str(script)})
    assert result.exit_code == 0, result.output
    assert '"name": "list_files"' in result.output
    assert '"stop_reason": "tool_use"' in result.output
