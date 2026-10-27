from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml

from factory import pipeline
from factory.config import FactoryConfig
from factory.entities import load_entities_raw
from factory.harness import checks, sandbox
from factory.harness.postgres import PostgresServer, PostgresUnavailable
from factory.llm.scripted import ScriptedProvider
from factory.pipeline import (
    INCREMENTAL_REFUSAL,
    NOT_APPROVED,
    BuildOptions,
    IncrementalNotSupported,
    build,
    input_profile,
    latest_passed_run,
    new_run_id,
)
from factory.report.models import CheckResult
from factory.stations import contract, scaffold, sdk
from factory.stations.scaffold import ScaffoldResult

from .conftest import FIXTURES, ROOT

EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"


@pytest.mark.parametrize(
    ("raw", "profile"),
    [
        ({"openapi": "3.1.0", "paths": {}}, "openapi"),
        ({"approved": True, "app": "tasks", "entities": {}}, "entities"),
    ],
)
def test_input_profile(raw: dict[str, object], profile: str) -> None:
    assert input_profile(raw) == profile


def test_input_profile_rejects_unknown_documents() -> None:
    with pytest.raises(ValueError, match="neither"):
        input_profile({"swagger": "2.0"})


def test_unrecognised_input_runs_the_openapi_profile_and_fails_lint(tmp_path: Path) -> None:
    spec = tmp_path / "s.yaml"
    spec.write_text("swagger: '2.0'\n")
    run = build(BuildOptions(spec, use_llm=False, run_id="bad"),
                FactoryConfig(runs_dir=tmp_path / "runs"), None)
    assert run.status == "failed"
    assert run.config["profile"] == "openapi"
    assert run.status_reason == "spec lint failed"
    assert run.profile == "openapi" and run.approved is None and run.budgets == {}


# --- entities input: refusals --------------------------------------------------------------------


def entities_file(tmp_path: Path, **changes: Any) -> Path:
    raw = load_entities_raw(EXAMPLE)  # not safe_load: it reads the unquoted `on:` as True
    for key, value in changes.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    path = tmp_path / "anything.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def run_entities(tmp_path: Path, path: Path, **kwargs: Any) -> Any:
    return build(BuildOptions(path, use_llm=False, **kwargs),
                 FactoryConfig(runs_dir=tmp_path / "runs"), None)


def test_an_invalid_entities_file_lists_every_problem(tmp_path: Path) -> None:
    raw = load_entities_raw(EXAMPLE)
    del raw["entities"]["Project"]["owner"]
    raw["entities"]["Task"]["parent"] = "Nope"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(raw))
    run = run_entities(tmp_path, path, run_id="bad")
    assert run.status == "failed" and run.status_reason == "the entities file is invalid"
    assert [(s.name, s.status) for s in run.stations] == [("lint", "failed")]
    detail = run.stations[0].detail
    assert "must declare owner" in detail and "is not an entity" in detail
    assert run.profile == "entities" and run.approved is None and run.budgets == {}
    assert run.spec_name == "bad.yaml"  # no app name to use


def test_unparseable_yaml_is_an_error_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("approved: true\nentities: [unclosed\n")
    run = run_entities(tmp_path, path, run_id="broken")
    # Unparseable YAML cannot be classified, so it takes the OpenAPI path, as it always has.
    assert run.status == "error" and run.profile == "openapi"
    assert [(s.name, s.status) for s in run.stations] == [("lint", "error")]


@pytest.mark.parametrize("approved", [False, None])
def test_an_unapproved_file_is_refused_and_says_why(tmp_path: Path, approved: bool | None) -> None:
    run = run_entities(tmp_path, entities_file(tmp_path, approved=approved), run_id="u")
    assert run.status == "failed" and run.status_reason == NOT_APPROVED
    assert [(s.name, s.status) for s in run.stations] == [("lint", "failed")]
    assert "set `approved: true`" in run.stations[0].detail
    assert run.approved is False  # recorded: the hash plus the flag (R1)
    assert not (tmp_path / "runs" / "u" / "project").exists()  # nothing was generated


def test_runs_are_named_after_the_app_and_record_their_budgets(tmp_path: Path) -> None:
    run = run_entities(tmp_path, entities_file(tmp_path, approved=False))
    assert run.run_id.endswith("-tasks") and run.spec_name == "tasks"
    assert run.budgets == {"llm_items": 3.0, "max_iters": 10.0,
                           "max_lines_changed_per_iter": 450.0, "max_cost_usd_per_run": 3.25}
    assert (run.config["max_iters"], run.config["max_lines_changed_per_iter"]) == (10, 450)
    assert run.config["profile"] == "entities"


def test_new_run_id_prefers_the_app_name(tmp_path: Path) -> None:
    assert new_run_id(tmp_path / "x.yaml", "tasks").endswith("-tasks")
    assert new_run_id(tmp_path / "x.yaml").endswith("-x")


def test_incremental_builds_of_entities_are_refused_before_a_run_exists(tmp_path: Path) -> None:
    path = entities_file(tmp_path)
    with pytest.raises(IncrementalNotSupported, match="Open Question 2"):
        run_entities(tmp_path, path, previous_run="earlier", run_id="inc")
    assert not (tmp_path / "runs").exists()
    assert "Open Question 2" in INCREMENTAL_REFUSAL


def test_latest_passed_run_is_keyed_on_the_app_name(tmp_path: Path) -> None:
    cfg = FactoryConfig(runs_dir=tmp_path / "runs")
    run = run_entities(tmp_path, entities_file(tmp_path, approved=False), run_id="r1")
    assert latest_passed_run(cfg, "tasks") is None  # failed runs never count
    assert run.spec_name == "tasks"


# --- entities input: the station flow with the heavy parts faked ---------------------------------


def ok_result() -> sandbox.CommandResult:
    return sandbox.CommandResult((), 0, "", "", 0.1, False)


class World:
    """Fakes for everything slow, recording what the pipeline did and in which order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.run_all_env: Any = None
        self.alembic_ok = True
        self.deploy_ok = True
        self.postgres_error: str | None = None
        monkeypatch.setattr(pipeline, "scaffold_entities", self.scaffold_entities)
        monkeypatch.setattr(scaffold, "setup_env", lambda project, timeout: ok_result())
        monkeypatch.setattr(pipeline, "postgres_server", self.postgres_server)
        monkeypatch.setattr(sandbox, "run", self.run)
        monkeypatch.setattr(checks, "run_all", self.run_all)
        monkeypatch.setattr(contract, "run_contract", self.run_contract)
        monkeypatch.setattr(contract, "run_deploy_check", self.run_deploy_check)
        monkeypatch.setattr(sdk, "generate_sdk", lambda project, timeout: ok_result())

    def scaffold_entities(self, doc: Any, path: Path, project: Path, timeout: float,
                          cfg: FactoryConfig) -> ScaffoldResult:
        self.events.append("scaffold")
        project.mkdir(parents=True)
        return ScaffoldResult(files=["a", "b"], models=ok_result())

    @contextmanager
    def postgres_server(self, cfg: FactoryConfig, run_dir: Path,
                        run_id: str) -> Iterator[PostgresServer]:
        if self.postgres_error:
            raise PostgresUnavailable(self.postgres_error)
        self.events.append("postgres-start")
        sql_log = self.events

        def sql(statement: str, database: str) -> sandbox.CommandResult:
            sql_log.append(statement)
            return ok_result()

        try:
            yield PostgresServer(port=5544, password="pw", _sql=sql)  # noqa: S106
        finally:
            self.events.append("postgres-stop")

    def run(self, cmd: Sequence[str], cwd: Path, timeout_s: float,
            env: Mapping[str, str] | None = None) -> sandbox.CommandResult:
        self.events.append("alembic:" + " ".join(cmd[-3:]))
        assert env is not None and env["DATABASE_URL"].endswith("/template_db")
        if self.alembic_ok:
            return ok_result()
        return sandbox.CommandResult(tuple(cmd), 1, "", "relation already exists", 0.1, False)

    def run_all(self, project: Path, cfg: FactoryConfig, log_dir: Path,
                check_env: Any = None) -> list[CheckResult]:
        self.events.append("baseline")
        self.run_all_env = check_env
        return [CheckResult(name="pytest", passed=True, summary="ok")]

    def run_contract(self, project: Path, cfg: FactoryConfig, run_dir: Path,
                     check_env: Any = None) -> CheckResult:
        self.events.append("contract")
        assert check_env is self.run_all_env
        return CheckResult(name="contract", passed=True, summary="1054 cases")

    def run_deploy_check(self, project: Path, cfg: FactoryConfig, run_dir: Path, run_id: str,
                         doc: Any) -> CheckResult:
        self.events.append(f"deploy:{run_id}")
        if self.deploy_ok:
            return CheckResult(name="deploy", passed=True, summary="healthy")
        return CheckResult(name="deploy", passed=False, summary="docker compose up failed")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    return World(monkeypatch)


def test_entities_stations_run_in_order_and_postgres_is_always_stopped(
        world: World, tmp_path: Path) -> None:
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="flow")
    assert [(s.name, s.status) for s in run.stations] == [
        ("lint", "passed"), ("scaffold", "passed"), ("setup", "passed"), ("postgres", "passed"),
        ("test_writer", "skipped"), ("baseline_checks", "passed"), ("coder", "skipped"),
        ("contract", "passed"), ("sdk", "passed"), ("deploy_check", "passed")]
    assert run.status == "passed" and run.approved is True
    assert run.deploy is not None and run.deploy.passed
    # the harness database is gone before the deploy check starts its own compose stack
    assert world.events == [
        "scaffold", "postgres-start", 'CREATE DATABASE "template_db"',
        "alembic:alembic upgrade head", "baseline", "contract", "postgres-stop", "deploy:flow"]
    assert world.run_all_env is not None
    assert "5544" in run.stations[3].detail and "migrated" in run.stations[3].detail
    assert run.stations[1].detail == "2 files generated"
    assert "expanded to" in run.stations[0].detail


def test_the_deploy_check_can_be_switched_off(world: World, tmp_path: Path) -> None:
    run = build(BuildOptions(entities_file(tmp_path), use_llm=False, run_id="nodeploy"),
                FactoryConfig(runs_dir=tmp_path / "runs", deploy_check=False), None)
    stations = {s.name: s for s in run.stations}
    assert stations["deploy_check"].status == "skipped"
    assert "deploy_check is off" in stations["deploy_check"].detail
    assert run.deploy is None and run.status == "passed"
    assert not any(e.startswith("deploy:") for e in world.events)


def test_a_failing_deploy_check_fails_the_run(world: World, tmp_path: Path) -> None:
    world.deploy_ok = False
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="baddeploy")
    assert run.status == "failed" and "deploy_check" in run.status_reason
    assert run.deploy is not None and not run.deploy.passed
    assert run.stations[-1].detail == "docker compose up failed"


def test_the_template_database_is_migrated_with_the_real_alembic_revision(
        world: World, tmp_path: Path) -> None:
    run_entities(tmp_path, entities_file(tmp_path), run_id="mig")
    assert any(e.startswith("alembic:") and e.endswith("upgrade head") for e in world.events)
    assert world.events.index('CREATE DATABASE "template_db"') < next(
        i for i, e in enumerate(world.events) if e.startswith("alembic:"))


def test_postgres_unavailable_fails_fast_with_the_reason(world: World, tmp_path: Path) -> None:
    world.postgres_error = "Docker is not reachable: permission denied"
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="nodocker")
    assert run.status == "failed" and run.status_reason == "Postgres is not available"
    assert [s.name for s in run.stations] == ["lint", "scaffold", "setup", "postgres"]
    assert "Docker is not reachable" in run.stations[-1].detail
    assert "baseline" not in world.events


def test_a_failed_migration_stops_the_run_and_still_stops_postgres(
        world: World, tmp_path: Path) -> None:
    world.alembic_ok = False
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="badmig")
    assert run.status == "failed" and run.status_reason == "`alembic upgrade head` failed"
    assert "relation already exists" in run.stations[-1].detail
    assert world.events[-1] == "postgres-stop" and "baseline" not in world.events


def test_the_test_writer_is_skipped_when_the_llm_has_nothing_to_do(
        world: World, tmp_path: Path) -> None:
    path = tmp_path / "plain.yaml"
    path.write_text(yaml.safe_dump({
        "approved": True, "app": "plain",
        "entities": {"Note": {"owner": "user", "fields": {"body": {"type": "str"}}}}}))
    run = build(BuildOptions(path, run_id="plain"), FactoryConfig(runs_dir=tmp_path / "runs"),
                ScriptedProvider(FIXTURES / "scripted" / "ping.yaml"))
    stations = {s.name: s for s in run.stations}
    assert stations["test_writer"].status == "skipped"
    assert "nothing to test" in stations["test_writer"].detail
    assert stations["coder"].status == "passed"  # baseline already green: nothing to implement
    assert run.budgets["llm_items"] == 0.0 and run.config["max_iters"] == 4
    assert run.status == "passed"


def test_a_failing_contract_fails_the_run(
        world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(contract, "run_contract", lambda *a, **k: CheckResult(
        name="contract", passed=False, summary="3 unique failures"))
    run = run_entities(tmp_path, entities_file(tmp_path), run_id="badcontract")
    assert run.status == "failed" and "contract" in run.status_reason
    assert world.events[-2:] == ["postgres-stop", "deploy:badcontract"]  # still checked, last
