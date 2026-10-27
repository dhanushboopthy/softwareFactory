"""Starting, watching and cancelling builds from the UI."""

import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from factory.pipeline import NOT_APPROVED
from factory.report.models import RunRecord, StationResult
from factory.report.writer import write_report
from factory.ui.app import create_app
from factory.ui.environment import Check
from factory.ui.jobs import (
    GRACE_S,
    BuildBusy,
    BuildRunner,
    build_argv,
    build_env,
    unique_run_id,
    validate_for_build,
)
from factory.ui.security import UiSettings

from .conftest import FIXTURES, ROOT
from .test_ui_security import BASE, StubEnvironment, settings_for

EXAMPLE = (ROOT / "examples" / "tasks" / "entities.yaml").read_text()
SCRIPTED = {"LLM_PROVIDER": "scripted", "LLM_SCRIPT": str(FIXTURES / "scripted" / "ping.yaml")}


class FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code


@dataclass
class Spawned:
    argv: list[str]
    kwargs: dict[str, Any]
    proc: FakeProc


class FakeSpawn:
    def __init__(self) -> None:
        self.calls: list[Spawned] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProc:
        proc = FakeProc(pid=4000 + len(self.calls))
        self.calls.append(Spawned(argv, kwargs, proc))
        kwargs["stdout"].write(b"[lint]\n  -> passed\n")  # the child's output goes to the log
        return proc


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append((pid, int(sig))))
    return sent


def approved_text(app: str = "tasks") -> str:
    return EXAMPLE.replace("app: tasks", f"app: {app}")


# --- pure helpers -------------------------------------------------------------------------------


def test_build_argv() -> None:
    argv = build_argv(Path("/d/e.yaml"), "run-1", no_llm=False)
    assert argv == [sys.executable, "-m", "factory", "build", "/d/e.yaml", "--run-id", "run-1"]
    assert build_argv(Path("/d/e.yaml"), "run-1", no_llm=True)[-1] == "--no-llm"


def test_the_build_environment(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, env={"ANTHROPIC_API_KEY": "k", "LLM_PROVIDER": "anthropic",
                                           "FACTORY_UI_TOKEN": "t" * 20, "PATH": "/bin"})
    env = build_env(settings)
    assert env["ANTHROPIC_API_KEY"] == "k" and env["LLM_PROVIDER"] == "anthropic"  # it needs them
    assert env["FACTORY_RUNS_DIR"] == str((tmp_path / "runs").resolve())
    assert "FACTORY_UI_TOKEN" not in env                                # it does not


def test_validate_for_build() -> None:
    doc, problems = validate_for_build(EXAMPLE)
    assert doc is not None and doc.app == "tasks" and problems == []
    unapproved, problems = validate_for_build(EXAMPLE.replace("approved: true", "approved: false"))
    assert unapproved is not None and problems == [NOT_APPROVED]
    assert validate_for_build(EXAMPLE.replace("approved: true\n", ""))[1] == [NOT_APPROVED]
    invalid, problems = validate_for_build("approved: true\napp: x\nentities:\n  A:\n"
                                           "    fields: {n: {type: str}}\n")
    assert invalid is None and any("must declare owner" in p for p in problems)
    assert validate_for_build("app: [broken")[0] is None
    assert validate_for_build("just text")[1]


def test_run_ids_are_unique(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    first = unique_run_id(settings, Path("x.yaml"), "tasks")
    assert first.endswith("-tasks")
    (settings.cfg.runs_dir / first).mkdir(parents=True)
    second = unique_run_id(settings, Path("x.yaml"), "tasks")
    assert second == f"{first}-2"
    (settings.cfg.drafts_dir / second).mkdir(parents=True)
    assert unique_run_id(settings, Path("x.yaml"), "tasks") == f"{first}-3"


# --- the runner with a fake process -------------------------------------------------------------


def runner_for(tmp_path: Path, **kwargs: Any) -> tuple[BuildRunner, FakeSpawn, UiSettings]:
    settings = settings_for(tmp_path, env=SCRIPTED)
    spawn = FakeSpawn()
    return BuildRunner(settings, spawn=spawn, **kwargs), spawn, settings  # type: ignore[arg-type]


def test_start_runs_the_build_in_its_own_session_with_its_log(tmp_path: Path) -> None:
    runner, spawn, settings = runner_for(tmp_path)
    job = runner.start(tmp_path / "e.yaml", "run-1")
    call = spawn.calls[0]
    assert call.argv[-3:] == [str(tmp_path / "e.yaml"), "--run-id", "run-1"]
    assert call.kwargs["start_new_session"] is True       # so Cancel can signal its whole group
    assert call.kwargs["env"]["LLM_PROVIDER"] == "scripted"
    assert "shell" not in call.kwargs                      # an argv list, never a shell string
    assert job.log_path == settings.cfg.runs_dir / ".ui" / "run-1.log"
    assert runner.log_tail("run-1") == "[lint]\n  -> passed\n"
    assert job.running and runner.current() is job and runner.job_for("run-1") is job


def test_only_one_build_runs_at_a_time(tmp_path: Path) -> None:
    runner, spawn, _ = runner_for(tmp_path)
    first = runner.start(tmp_path / "e.yaml", "run-1")
    with pytest.raises(BuildBusy) as busy:
        runner.start(tmp_path / "e.yaml", "run-2")
    assert busy.value.run_id == "run-1" and len(spawn.calls) == 1
    spawn.calls[0].proc.code = 0                           # it finished
    assert runner.current() is None
    second = runner.start(tmp_path / "e.yaml", "run-2")
    assert runner.current() is second and not first.running


def test_a_bad_run_id_is_refused_before_anything_starts(tmp_path: Path) -> None:
    runner, spawn, _ = runner_for(tmp_path)
    for bad in ("../x", "a b", "", ".hidden"):
        with pytest.raises(ValueError, match="not a valid run id"):
            runner.start(tmp_path / "e.yaml", bad)
    assert spawn.calls == []


def test_cancel_sends_ctrl_c_to_the_whole_process_group(tmp_path: Path,
                                                        signals: list[tuple[int, int]]) -> None:
    runner, spawn, _ = runner_for(tmp_path)
    job = runner.start(tmp_path / "e.yaml", "run-1")
    assert runner.cancel("run-1") is True
    assert signals == [(4000, int(signal.SIGINT))]
    assert job.cancelling and job.cancel_requested_at is not None
    assert not job.force_offered()                         # not yet: it is cleaning up
    later = datetime.now(UTC) + timedelta(seconds=GRACE_S + 1)
    assert job.force_offered(later)
    assert runner.cancel("run-1", force=True) is True
    assert signals[-1] == (4000, int(signal.SIGKILL))
    spawn.calls[0].proc.code = -9
    assert runner.cancel("run-1") is False                 # nothing left to cancel
    assert runner.cancel("never-started") is False


def test_a_cancel_after_the_process_exited_is_harmless(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, _ = runner_for(tmp_path)
    runner.start(tmp_path / "e.yaml", "run-1")

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", gone)
    assert runner.cancel("run-1") is True


def test_shutdown_cancels_then_kills(tmp_path: Path, signals: list[tuple[int, int]]) -> None:
    runner, _, _ = runner_for(tmp_path)
    runner.shutdown()                                      # nothing running: nothing to do
    assert signals == []
    runner.start(tmp_path / "e.yaml", "run-1")
    runner.shutdown(wait_s=0.3)                            # it ignores the Ctrl-C
    assert [s for _, s in signals] == [int(signal.SIGINT), int(signal.SIGKILL)]


def test_log_tail_is_bounded_and_safe(tmp_path: Path) -> None:
    runner, _, settings = runner_for(tmp_path)
    runner.start(tmp_path / "e.yaml", "run-1")
    runner.job_for("run-1").log_path.write_bytes(b"x" * 10_000 + b"END")  # type: ignore[union-attr]
    tail = runner.log_tail("run-1", limit=100)
    assert tail is not None and len(tail) == 100 and tail.endswith("END")
    assert runner.log_tail("unknown") is None and runner.log_tail("../x") is None
    secret = tmp_path / "secret.log"
    secret.write_text("private")
    (settings.cfg.runs_dir / ".ui" / "link.log").symlink_to(secret)
    assert runner.log_tail("link") is None                 # a symlink is never followed


# --- the pages ----------------------------------------------------------------------------------


@pytest.fixture
def ui(tmp_path: Path, signals: list[tuple[int, int]]) -> tuple[TestClient, BuildRunner,
                                                                FakeSpawn, UiSettings]:
    runner, spawn, settings = runner_for(tmp_path)
    client = TestClient(create_app(settings, StubEnvironment(settings), runner), base_url=BASE)
    return client, runner, spawn, settings


def upload(client: TestClient, settings: UiSettings, text: str = EXAMPLE, name: str = "e.yaml",
           no_llm: bool = False, **fields: str) -> Any:
    data = {"csrf": settings.csrf_token, **fields}
    if no_llm:
        data["no_llm"] = "on"
    return client.post("/builds", data=data, files={"entities": (name, text, "text/yaml")},
                       follow_redirects=False)


def test_the_form_asks_for_an_approved_file(ui: Any) -> None:
    client, _, _, settings = ui
    html = client.get("/builds/new").text
    assert 'enctype="multipart/form-data"' in html and 'type="file"' in html
    assert f'name="csrf" value="{settings.csrf_token}"' in html
    assert "approved: true" in html and 'name="no_llm"' in html


def test_environment_problems_are_shown_on_the_form(tmp_path: Path) -> None:
    runner, _, settings = runner_for(tmp_path)
    checks = [Check("Postgres (Docker)", False, "daemon down", "Start Docker."),
              Check("LLM provider", False, "not set", "Set LLM_PROVIDER.")]
    client = TestClient(create_app(settings, StubEnvironment(settings, checks), runner),
                        base_url=BASE)
    html = client.get("/builds/new").text
    assert "daemon down" in html and "Start Docker." in html
    assert "Set LLM_PROVIDER." not in html                 # the provider has its own message


def test_uploading_an_approved_file_starts_a_build_and_redirects(ui: Any) -> None:
    client, runner, spawn, settings = ui
    response = upload(client, settings)
    run_id = response.headers["location"].removeprefix("/runs/")
    assert response.status_code == 303 and run_id.endswith("-tasks")
    saved = settings.cfg.drafts_dir / run_id / "entities.yaml"
    assert saved.read_text() == EXAMPLE                    # exactly what was uploaded
    assert spawn.calls[0].argv[-3:] == [str(saved), "--run-id", run_id]
    assert runner.current() is not None


def test_the_uploaded_file_name_is_never_used_as_a_path(ui: Any) -> None:
    client, _, spawn, settings = ui
    response = upload(client, settings, name="../../../etc/evil.yaml")
    assert response.status_code == 303
    saved = Path(spawn.calls[0].argv[-3])
    assert saved.parent.parent == settings.cfg.drafts_dir and saved.name == "entities.yaml"
    assert "etc" not in str(saved)


def test_generate_and_check_only_passes_no_llm(ui: Any) -> None:
    client, _, spawn, settings = ui
    upload(client, settings, no_llm=True)
    assert spawn.calls[0].argv[-1] == "--no-llm"


@pytest.mark.parametrize(("text", "expected"), [
    (EXAMPLE.replace("approved: true", "approved: false"), "not approved"),
    (EXAMPLE.replace("    owner: user\n", "", 1), "must declare owner"),
    ("app: [broken", "not valid YAML"),
])
def test_files_that_cannot_be_built_are_explained_and_nothing_starts(
        ui: Any, text: str, expected: str) -> None:
    client, _, spawn, settings = ui
    response = upload(client, settings, text)
    assert response.status_code == 422 and expected in response.text
    assert "This file cannot be built yet" in response.text
    assert spawn.calls == [] and not settings.cfg.drafts_dir.exists()


def test_without_a_provider_only_check_only_builds_can_start(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, env={})
    spawn = FakeSpawn()
    client = TestClient(create_app(settings, StubEnvironment(settings),
                                   BuildRunner(settings, spawn=spawn)),  # type: ignore[arg-type]
                        base_url=BASE)
    refused = upload(client, settings)
    assert refused.status_code == 422 and "No LLM provider is configured" in refused.text
    assert spawn.calls == []
    assert upload(client, settings, no_llm=True).status_code == 303


def test_a_second_build_is_refused_while_one_runs(ui: Any) -> None:
    client, _, spawn, settings = ui
    first = upload(client, settings)
    second = upload(client, settings)
    assert second.status_code == 409 and "still running" in second.text
    assert first.headers["location"].removeprefix("/runs/") in second.text
    assert len(spawn.calls) == 1
    assert "already running" in client.get("/builds/new").text


@pytest.mark.parametrize(("kwargs", "status"), [
    ({"files": None}, 400),                                # no file at all
    ({"raw": b"\xff\xfe\x00bad"}, 400),                    # not text
    ({"raw": b"a: 1\n" + b"#" * 300_000}, 413),            # far above the size limit
])
def test_bad_uploads(ui: Any, kwargs: dict[str, Any], status: int) -> None:
    client, _, spawn, settings = ui
    if "files" in kwargs:
        response = client.post("/builds", data={"csrf": settings.csrf_token})
    else:
        response = client.post("/builds", data={"csrf": settings.csrf_token},
                               files={"entities": ("e.yaml", kwargs["raw"])})
    assert response.status_code == status and spawn.calls == []


def test_starting_a_build_needs_the_csrf_token_and_a_same_origin_request(ui: Any) -> None:
    client, _, spawn, settings = ui
    files = {"entities": ("e.yaml", EXAMPLE)}
    assert client.post("/builds", files=files).status_code == 403
    assert client.post("/builds", data={"csrf": "wrong"}, files=files).status_code == 403
    assert client.post("/builds", data={"csrf": settings.csrf_token}, files=files,
                       headers={"Origin": "http://evil.example"}).status_code == 403
    assert spawn.calls == []


def test_right_after_starting_the_run_page_shows_the_console(ui: Any) -> None:
    client, runner, spawn, settings = ui
    run_id = upload(client, settings).headers["location"].removeprefix("/runs/")
    html = client.get(f"/runs/{run_id}").text
    assert "starting" in html and "[lint]" in html and 'http-equiv="refresh"' in html
    spawn.calls[0].proc.code = 2                           # it died before writing a report
    died = client.get(f"/runs/{run_id}").text
    assert "did not start" in died and "exit code 2" in died and "[lint]" in died
    assert 'http-equiv="refresh"' not in died
    assert client.get("/runs/never-started").status_code == 404


def write_running_record(settings: UiSettings, run_id: str) -> None:
    record = RunRecord(run_id=run_id, spec_name="tasks", spec_path="/x", spec_sha256="h",
                       provider="scripted", model="scripted", status="running",
                       profile="entities")
    record.stations.append(StationResult(name="lint", status="passed"))
    write_report(record, settings.cfg.runs_dir / run_id)


def test_the_run_page_offers_cancel_and_shows_the_console(ui: Any,
                                                          signals: list[tuple[int, int]]) -> None:
    client, runner, _, settings = ui
    run_id = upload(client, settings).headers["location"].removeprefix("/runs/")
    write_running_record(settings, run_id)
    html = client.get(f"/runs/{run_id}").text
    assert "Cancel build" in html and f'action="/runs/{run_id}/cancel"' in html
    assert f'name="csrf" value="{settings.csrf_token}"' in html
    assert "Console output" in html and "[lint]" in html
    response = client.post(f"/runs/{run_id}/cancel", data={"csrf": settings.csrf_token},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/runs/{run_id}"
    assert signals == [(4000, int(signal.SIGINT))]
    after = client.get(f"/runs/{run_id}").text
    assert "cancelling" in after and "Cancel build" not in after and "Force stop" not in after
    job = runner.job_for(run_id)
    assert job is not None
    job.cancel_requested_at = datetime.now(UTC) - timedelta(seconds=GRACE_S + 5)
    assert "Force stop" in client.get(f"/runs/{run_id}").text
    client.post(f"/runs/{run_id}/cancel", data={"csrf": settings.csrf_token, "force": "1"})
    assert signals[-1] == (4000, int(signal.SIGKILL))


def test_cancel_needs_the_token_and_a_running_build(ui: Any) -> None:
    client, _, spawn, settings = ui
    run_id = upload(client, settings).headers["location"].removeprefix("/runs/")
    assert client.post(f"/runs/{run_id}/cancel").status_code == 403
    assert client.post(f"/runs/{run_id}/cancel", data={"csrf": settings.csrf_token},
                       headers={"Origin": "http://evil.example"}).status_code == 403
    stranger = client.post("/runs/never-started/cancel", data={"csrf": settings.csrf_token})
    assert stranger.status_code == 409 and "not running" in stranger.text
    spawn.calls[0].proc.code = 0
    finished = client.post(f"/runs/{run_id}/cancel", data={"csrf": settings.csrf_token})
    assert finished.status_code == 409


def test_runs_not_started_here_have_no_console_or_cancel_button(ui: Any) -> None:
    client, _, _, settings = ui
    write_running_record(settings, "from-the-command-line")
    html = client.get("/runs/from-the-command-line").text
    assert "Console output" not in html and "Cancel build" not in html


def test_the_dashboard_shows_the_running_build_and_the_new_build_button(ui: Any) -> None:
    client, _, spawn, settings = ui
    assert 'href="/builds/new"' in client.get("/").text
    assert "Build running" not in client.get("/").text
    run_id = upload(client, settings).headers["location"].removeprefix("/runs/")
    html = client.get("/").text
    assert "Build running" in html and f'href="/runs/{run_id}"' in html
    assert 'http-equiv="refresh"' in html
    spawn.calls[0].proc.code = 0
    assert "Build running" not in client.get("/").text


def test_stopping_the_server_stops_a_running_build(
        tmp_path: Path, signals: list[tuple[int, int]]) -> None:
    runner, _, settings = runner_for(tmp_path, shutdown_wait_s=0.3)
    with TestClient(create_app(settings, StubEnvironment(settings), runner), base_url=BASE) as c:
        upload(c, settings)
        assert signals == []
    # leaving the block shut the server down: it asked the build to stop, then killed it
    assert [s for _, s in signals] == [int(signal.SIGINT), int(signal.SIGKILL)]


# --- a real build through the UI (opt in: FACTORY_E2E=1) ------------------------------------------


def real_build_ui(tmp_path: Path) -> tuple[TestClient, UiSettings]:
    """The UI wired to a real BuildRunner whose builds use scripted agents and local Postgres."""
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "UV", "UV_CACHE_DIR", "SSL_CERT_FILE", "TMPDIR", "LANG")
           or k.upper() in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    env.update({
        "LLM_PROVIDER": "scripted",
        "LLM_SCRIPT": str(FIXTURES / "scripted" / "tasks_v1" / "script.yaml"),
        "FACTORY_POSTGRES_BACKEND": os.environ.get("FACTORY_POSTGRES_BACKEND", "docker"),
        "FACTORY_DEPLOY_CHECK": "1" if os.environ.get(
            "FACTORY_POSTGRES_BACKEND", "docker") == "docker" else "0",
    })
    settings = settings_for(tmp_path, env=env)
    client = TestClient(create_app(settings, StubEnvironment(settings), BuildRunner(settings)),
                        base_url=BASE)
    return client, settings


def run_status(settings: UiSettings, run_id: str) -> str | None:
    path = settings.cfg.runs_dir / run_id / "run.json"
    try:
        import json

        return str(json.loads(path.read_text())["status"])
    except (OSError, ValueError, KeyError):
        return None


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("FACTORY_E2E") != "1", reason="set FACTORY_E2E=1")
def test_a_real_build_started_and_watched_through_the_ui(tmp_path: Path) -> None:
    client, settings = real_build_ui(tmp_path)
    response = upload(client, settings, EXAMPLE, "entities.yaml")
    assert response.status_code == 303
    run_id = response.headers["location"].removeprefix("/runs/")
    first = client.get(f"/runs/{run_id}")                  # right away: starting, or already going
    assert first.status_code == 200
    assert wait_for(lambda: run_status(settings, run_id) not in (None, "running"), seconds=1200)
    job = client.app.state.runner.job_for(run_id)  # type: ignore[attr-defined]
    assert wait_for(lambda: not job.running, seconds=120)  # the process still cleans up after it
    page = client.get(f"/runs/{run_id}").text
    assert run_status(settings, run_id) == "passed", page[-3000:]
    assert '<span class="chip ok">passed</span>' in page
    assert "cheat_never_reject" in page and "cheat_always_reject" in page
    assert "Console output" in page and "status: passed" in page
    assert "http-equiv" not in page                        # finished: it stops reloading
    assert "Cancel build" not in page
    archive = client.get(f"/runs/{run_id}/download/project")
    assert archive.status_code == 200 and archive.content[:2] == b"PK"
    sdk = client.get(f"/runs/{run_id}/download/client")
    assert sdk.status_code == 200
    rules = client.get(f"/runs/{run_id}/files/project/app/rules/project.py").text
    assert "RuleViolation" in rules
    assert "Build running" not in client.get("/").text     # and the dashboard knows it is over


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("FACTORY_E2E") != "1", reason="set FACTORY_E2E=1")
def test_cancelling_a_real_build_mid_flight_leaves_nothing_behind(tmp_path: Path) -> None:
    client, settings = real_build_ui(tmp_path)
    run_id = upload(client, settings).headers["location"].removeprefix("/runs/")
    assert wait_for(lambda: run_status(settings, run_id) == "running", seconds=60)
    time.sleep(3)                                          # let it get into the slow stations
    client.post(f"/runs/{run_id}/cancel", data={"csrf": settings.csrf_token})
    assert wait_for(lambda: run_status(settings, run_id) == "cancelled", seconds=120), (
        client.get(f"/runs/{run_id}").text[-2500:])
    page = client.get(f"/runs/{run_id}").text
    assert "cancelled" in page and "cancelled by the user" in page
    assert "Cancel build" not in page
    runner: BuildRunner = client.app.state.runner  # type: ignore[attr-defined]
    job = runner.job_for(run_id)
    assert job is not None and wait_for(lambda: not job.running) and job.returncode == 130
    run_dir = str(settings.cfg.runs_dir.resolve())
    import subprocess

    leftovers = subprocess.run(  # noqa: S603 - a fixed argv; looks for processes using this run
        ["pgrep", "-f", run_dir],  # noqa: S607
        capture_output=True, text=True, check=False).stdout.split()
    assert [p for p in leftovers if int(p) != os.getpid()] == []


# --- real processes -----------------------------------------------------------------------------

HANDLES_SIGINT = """
import signal, sys, time, pathlib
def stop(signum, frame):
    time.sleep(0.2)                                  # "cleaning up"
    pathlib.Path(sys.argv[1]).write_text("cleaned up")
    sys.exit(130)
signal.signal(signal.SIGINT, stop)
pathlib.Path(sys.argv[1] + ".started").write_text("x")
time.sleep(60)
"""
IGNORES_SIGINT = """
import signal, sys, time, pathlib
signal.signal(signal.SIGINT, signal.SIG_IGN)
pathlib.Path(sys.argv[1] + ".started").write_text("x")
time.sleep(60)
"""


def real_runner(tmp_path: Path, script: str) -> tuple[BuildRunner, Path]:
    marker = tmp_path / "marker"
    settings = settings_for(tmp_path, env=SCRIPTED)
    runner = BuildRunner(settings, argv=lambda s, r, n: [sys.executable, "-c", script,
                                                          str(marker)])
    return runner, marker


def wait_for(predicate: Any, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_cancelling_a_real_process_lets_it_clean_up(tmp_path: Path) -> None:
    runner, marker = real_runner(tmp_path, HANDLES_SIGINT)
    job = runner.start(tmp_path / "e.yaml", "real-1")
    assert wait_for(lambda: Path(f"{marker}.started").exists())
    assert runner.cancel("real-1")
    assert wait_for(lambda: not job.running)
    assert marker.read_text() == "cleaned up" and job.returncode == 130
    assert runner.current() is None


def test_force_stop_kills_a_process_that_ignores_ctrl_c(tmp_path: Path) -> None:
    runner, marker = real_runner(tmp_path, IGNORES_SIGINT)
    job = runner.start(tmp_path / "e.yaml", "real-2")
    assert wait_for(lambda: Path(f"{marker}.started").exists())
    runner.cancel("real-2")
    time.sleep(0.5)
    assert job.running                                     # it ignored the polite request
    runner.cancel("real-2", force=True)
    assert wait_for(lambda: not job.running) and job.returncode == -9


def test_shutdown_stops_a_stubborn_real_process(tmp_path: Path) -> None:
    runner, marker = real_runner(tmp_path, IGNORES_SIGINT)
    job = runner.start(tmp_path / "e.yaml", "real-3")
    assert wait_for(lambda: Path(f"{marker}.started").exists())
    runner.shutdown(wait_s=0.5)
    assert wait_for(lambda: not job.running)
