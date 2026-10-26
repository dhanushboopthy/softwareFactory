"""The dashboard, its formatting helpers and the environment panel."""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from factory.config import FactoryConfig
from factory.harness import sandbox
from factory.report.models import RunRecord, StationResult, Usage, now
from factory.report.runs import STALE_AFTER_S, RunSummary
from factory.report.writer import write_report
from factory.ui.app import create_app
from factory.ui.environment import (
    Check,
    Environment,
    check_deploy,
    check_postgres,
    check_provider,
    check_runs_dir,
    check_uv,
)
from factory.ui.security import UiSettings
from factory.ui.views import (
    REFRESH_S,
    fmt_cost,
    fmt_duration,
    fmt_time,
    refresh_seconds,
    run_row,
)

from .conftest import FIXTURES
from .test_ui_security import BASE, StubEnvironment, settings_for


def record(run_id: str, status: str = "passed", started: datetime | None = None,
           **extra: Any) -> RunRecord:
    return RunRecord(run_id=run_id, spec_name="tasks", spec_path="/x", spec_sha256="h",
                     provider="scripted", model="scripted", status=status,  # type: ignore[arg-type]
                     started_at=started or now(), **extra)


def save(settings: UiSettings, rec: RunRecord) -> None:
    write_report(rec, settings.cfg.runs_dir / rec.run_id)


def dashboard(settings: UiSettings, checks: list[Check] | None = None) -> str:
    client = TestClient(create_app(settings, StubEnvironment(settings, checks)), base_url=BASE)
    response = client.get("/")
    assert response.status_code == 200
    return response.text


# --- the page -----------------------------------------------------------------------------------


def test_an_empty_dashboard_says_how_to_start(tmp_path: Path) -> None:
    html = dashboard(settings_for(tmp_path))
    assert "No runs yet" in html and "factory build" in html
    assert "<table" not in html and 'http-equiv="refresh"' not in html


def test_runs_are_listed_newest_first_with_status_chips(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    base = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
    save(settings, record("old-ok", started=base, profile="openapi",
                          usage=Usage(cost_usd=0.5)))
    save(settings, record("newer-bad", "failed", started=base + timedelta(hours=1),
                          profile="entities", status_reason="failed stations: contract"))
    save(settings, record("newest-stopped", "cancelled", started=base + timedelta(hours=2)))
    html = dashboard(settings)
    assert html.index("newest-stopped") < html.index("newer-bad") < html.index("old-ok")
    assert 'href="/runs/old-ok"' in html
    assert '<span class="chip ok">passed</span>' in html
    assert '<span class="chip bad">failed</span>' in html
    assert '<span class="chip warn">cancelled</span>' in html
    assert "failed stations: contract" in html
    assert "$0.50" in html and "entities" in html and "OpenAPI" in html


def test_the_page_refreshes_only_while_something_is_running(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    save(settings, record("done"))
    assert 'http-equiv="refresh"' not in dashboard(settings)
    running = record("busy", "running")
    running.stations.append(StationResult(name="test_writer", status="passed"))
    save(settings, running)
    html = dashboard(settings)
    assert f'<meta http-equiv="refresh" content="{REFRESH_S}">' in html
    assert "at test_writer" in html and '<span class="chip run">running</span>' in html


def test_a_run_that_stopped_writing_is_shown_as_stalled_not_running(tmp_path: Path) -> None:
    import os

    settings = settings_for(tmp_path)
    save(settings, record("zombie", "running"))
    old = (datetime.now(UTC) - timedelta(seconds=STALE_AFTER_S + 120)).timestamp()
    os.utime(settings.cfg.runs_dir / "zombie" / "run.json", (old, old))
    html = dashboard(settings)
    assert "stalled" in html and "may have crashed" in html
    assert 'http-equiv="refresh"' not in html  # a dead run must not keep the page reloading


def test_a_damaged_run_json_is_shown_but_not_linked(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    save(settings, record("fine"))
    broken = settings.cfg.runs_dir / "broken"
    broken.mkdir()
    (broken / "run.json").write_text("{not json")
    html = dashboard(settings)
    assert 'href="/runs/fine"' in html and 'href="/runs/broken"' not in html
    assert "unreadable" in html


def test_the_environment_panel_shows_marks_details_and_fixes(tmp_path: Path) -> None:
    checks = [Check("LLM provider", True, "scripted / scripted"),
              Check("Postgres (Docker)", False, "Cannot connect to the Docker daemon",
                    "Start Docker, or set FACTORY_POSTGRES_BACKEND=local."),
              Check("Runs directory", None, "runs (created by the first build)")]
    html = dashboard(settings_for(tmp_path), checks)
    assert "✓" in html and "✗" in html and "–" in html
    assert "Cannot connect to the Docker daemon" in html
    assert "Start Docker, or set FACTORY_POSTGRES_BACKEND=local." in html


def test_no_secret_value_reaches_the_page(tmp_path: Path) -> None:
    canary = "sk-ant-THIS-MUST-NEVER-BE-SHOWN"
    cfg = FactoryConfig(runs_dir=tmp_path / "runs", postgres_backend="local",
                        postgres_bin_dir=str(tmp_path / "none"), deploy_check=False)
    settings = UiSettings(cfg=cfg, env={"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "",
                                        "ANTHROPIC_API_KEY": canary, "FACTORY_UI_TOKEN": canary})
    client = TestClient(create_app(settings), base_url=BASE)  # the real environment checks
    html = client.get("/").text
    assert canary not in html and "OPENAI_API_KEY is not set" in html


# --- formatting helpers -------------------------------------------------------------------------


def test_cost_and_time_formatting() -> None:
    assert (fmt_cost(None), fmt_cost(0.0), fmt_cost(1.239)) == ("n/a", "$0.00", "$1.24")
    assert fmt_time(None) == "—"
    assert len(fmt_time(datetime(2026, 10, 26, 9, 5, tzinfo=UTC))) == len("2026-10-26 09:05")


@pytest.mark.parametrize(("seconds", "expected"), [
    (5, "5s"), (89, "89s"), (90, "1 min"), (600, "10 min"), (5399, "89 min"), (7200, "2.0 h")])
def test_duration_formatting(seconds: int, expected: str) -> None:
    start = datetime(2026, 10, 26, tzinfo=UTC)
    assert fmt_duration(start, start + timedelta(seconds=seconds), start) == expected
    assert fmt_duration(None, None, start) == "—"
    assert fmt_duration(start, None, start + timedelta(seconds=30)) == "30s"  # still running


def summary(status: str, **kw: Any) -> RunSummary:
    return RunSummary(run_id="r", updated_at=datetime.now(UTC), spec_name="tasks",
                      profile="entities", status=status, started_at=now(), **kw)


@pytest.mark.parametrize(("status", "css"), [
    ("passed", "ok"), ("running", "run"), ("failed", "bad"), ("error", "bad"),
    ("budget_exhausted", "warn"), ("cancelled", "warn")])
def test_every_status_has_a_chip_style(status: str, css: str) -> None:
    assert run_row(summary(status)).status_class == css


def test_run_row_details() -> None:
    failed = run_row(summary("failed", status_reason="failed stations: coder", iterations=3,
                             cost_usd=1.5))
    assert (failed.kind, failed.detail, failed.iterations, failed.cost) == (
        "entities", "failed stations: coder", 3, "$1.50")
    running = run_row(summary("running", current_station="postgres"))
    assert running.detail == "at postgres" and not running.stale
    assert run_row(summary("running")).detail == "starting"
    openapi = RunSummary(run_id="r", updated_at=now(), profile="openapi", status="passed")
    assert run_row(openapi).kind == "OpenAPI"


def test_unreadable_runs_get_a_plain_row() -> None:
    row = run_row(RunSummary(run_id="x", updated_at=now(), readable=False, error="boom"))
    assert (row.status, row.status_class, row.readable, row.detail) == (
        "unreadable", "bad", False, "boom")


def test_refresh_follows_the_runs() -> None:
    assert refresh_seconds([]) is None
    assert refresh_seconds([summary("passed")]) is None
    assert refresh_seconds([summary("passed"), summary("running")]) == REFRESH_S
    long_ago = datetime.now(UTC) - timedelta(seconds=STALE_AFTER_S + 5)
    stalled = RunSummary(run_id="r", updated_at=long_ago, status="running")
    assert refresh_seconds([stalled]) is None


# --- environment checks -------------------------------------------------------------------------


def cmd_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> sandbox.CommandResult:
    return sandbox.CommandResult(("docker",), returncode, stdout, stderr, 0.1, False)


def fake_run(monkeypatch: pytest.MonkeyPatch, result: sandbox.CommandResult | None = None,
             raises: Exception | None = None) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd: Sequence[str], cwd: Path, timeout_s: float,
            env: Mapping[str, str] | None = None) -> sandbox.CommandResult:
        calls.append(list(cmd))
        if raises:
            raise raises
        assert result is not None
        return result

    monkeypatch.setattr(sandbox, "run", run)
    return calls


def settings_with(tmp_path: Path, env: dict[str, str] | None = None, **cfg: Any) -> UiSettings:
    config = FactoryConfig(runs_dir=tmp_path / "runs", **cfg)
    return UiSettings(cfg=config, env=env or {})


def test_provider_check(tmp_path: Path) -> None:
    script = FIXTURES / "scripted" / "ping.yaml"
    good = check_provider(settings_with(tmp_path, {"LLM_PROVIDER": "scripted",
                                                   "LLM_SCRIPT": str(script)}))
    assert good.ok is True and good.detail == "scripted / scripted"
    bad = check_provider(settings_with(tmp_path, {}))
    assert bad.ok is False and "LLM_PROVIDER" in bad.detail and "--no-llm" in bad.hint
    keyless = check_provider(settings_with(tmp_path, {"LLM_PROVIDER": "deepseek"}))
    assert keyless.ok is False and keyless.detail == "DEEPSEEK_API_KEY is not set"


def test_docker_check_reports_the_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, cmd_result(0, "27.3.1\n"))
    check = check_postgres(settings_with(tmp_path, {"DOCKER_HOST": "unix:///x"}))
    assert check.ok is True and check.detail == "Docker 27.3.1"
    assert calls == [["docker", "info", "--format", "{{.ServerVersion}}"]]


def test_docker_check_explains_how_to_fix_it(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, cmd_result(1, "", "permission denied while trying to connect\nmore"))
    check = check_postgres(settings_with(tmp_path))
    assert check.ok is False and check.detail == "permission denied while trying to connect"
    assert "FACTORY_POSTGRES_BACKEND=local" in check.hint


def test_a_missing_docker_binary_is_reported(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, raises=FileNotFoundError("docker"))
    check = check_postgres(settings_with(tmp_path))
    assert check.ok is False and "not found" in check.detail


def test_the_local_backend_checks_the_binaries(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, cmd_result())
    bin_dir = tmp_path / "pg"
    settings = settings_with(tmp_path, postgres_backend="local", postgres_bin_dir=str(bin_dir))
    missing = check_postgres(settings)
    assert missing.ok is False and "initdb" in missing.detail
    bin_dir.mkdir()
    for tool in ("initdb", "postgres", "pg_isready", "psql"):
        (bin_dir / tool).touch()
    assert check_postgres(settings).ok is True
    assert calls == []  # Docker is never asked about


def test_deploy_check_combinations(tmp_path: Path) -> None:
    docker_ok = Check("Postgres (Docker)", True, "Docker 27")
    docker_bad = Check("Postgres (Docker)", False, "down")
    assert check_deploy(settings_with(tmp_path, deploy_check=False), docker_ok).ok is None
    assert check_deploy(settings_with(tmp_path), docker_ok).ok is True
    assert check_deploy(settings_with(tmp_path), docker_bad).ok is False
    local = check_deploy(settings_with(tmp_path, postgres_backend="local"), docker_ok)
    assert local.ok is None and "FACTORY_DEPLOY_CHECK=0" in local.hint


def test_uv_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "find_uv", lambda: "/usr/bin/uv")
    assert check_uv().ok is True

    def missing() -> str:
        raise FileNotFoundError("uv")

    monkeypatch.setattr(sandbox, "find_uv", missing)
    assert check_uv().ok is False


def test_runs_directory_check(tmp_path: Path) -> None:
    assert check_runs_dir(settings_with(tmp_path)).ok is None   # not created yet
    (tmp_path / "runs").mkdir()
    assert check_runs_dir(settings_with(tmp_path)).ok is True
    file_path = tmp_path / "runs-file"
    file_path.write_text("x")
    cfg = FactoryConfig(runs_dir=file_path)
    assert check_runs_dir(UiSettings(cfg=cfg, env={})).ok is False


def test_checks_are_cached_for_a_while(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_run(monkeypatch, cmd_result(0, "27\n"))
    now_s = [100.0]
    environment = Environment(settings_with(tmp_path), ttl_s=30, clock=lambda: now_s[0])
    first = environment.checks()
    now_s[0] = 120.0
    assert environment.checks() is first and len(calls) == 1
    now_s[0] = 131.0
    assert environment.checks() is not first and len(calls) == 2


def test_ready_to_build_ignores_the_provider(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fake_run(monkeypatch, cmd_result(0, "27\n"))
    monkeypatch.setattr(sandbox, "find_uv", lambda: "/usr/bin/uv")
    ready = Environment(settings_with(tmp_path, deploy_check=False))
    assert ready.ready_to_build()          # no provider configured, but --no-llm builds work
    fake_run(monkeypatch, cmd_result(1, "", "daemon down"))
    assert not Environment(settings_with(tmp_path)).ready_to_build()
