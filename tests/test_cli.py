from pathlib import Path

from typer.testing import CliRunner

from factory import __version__
from factory.cli import app

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


def test_llm_ping_with_scripted_provider() -> None:
    script = Path(__file__).parent / "fixtures" / "scripted" / "ping.yaml"
    result = runner.invoke(app, ["llm-ping"],
                           env={"LLM_PROVIDER": "scripted", "LLM_SCRIPT": str(script)})
    assert result.exit_code == 0, result.output
    assert '"name": "list_files"' in result.output
    assert '"stop_reason": "tool_use"' in result.output
