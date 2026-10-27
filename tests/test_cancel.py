"""Cancelling a build (Ctrl-C / SIGINT): the run says so, and nothing is left running."""

import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from factory import pipeline
from factory.cli import app
from factory.config import FactoryConfig
from factory.harness import checks, sandbox
from factory.pipeline import BuildOptions, build
from factory.report.models import RunRecord
from factory.report.writer import load_record
from factory.stations import lint as lint_station

from .test_pipeline import World, entities_file, run_entities

runner = CliRunner()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch)


def interrupt(*args: Any, **kwargs: Any) -> Any:
    raise KeyboardInterrupt


def test_a_cancelled_entities_build_is_recorded_as_cancelled(
        world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pipeline, "scaffold_entities", interrupt)
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="cancel1")
    assert run.status == "cancelled" and run.status_reason == "cancelled by the user"
    assert [(s.name, s.status) for s in run.stations] == [("lint", "passed"),
                                                          ("scaffold", "error")]
    assert run.stations[-1].detail == "interrupted (cancelled)"
    assert run.stations[-1].ended_at is not None and run.ended_at is not None
    on_disk = load_record(tmp_path / "runs" / "cancel1")
    assert on_disk.status == "cancelled"                     # the report was written too
    assert (tmp_path / "runs" / "cancel1" / "report.md").exists()


def test_cancelling_during_the_checks_still_stops_postgres(
        world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(checks, "run_all", interrupt)
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="cancel2")
    assert run.status == "cancelled"
    assert world.events[-1] == "postgres-stop"               # the finally blocks ran
    assert not any(e.startswith("deploy:") for e in world.events)  # and nothing came after
    assert run.stations[-1].name == "baseline_checks" and run.stations[-1].status == "error"


def test_a_cancelled_openapi_build_is_recorded_too(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(lint_station, "lint_spec", interrupt)
    spec = tmp_path / "s.yaml"
    spec.write_text("openapi: 3.1.0\n")
    run = build(BuildOptions(spec, use_llm=False, run_id="cancel3"),
                FactoryConfig(runs_dir=tmp_path / "runs"), None)
    assert run.status == "cancelled" and run.profile == "openapi"


def test_other_failures_are_still_errors_not_cancellations(
        world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "scaffold_entities", explode)
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="boom")
    assert run.status == "error" and "boom" in run.status_reason


@pytest.mark.parametrize(("status", "code"), [("passed", 0), ("failed", 1), ("error", 1),
                                              ("budget_exhausted", 1), ("cancelled", 130)])
def test_the_build_command_exits_with_the_conventional_code(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, code: int) -> None:
    def fake_build(opts: BuildOptions, cfg: FactoryConfig, provider: object,
                   log: object) -> RunRecord:
        return RunRecord(run_id="x", spec_name="s", spec_path="p", spec_sha256="h",
                         provider="none", model="none", status=status)  # type: ignore[arg-type]

    monkeypatch.setattr(pipeline, "build", fake_build)
    spec = tmp_path / "s.yaml"
    spec.write_text("openapi: 3.1.0\n")
    result = runner.invoke(app, ["build", str(spec), "--no-llm"],
                           env={"FACTORY_RUNS_DIR": str(tmp_path / "runs")})
    assert result.exit_code == code


def test_interrupting_a_command_kills_the_whole_child_process_group(tmp_path: Path) -> None:
    """The child has its own session; without the cleanup it would outlive the factory."""
    pidfile = tmp_path / "pid"
    child = f"echo $$ > {pidfile}; sleep 60 & wait"
    timer = threading.Timer(0.8, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            sandbox.run(["sh", "-c", child], cwd=tmp_path, timeout_s=30)
    finally:
        timer.cancel()
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)  # the group still exists
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail("the child's process group survived the interruption")
