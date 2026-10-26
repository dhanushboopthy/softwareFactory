"""Safe run paths, run summaries for a list of runs, and project archives."""

import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from factory.export import client_archive, project_archive
from factory.report.models import Iteration, RunRecord, StationResult, Usage, now
from factory.report.runs import (
    STALE_AFTER_S,
    RunSummary,
    list_summaries,
    read_summary,
    run_dir_for,
    summarize,
    valid_run_id,
)
from factory.report.writer import write_report


def make_record(run_id: str = "r1", status: str = "passed", started: datetime | None = None,
                **extra: object) -> RunRecord:
    return RunRecord(run_id=run_id, spec_name="tasks", spec_path="/x", spec_sha256="ab" * 32,
                     provider="scripted", model="scripted", status=status,  # type: ignore[arg-type]
                     started_at=started or now(), **extra)  # type: ignore[arg-type]


def save(runs: Path, record: RunRecord) -> Path:
    run_dir = runs / record.run_id
    write_report(record, run_dir)
    return run_dir


# --- run ids and paths --------------------------------------------------------------------------


@pytest.mark.parametrize("run_id", ["tasks", "20261026-190412-tasks", "v1", "a.b_c-d", "0"])
def test_valid_run_ids(run_id: str) -> None:
    assert valid_run_id(run_id)


@pytest.mark.parametrize("run_id", ["", ".", "..", "../x", "a/b", "a\\b", ".hidden", "-x", "a b",
                                    "x" * 129, "ä", "a\x00b", "a\n"])
def test_invalid_run_ids(run_id: str) -> None:
    assert not valid_run_id(run_id)


def test_run_dir_for_stays_inside_the_runs_directory(tmp_path: Path) -> None:
    (tmp_path / "runs" / "r1").mkdir(parents=True)
    assert run_dir_for(tmp_path / "runs", "r1") == (tmp_path / "runs" / "r1").resolve()
    for bad in ("../runs", "..", "r1/..", "/etc"):
        with pytest.raises(ValueError):
            run_dir_for(tmp_path / "runs", bad)


def test_a_symlinked_run_directory_cannot_escape(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (runs / "sneaky").symlink_to(outside)
    with pytest.raises(ValueError, match="outside"):
        run_dir_for(runs, "sneaky")


# --- summaries ----------------------------------------------------------------------------------


def test_summary_of_a_finished_run(tmp_path: Path) -> None:
    record = make_record(profile="entities", approved=True, usage=Usage(cost_usd=0.42))
    record.stations += [StationResult(name="lint", status="passed", ended_at=now()),
                        StationResult(name="coder", status="failed", ended_at=now())]
    record.iterations += [Iteration(agent="coder", index=1), Iteration(agent="coder", index=2)]
    summary = read_summary(save(tmp_path, record))
    assert summary is not None and summary.readable
    assert (summary.run_id, summary.spec_name, summary.profile, summary.status) == (
        "r1", "tasks", "entities", "passed")
    assert (summary.stations, summary.stations_failed, summary.iterations) == (2, 1, 2)
    assert summary.cost_usd == 0.42 and summary.approved is True
    assert summary.current_station == "coder" and not summary.running


def test_the_current_station_is_the_unfinished_one_while_running(tmp_path: Path) -> None:
    record = make_record(status="running")
    record.stations += [StationResult(name="lint", status="passed", ended_at=now()),
                        StationResult(name="test_writer", status="passed")]  # still open
    summary = read_summary(save(tmp_path, record))
    assert summary is not None and summary.running
    assert summary.current_station == "test_writer"


def test_a_run_without_stations_has_no_current_station(tmp_path: Path) -> None:
    summary = read_summary(save(tmp_path, make_record(status="running")))
    assert summary is not None and summary.current_station is None


def test_stale_runs_are_detected() -> None:
    long_ago = datetime.now(UTC) - timedelta(seconds=STALE_AFTER_S + 60)
    running = summarize("r", make_record(status="running"), long_ago)
    assert running.stale()
    assert not summarize("r", make_record(status="running"), datetime.now(UTC)).stale()
    assert not summarize("r", make_record(status="failed"), long_ago).stale()  # not "running"
    assert not RunSummary(run_id="x", updated_at=long_ago, readable=False).stale()


def test_a_directory_that_is_not_a_run_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "scratch").mkdir()
    assert read_summary(tmp_path / "scratch") is None


def test_a_damaged_run_json_gives_a_placeholder_not_an_exception(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "run.json").write_text('{"run_id": "broken", "status": ')
    summary = read_summary(broken)
    assert summary is not None and not summary.readable and summary.run_id == "broken"
    assert summary.error and not summary.running
    wrong_shape = tmp_path / "wrong"
    wrong_shape.mkdir()
    (wrong_shape / "run.json").write_text('{"hello": 1}')
    placeholder = read_summary(wrong_shape)
    assert placeholder is not None and not placeholder.readable


def test_list_summaries_is_newest_first_and_skips_the_noise(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    base = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
    save(runs, make_record("old", started=base))
    save(runs, make_record("newest", started=base + timedelta(hours=2), status="running"))
    save(runs, make_record("middle", started=base + timedelta(hours=1)))
    (runs / "scratch").mkdir()
    (runs / ".hidden").mkdir()
    (runs / "a file").write_text("not a run")
    (runs / "link").symlink_to(runs / "old")
    broken = runs / "broken"
    broken.mkdir()
    (broken / "run.json").write_text("nope")
    ids = [s.run_id for s in list_summaries(runs)]
    assert ids == ["newest", "middle", "old", "broken"]  # the unreadable one is last
    assert "scratch" not in ids and ".hidden" not in ids
    assert "link" not in ids and "a file" not in ids


def test_list_summaries_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert list_summaries(tmp_path / "nowhere") == []


# --- archives -----------------------------------------------------------------------------------


def make_project(root: Path) -> Path:
    files = {
        "pyproject.toml": "x", "openapi.yaml": "y", "app/main.py": "z", "app/rules/project.py": "r",
        "migrations/versions/0001_initial.py": "m", "tests/test_project.py": "t",
        "client/__init__.py": "c", "client/api/default.py": "a", "Dockerfile": "d",
        ".venv/lib/x.py": "secret", ".env": "JWT_SECRET=abc", "app/__pycache__/m.pyc": "bytecode",
        ".hypothesis/x": "h", ".schemathesis/y": "s", ".factory/manifest.json": "{}",
        "app.db": "sqlite", "app/data.sqlite3": "sqlite", ".ruff_cache/z": "cache",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def names(archive: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(archive)).namelist()


def test_the_project_archive_has_the_source_and_none_of_the_noise(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    assert names(project_archive(project, "tasks")) == sorted([
        "tasks/Dockerfile", "tasks/app/main.py", "tasks/app/rules/project.py",
        "tasks/client/__init__.py", "tasks/client/api/default.py", "tasks/openapi.yaml",
        "tasks/migrations/versions/0001_initial.py", "tasks/pyproject.toml",
        "tasks/tests/test_project.py"])


def test_secrets_and_virtualenvs_never_reach_the_archive(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    archive = project_archive(project, "tasks")
    joined = " ".join(names(archive))
    for forbidden in (".env", ".venv", "__pycache__", ".db", ".sqlite3", ".factory"):
        assert forbidden not in joined, forbidden
    blob = b"".join(zipfile.ZipFile(io.BytesIO(archive)).read(n)
                    for n in names(archive))
    assert b"JWT_SECRET=abc" not in blob and b"secret" not in blob


def test_symlinks_are_not_followed_into_the_archive(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    outside = tmp_path / "private.txt"
    outside.write_text("do not leak")
    (project / "app" / "link.txt").symlink_to(outside)
    (project / "linked_dir").symlink_to(tmp_path)
    archive = project_archive(project, "tasks")
    assert not any("link" in n for n in names(archive))
    assert b"do not leak" not in b"".join(
        zipfile.ZipFile(io.BytesIO(archive)).read(n) for n in names(archive))


def test_the_archive_is_reproducible(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    first = project_archive(project, "tasks")
    os.utime(project / "app" / "main.py", (1, 1))
    assert project_archive(project, "tasks") == first


def test_the_client_archive_contains_only_the_sdk(tmp_path: Path) -> None:
    project = make_project(tmp_path / "project")
    assert names(client_archive(project, "tasks-client")) == [
        "tasks-client/__init__.py", "tasks-client/api/default.py"]


def test_missing_things_are_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        project_archive(tmp_path / "nowhere", "x")
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(FileNotFoundError, match="no generated client"):
        client_archive(bare, "x")

