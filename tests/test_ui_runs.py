"""The run page, the file viewer and the downloads."""

import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from factory.report.models import (
    CheckResult,
    Iteration,
    RunRecord,
    StationResult,
    Usage,
    Violation,
    now,
)
from factory.report.runs import STALE_AFTER_S
from factory.report.writer import write_report
from factory.ui import files
from factory.ui.app import create_app
from factory.ui.security import UiSettings
from factory.ui.views import REFRESH_S, budget_bars, check_chip, station_chip

from .test_ui_security import BASE, StubEnvironment, settings_for


def finished_record(run_id: str = "tasks", **extra: Any) -> RunRecord:
    record = RunRecord(
        run_id=run_id, spec_name="tasks", spec_path="/x", spec_sha256="ab" * 32,
        provider="scripted", model="scripted", status="passed", profile="entities",
        approved=True, usage=Usage(llm_calls=7, input_tokens=1200, output_tokens=300,
                                   cost_usd=0.5),
        config={"max_iters": 10, "max_cost_usd_per_run": 3.25}, **extra)
    record.stations += [
        StationResult(name="lint", status="passed", detail="expanded to 9 paths",
                      ended_at=now()),
        StationResult(name="test_writer", status="passed", detail="suite accepted",
                      ended_at=now()),
        StationResult(name="contract", status="failed", detail="3 failures", ended_at=now())]
    violation = Violation(rule="read_only", agent="coder", path="app/crud/project.py",
                          detail="app/crud/project.py is read-only for coder", iteration=1)
    record.violations.append(violation)
    record.iterations += [
        Iteration(agent="test_writer", index=1, all_passed=False, tool_calls=3,
                  lines_added=40, failure_summary="### cheat_always_reject: NOT caught",
                  checks=[CheckResult(name="test_quality", passed=True, summary="ok"),
                          CheckResult(name="cheat_always_reject", passed=False,
                                      summary="NOT caught")]),
        Iteration(agent="coder", index=1, all_passed=True, lines_added=12, lines_removed=2,
                  diff_path="iterations/coder-01.diff", violations=[violation],
                  checks=[CheckResult(name="pytest", passed=True, summary="30 passed"),
                          CheckResult(name="schemathesis", passed=False, skipped=True,
                                      summary="skipped")])]
    record.cheat_probes = [
        CheckResult(name="cheat_never_reject", passed=True, summary="caught"),
        CheckResult(name="cheat_always_reject", passed=False, summary="NOT caught")]
    record.contract = CheckResult(name="contract", passed=False, summary="3 unique failures",
                                  failures=["POST /projects: Server error"])
    record.deploy = CheckResult(name="deploy", passed=True, summary="compose project healthy")
    record.prompt_versions = {"coder_v2.system.j2": "abc123def456"}
    record.ended_at = now()
    return record


def make_run(settings: UiSettings, record: RunRecord, project: bool = True) -> Path:
    run_dir = settings.cfg.runs_dir / record.run_id
    write_report(record, run_dir)
    (run_dir / "iterations").mkdir(exist_ok=True)
    (run_dir / "iterations" / "coder-01.diff").write_text("--- a/x\n+++ b/x\n+<b>new</b>\n")
    (run_dir / "checks" / "baseline").mkdir(parents=True, exist_ok=True)
    (run_dir / "checks" / "baseline" / "pytest.log").write_text("1 failed\n")
    if project:
        for rel, content in {
            "openapi.yaml": "openapi: 3.1.0\n", "pyproject.toml": "[project]\n",
            "app/main.py": "app = 1\n", "app/rules/project.py": "def archive(): ...\n",
            "client/__init__.py": "# sdk\n", ".env": "JWT_SECRET=abc", ".venv/lib/x.py": "venv",
            "app.db": "sqlite", ".env.example": "JWT_SECRET=\n",
        }.items():
            path = run_dir / "project" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    return run_dir


@pytest.fixture
def settings(tmp_path: Path) -> UiSettings:
    return settings_for(tmp_path)


@pytest.fixture
def client(settings: UiSettings) -> TestClient:
    return TestClient(create_app(settings, StubEnvironment(settings)), base_url=BASE)


# --- the run page -------------------------------------------------------------------------------


def test_a_finished_run_shows_everything(settings: UiSettings, client: TestClient) -> None:
    make_run(settings, finished_record())
    html = client.get("/runs/tasks").text
    assert "tasks" in html and "entities file" in html and "approved: yes" in html
    assert "sha256 abababababab" in html
    assert "scripted / scripted" in html and "7 calls" in html and "$0.50" in html
    # stations with their details
    assert "expanded to 9 paths" in html and "suite accepted" in html
    assert '<span class="chip ok">passed</span>' in html
    assert '<span class="chip bad">failed</span>' in html
    # iterations, their checks, the diff link and the violation
    assert "test_writer #1" in html and "coder #1" in html
    assert 'href="/runs/tasks/files/iterations/coder-01.diff"' in html
    assert "cheat_always_reject: fail" in html and "pytest: pass" in html
    assert "schemathesis: skipped" in html
    assert "app/crud/project.py is read-only for coder" in html
    assert "### cheat_always_reject: NOT caught" in html
    # probes, contract, deploy, prompts
    assert "NOT caught" in html and "caught" in html
    assert "3 unique failures" in html and "POST /projects: Server error" in html
    assert "compose project healthy" in html
    assert "coder_v2.system.j2" in html and "abc123def456" in html


def test_budget_bars_show_progress_against_the_limits(settings: UiSettings,
                                                      client: TestClient) -> None:
    make_run(settings, finished_record())
    html = client.get("/runs/tasks").text
    assert '<progress max="10.0" value="1.0">' in html and "1 of 10" in html
    assert '<progress max="3.25" value="0.5">' in html and "$0.50 of $3.25" in html
    bars = budget_bars(finished_record())
    assert [b[0] for b in bars] == ["Coder iterations", "Cost"]
    assert budget_bars(RunRecord(run_id="x", spec_name="x", spec_path="p", spec_sha256="h",
                                 provider="p", model="m")) == []


def test_the_page_does_not_refresh_once_the_run_is_over(settings: UiSettings,
                                                        client: TestClient) -> None:
    make_run(settings, finished_record())
    assert "http-equiv" not in client.get("/runs/tasks").text


def test_a_running_run_refreshes_and_marks_its_open_station(settings: UiSettings,
                                                            client: TestClient) -> None:
    record = finished_record("busy")
    record.status, record.ended_at, record.contract, record.deploy = "running", None, None, None
    record.stations = [StationResult(name="lint", status="passed", ended_at=now()),
                       StationResult(name="coder", status="passed")]  # still open
    make_run(settings, record)
    html = client.get("/runs/busy").text
    assert f'<meta http-equiv="refresh" content="{REFRESH_S}">' in html
    assert '<span class="chip run">running</span>' in html  # the status and the open station
    assert html.count('<span class="chip run">') == 2
    assert "Not run yet" in html


def test_a_run_that_stopped_updating_is_shown_as_stalled(settings: UiSettings,
                                                         client: TestClient) -> None:
    record = finished_record("zombie")
    record.status, record.ended_at = "running", None
    record.stations = [StationResult(name="coder", status="passed")]
    run_dir = make_run(settings, record)
    old = (datetime.now(UTC) - timedelta(seconds=STALE_AFTER_S + 120)).timestamp()
    os.utime(run_dir / "run.json", (old, old))
    html = client.get("/runs/zombie").text
    assert "may have crashed" in html and "http-equiv" not in html
    assert '<span class="chip warn">stalled</span>' in html


def test_an_openapi_run_has_no_entities_sections(settings: UiSettings, client: TestClient) -> None:
    record = finished_record("v1")
    record.profile, record.approved = "openapi", None
    record.cheat_probes, record.deploy = [], None
    make_run(settings, record)
    html = client.get("/runs/v1").text
    assert "OpenAPI spec" in html
    for absent in ("Anti-cheating probes", "Deploy check", "approved:"):
        assert absent not in html


def test_hostile_text_in_a_record_is_escaped(settings: UiSettings, client: TestClient) -> None:
    record = finished_record("evil")
    record.spec_name = "<script>alert(1)</script>"
    record.error = "</pre><script>alert(2)</script>"
    record.iterations[0].failure_summary = '"><img src=x onerror=alert(3)>'
    make_run(settings, record)
    html = client.get("/runs/evil").text
    assert "<script" not in html and "<img src=x" not in html and "</pre><script" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# (A literal `..` is normalised away by HTTP clients before it is sent; run_dir_for rejects it.)
@pytest.mark.parametrize("run_id", ["missing", "%2e%2e", "a%2fb", ".hidden", "x" * 200,
                                    "tasks%00", "tasks%0a"])
def test_unknown_or_hostile_run_ids_are_a_plain_404(client: TestClient, run_id: str) -> None:
    assert client.get(f"/runs/{run_id}").status_code == 404


def test_a_damaged_run_json_gives_a_clear_page(settings: UiSettings, client: TestClient) -> None:
    broken = settings.cfg.runs_dir / "broken"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text("{nope")
    response = client.get("/runs/broken")
    assert response.status_code == 500 and "may be damaged" in response.text


def test_a_symlinked_run_directory_is_not_followed(settings: UiSettings, tmp_path: Path,
                                                   client: TestClient) -> None:
    outside = tmp_path / "outside"
    write_report(finished_record("evil"), outside)
    settings.cfg.runs_dir.mkdir(parents=True, exist_ok=True)
    (settings.cfg.runs_dir / "evil").symlink_to(outside)
    assert client.get("/runs/evil").status_code == 404


# --- chips --------------------------------------------------------------------------------------


def test_check_and_station_chips() -> None:
    assert check_chip(CheckResult(name="x", passed=True, summary="")) == ("ok", "pass")
    assert check_chip(CheckResult(name="x", passed=False, summary="")) == ("bad", "fail")
    assert check_chip(CheckResult(name="x", passed=False, skipped=True, summary="")) == (
        "na", "skipped")
    open_station = StationResult(name="coder", status="passed")
    assert station_chip(open_station, True) == ("run", "running")
    assert station_chip(open_station, False, run_stalled=True) == ("warn", "stalled")
    done = StationResult(name="lint", status="failed", ended_at=now())
    assert station_chip(done, True) == ("bad", "failed")
    assert station_chip(StationResult(name="x", status="skipped", ended_at=now()), False) == (
        "na", "skipped")


# --- the file viewer ----------------------------------------------------------------------------


def test_the_root_lists_only_the_allowed_places(settings: UiSettings, client: TestClient) -> None:
    run_dir = make_run(settings, finished_record())
    (run_dir / ".schemathesis-home").mkdir()
    (run_dir / "secret-notes.txt").write_text("x")
    html = client.get("/runs/tasks/files").text
    for shown in ("project/", "iterations/", "checks/", "report.md", "run.json"):
        assert shown in html, shown
    assert "secret-notes" not in html and ".schemathesis-home" not in html


def test_files_and_directories_are_shown(settings: UiSettings, client: TestClient) -> None:
    make_run(settings, finished_record())
    listing = client.get("/runs/tasks/files/project/app").text
    assert "main.py" in listing and "rules/" in listing
    code = client.get("/runs/tasks/files/project/app/rules/project.py")
    assert code.status_code == 200 and "def archive(): ..." in code.text
    assert "&lt;b&gt;new&lt;/b&gt;" in client.get("/runs/tasks/files/iterations/coder-01.diff").text
    assert "1 failed" in client.get("/runs/tasks/files/checks/baseline/pytest.log").text
    assert 'href="/runs/tasks/files/project/app"' in code.text  # breadcrumbs


# Raw `/../` and `/./` segments are removed by HTTP clients before sending, so they cannot be
# tested here; test_resolve_unit_cases covers them, and the encoded forms are below.
@pytest.mark.parametrize("path", [
    "project/.env", "project/.venv/lib/x.py", "project/.venv", "project/app.db",
    "%2e%2e/tasks", "project/%2e%2e/run.json", "project/app/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
    "%2e%2e/%2e%2e/etc/passwd", "secret-notes.txt", ".schemathesis-home", "project/nothing",
    "project%5Capp", "project//app", "project%00",
])
def test_hostile_or_hidden_paths_are_a_404(settings: UiSettings, client: TestClient,
                                           path: str) -> None:
    run_dir = make_run(settings, finished_record())
    (run_dir / "secret-notes.txt").write_text("private")
    (run_dir / ".schemathesis-home").mkdir()
    response = client.get(f"/runs/tasks/files/{path}")
    assert response.status_code == 404
    assert "JWT_SECRET=abc" not in response.text and "private" not in response.text


def test_symlinks_inside_a_run_are_never_followed(settings: UiSettings, tmp_path: Path,
                                                  client: TestClient) -> None:
    run_dir = make_run(settings, finished_record())
    outside = tmp_path / "outside.txt"
    outside.write_text("do not leak")
    (run_dir / "project" / "link.txt").symlink_to(outside)
    (run_dir / "project" / "linked").symlink_to(tmp_path)
    (run_dir / "checks" / "escape").symlink_to(tmp_path)
    for path in ("project/link.txt", "project/linked/outside.txt", "checks/escape/outside.txt"):
        response = client.get(f"/runs/tasks/files/{path}")
        assert response.status_code == 404 and "do not leak" not in response.text
    listing = client.get("/runs/tasks/files/project").text
    assert "link.txt" not in listing and "linked" not in listing


def test_large_and_binary_files(settings: UiSettings, client: TestClient) -> None:
    run_dir = make_run(settings, finished_record())
    (run_dir / "checks" / "big.log").write_text("x" * (files.MAX_VIEW_BYTES + 50))
    (run_dir / "checks" / "blob.log").write_bytes(b"\x00\x01binary")
    big = client.get("/runs/tasks/files/checks/big.log").text
    assert f"Only the first {files.MAX_VIEW_BYTES} bytes are shown" in big
    assert "binary file" in client.get("/runs/tasks/files/checks/blob.log").text


def test_resolve_unit_cases(tmp_path: Path) -> None:
    run_dir = tmp_path / "r"
    (run_dir / "project" / "app").mkdir(parents=True)
    (run_dir / "project" / "app" / "x.py").write_text("x")
    (run_dir / "report.md").write_text("r")
    assert files.resolve(run_dir, "") == run_dir
    assert files.resolve(run_dir, "project/app/x.py").name == "x.py"
    assert files.resolve(run_dir, "report.md").name == "report.md"
    for bad in ("report.md/x", "run.jsonx", "project/../x", "project/./app", "project//app",
                "/etc/passwd", "project/app/x.pyc", "project/.env", "a\x00b", "project/ap\\p",
                "../r", "project/../../r"):
        with pytest.raises(files.NotViewable):
            files.resolve(run_dir, bad)
    assert files.breadcrumbs("project/app/x.py") == [
        ("project", "project"), ("app", "project/app"), ("x.py", "project/app/x.py")]


# --- downloads ----------------------------------------------------------------------------------


def test_the_project_download_is_a_clean_zip(settings: UiSettings, client: TestClient) -> None:
    make_run(settings, finished_record())
    response = client.get("/runs/tasks/download/project")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="tasks.zip"'
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    assert "tasks/app/rules/project.py" in names and "tasks/client/__init__.py" in names
    assert "tasks/.env.example" in names
    joined = " ".join(names)
    assert ".env\n" not in joined and "tasks/.env" not in names
    assert ".venv" not in joined and "app.db" not in joined


def test_the_client_and_spec_downloads(settings: UiSettings, client: TestClient) -> None:
    make_run(settings, finished_record())
    sdk = client.get("/runs/tasks/download/client")
    assert zipfile.ZipFile(io.BytesIO(sdk.content)).namelist() == ["tasks-client/__init__.py"]
    spec = client.get("/runs/tasks/download/openapi")
    assert spec.text == "openapi: 3.1.0\n"
    assert spec.headers["content-disposition"] == 'attachment; filename="tasks-openapi.yaml"'


def test_missing_things_and_unknown_downloads_are_a_404(settings: UiSettings,
                                                        client: TestClient) -> None:
    make_run(settings, finished_record("bare"), project=False)
    for what in ("project", "client", "openapi", "everything", "../x"):
        assert client.get(f"/runs/bare/download/{what}").status_code == 404, what
    assert client.get("/runs/nope/download/project").status_code == 404


def test_links_to_downloads_appear_only_when_there_is_something_to_download(
        settings: UiSettings, client: TestClient) -> None:
    make_run(settings, finished_record("full"))
    make_run(settings, finished_record("bare"), project=False)
    full, bare = client.get("/runs/full").text, client.get("/runs/bare").text
    for what in ("project", "client", "openapi"):
        assert f"/runs/full/download/{what}" in full
        assert f"/runs/bare/download/{what}" not in bare
