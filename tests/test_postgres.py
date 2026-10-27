"""harness/postgres.py: command lines, lifecycle and cleanup (fake sandbox), one opt-in real run."""

import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from factory.config import FactoryConfig
from factory.harness import postgres, sandbox
from factory.harness.postgres import (
    PostgresError,
    PostgresUnavailable,
    container_name,
    docker_env,
    docker_port_cmd,
    docker_psql_cmd,
    docker_rm_cmd,
    docker_run_cmd,
    local_initdb_cmd,
    local_postgres_cmd,
    local_psql_cmd,
    local_ready_cmd,
    parse_docker_port,
    postgres_server,
    safe_db_name,
)


def result(stdout: str = "", returncode: int = 0) -> sandbox.CommandResult:
    return sandbox.CommandResult(cmd=(), returncode=returncode, stdout=stdout, stderr="",
                                 duration_s=0.0, timed_out=False)


Handler = Callable[[list[str]], sandbox.CommandResult]


class FakeSandbox:
    """Records every command passed to sandbox.run and answers via ``handler``."""

    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler or (lambda cmd: result())
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, cmd: Sequence[str], cwd: Path, timeout_s: float,
            env: Mapping[str, str] | None = None) -> sandbox.CommandResult:
        self.calls.append((list(cmd), dict(env or {})))
        return self.handler(list(cmd))

    @property
    def commands(self) -> list[list[str]]:
        return [c for c, _ in self.calls]


def docker_handler(cmd: list[str]) -> sandbox.CommandResult:
    if cmd[:2] == ["docker", "port"]:
        return result("127.0.0.1:49153\n")
    return result()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSandbox:
    fake_sandbox = FakeSandbox(docker_handler)
    monkeypatch.setattr(sandbox, "run", fake_sandbox.run)
    return fake_sandbox


def docker_cfg(**overrides: Any) -> FactoryConfig:
    return replace(FactoryConfig(), postgres_backend="docker", **overrides)


# --- pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ("Run-1 Pytest", "run_1_pytest"),
    ("tasks_check", "tasks_check"),
    ("123abc", "db_123abc"),
    ("", "db_"),
    ("a" * 70, "a" * 63),
])
def test_safe_db_name(raw: str, expected: str) -> None:
    assert safe_db_name(raw) == expected


@pytest.mark.parametrize(("run_id", "expected"), [
    ("20261026-120000-tasks", "factory-pg-20261026-120000-tasks"),
    ("a b/c", "factory-pg-a-b-c"),
])
def test_container_name(run_id: str, expected: str) -> None:
    assert container_name(run_id) == expected


@pytest.mark.parametrize(("output", "port"), [
    ("127.0.0.1:49153\n", 49153),
    ("0.0.0.0:5000\n[::]:5000\n", 5000),
])
def test_parse_docker_port(output: str, port: int) -> None:
    assert parse_docker_port(output) == port


def test_parse_docker_port_rejects_garbage() -> None:
    with pytest.raises(PostgresUnavailable):
        parse_docker_port("no such container")


def test_docker_command_lines() -> None:
    run = docker_run_cmd("factory-pg-x", "postgres:16-alpine")
    assert run == ["docker", "run", "-d", "--rm", "--name", "factory-pg-x", "-e",
                   "POSTGRES_PASSWORD", "-p", "127.0.0.1::5432", "postgres:16-alpine"]
    assert docker_port_cmd("n") == ["docker", "port", "n", "5432/tcp"]
    assert docker_rm_cmd("n") == ["docker", "rm", "-f", "n"]
    assert docker_psql_cmd("n", "SELECT 1", "app") == [
        "docker", "exec", "n", "psql", "-h", "127.0.0.1", "-U", "postgres", "-d", "app",
        "-v", "ON_ERROR_STOP=1", "-tAc", "SELECT 1"]


def test_docker_env_keeps_only_docker_settings_and_the_password() -> None:
    host = {"DOCKER_HOST": "unix:///x", "DOCKER_CONFIG": "/c", "PATH": "/bin", "API_KEY": "k"}
    assert docker_env(host, "pw") == {"DOCKER_HOST": "unix:///x", "DOCKER_CONFIG": "/c",
                                      "POSTGRES_PASSWORD": "pw"}


def test_local_command_lines() -> None:
    bin_dir, data = Path("/pg/bin"), Path("/run/data")
    assert local_initdb_cmd(bin_dir, data, Path("/run/pw")) == [
        "/pg/bin/initdb", "-D", "/run/data", "-U", "postgres", "--auth=scram-sha-256",
        "--pwfile=/run/pw", "-E", "UTF8", "--locale=C"]
    start = local_postgres_cmd(bin_dir, data, 5544)
    assert start[:6] == ["/pg/bin/postgres", "-D", "/run/data", "-p", "5544", "-k"]
    assert "listen_addresses=127.0.0.1" in start
    assert local_ready_cmd(bin_dir, 5544) == [
        "/pg/bin/pg_isready", "-h", "127.0.0.1", "-p", "5544", "-U", "postgres"]
    assert local_psql_cmd(bin_dir, 5544, "SELECT 1")[-1] == "SELECT 1"


# --- docker lifecycle ---------------------------------------------------------------------------


def test_docker_server_lifecycle(fake: FakeSandbox, tmp_path: Path) -> None:
    name = container_name("run-1")
    with postgres_server(docker_cfg(), tmp_path, "run-1") as server:
        assert server.port == 49153
        server.create_database("tmpl")
        server.create_database("check_1", template="tmpl")
        server.drop_database("check_1")
        assert server.url("check_1") == (
            f"postgresql+psycopg://postgres:{server.password}@127.0.0.1:49153/check_1")
        password = server.password

    sql = [c[-1] for c in fake.commands if c[:3] == ["docker", "exec", name]]
    assert sql == ['SELECT 1',
                   'CREATE DATABASE "tmpl"',
                   'CREATE DATABASE "check_1" TEMPLATE "tmpl"',
                   'DROP DATABASE IF EXISTS "check_1" WITH (FORCE)']
    assert fake.commands[0][:2] == ["docker", "info"]
    assert fake.commands[-1] == docker_rm_cmd(name)
    # the password is only ever passed in the environment of `docker run`
    assert all(password not in " ".join(c) for c in fake.commands)
    run_env = next(env for c, env in fake.calls if c[:2] == ["docker", "run"])
    assert run_env["POSTGRES_PASSWORD"] == password
    assert len(password) >= 24


def test_docker_unreachable_fails_fast_without_starting_anything(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_sandbox = FakeSandbox(lambda cmd: result("Cannot connect", 1))
    monkeypatch.setattr(sandbox, "run", fake_sandbox.run)
    with (pytest.raises(PostgresUnavailable, match="Docker is not reachable"),
          postgres_server(docker_cfg(), tmp_path, "r")):
        pytest.fail("must not start")
    assert [c[:2] for c in fake_sandbox.commands] == [["docker", "info"]]


def test_container_is_removed_when_it_never_becomes_ready(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(cmd: list[str]) -> sandbox.CommandResult:
        if cmd[:3] == ["docker", "exec", container_name("r")]:
            return result("not ready", 1)
        return docker_handler(cmd)

    fake_sandbox = FakeSandbox(handler)
    monkeypatch.setattr(sandbox, "run", fake_sandbox.run)
    with (pytest.raises(PostgresUnavailable, match="not ready"),
          postgres_server(docker_cfg(postgres_ready_timeout_s=0), tmp_path, "r")):
        pytest.fail("must not start")
    assert fake_sandbox.commands[-1] == docker_rm_cmd(container_name("r"))


def test_container_is_removed_when_the_body_raises(fake: FakeSandbox, tmp_path: Path) -> None:
    with (pytest.raises(RuntimeError, match="boom"),
          postgres_server(docker_cfg(), tmp_path, "r")):
        raise RuntimeError("boom")
    assert fake.commands[-1] == docker_rm_cmd(container_name("r"))


def test_docker_run_failure_is_reported_and_cleaned_up(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(cmd: list[str]) -> sandbox.CommandResult:
        return result("pull access denied", 1) if cmd[:2] == ["docker", "run"] else result()

    fake_sandbox = FakeSandbox(handler)
    monkeypatch.setattr(sandbox, "run", fake_sandbox.run)
    with (pytest.raises(PostgresUnavailable, match="pull access denied"),
          postgres_server(docker_cfg(), tmp_path, "r")):
        pytest.fail("must not start")
    assert fake_sandbox.commands[-1][:2] == ["docker", "rm"]


def test_failed_statement_raises_postgres_error(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(cmd: list[str]) -> sandbox.CommandResult:
        if cmd[-1].startswith("CREATE"):
            return result("ERROR: database exists", 3)
        return docker_handler(cmd)

    monkeypatch.setattr(sandbox, "run", FakeSandbox(handler).run)
    with postgres_server(docker_cfg(), tmp_path, "r") as server, \
            pytest.raises(PostgresError, match="database exists"):
        server.create_database("dup")


def test_database_names_are_validated_before_reaching_sql(
        fake: FakeSandbox, tmp_path: Path) -> None:
    with postgres_server(docker_cfg(), tmp_path, "r") as server:
        for bad in ('x"; DROP DATABASE postgres; --', "Upper", "1db", ""):
            with pytest.raises(ValueError, match="not a safe database name"):
                server.create_database(bad)
            with pytest.raises(ValueError, match="not a safe database name"):
                server.drop_database(bad)
        with pytest.raises(ValueError, match="not a safe database name"):
            server.create_database("ok", template="Bad Name")
    assert not any("DROP DATABASE postgres" in " ".join(c) for c in fake.commands)


def test_password_is_url_quoted() -> None:
    fake_secret = "a/b:c@d"  # noqa: S105 - a test value, not a credential
    server = postgres.PostgresServer(port=1, password=fake_secret, _sql=lambda s, d: result())
    assert server.url("db") == "postgresql+psycopg://postgres:a%2Fb%3Ac%40d@127.0.0.1:1/db"


def test_unknown_backend_is_rejected(tmp_path: Path) -> None:
    cfg = replace(FactoryConfig(), postgres_backend="sqlite")
    with pytest.raises(ValueError, match="postgres_backend"), postgres_server(cfg, tmp_path, "r"):
        pytest.fail("must not start")


# --- local lifecycle ----------------------------------------------------------------------------


def make_bin_dir(root: Path) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for tool in ("initdb", "postgres", "pg_isready", "psql"):
        (bin_dir / tool).touch()
    return bin_dir


def test_local_backend_needs_the_binaries(tmp_path: Path) -> None:
    cfg = replace(FactoryConfig(), postgres_backend="local",
                  postgres_bin_dir=str(tmp_path / "nowhere"))
    with (pytest.raises(PostgresUnavailable, match="not found"),
          postgres_server(cfg, tmp_path / "run", "r")):
        pytest.fail("must not start")


def test_local_server_lifecycle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bin_dir = make_bin_dir(tmp_path)
    run_dir = tmp_path / "run"
    seen: dict[str, Any] = {}

    def handler(cmd: list[str]) -> sandbox.CommandResult:
        if cmd[0].endswith("initdb"):
            pwfile = Path(next(a for a in cmd if a.startswith("--pwfile=")).split("=", 1)[1])
            seen["pwfile_mode"] = pwfile.stat().st_mode & 0o777
            seen["pwfile_text"] = pwfile.read_text()
            (run_dir / "postgres" / "data").mkdir()
        return result()

    fake_sandbox = FakeSandbox(handler)
    monkeypatch.setattr(sandbox, "run", fake_sandbox.run)

    @contextmanager
    def fake_background(cmd: Sequence[str], cwd: Path, log_path: Path,
                        env: Mapping[str, str] | None = None) -> Iterator[Any]:
        seen["server_cmd"] = list(cmd)
        yield SimpleNamespace(poll=lambda: None, returncode=None)
        seen["stopped"] = True

    monkeypatch.setattr(sandbox, "background", fake_background)
    cfg = replace(FactoryConfig(), postgres_backend="local", postgres_bin_dir=str(bin_dir))
    with postgres_server(cfg, run_dir, "r") as server:
        server.create_database("tmpl")
        assert (run_dir / "postgres" / "data").exists()
        password = server.password
    psql_calls = [(c, env) for c, env in fake_sandbox.calls if c[0].endswith("psql")]
    assert psql_calls[0][0][-1] == 'CREATE DATABASE "tmpl"'
    assert psql_calls[0][1] == {"PGPASSWORD": password}
    assert all(password not in " ".join(c) for c in fake_sandbox.commands)
    assert seen["pwfile_mode"] == 0o600 and seen["pwfile_text"] == password
    assert seen["server_cmd"][0].endswith("postgres") and seen["stopped"]
    assert not (run_dir / "postgres").exists()  # data dir and password file are gone


def test_local_server_that_exits_early_is_reported(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bin_dir = make_bin_dir(tmp_path)
    monkeypatch.setattr(sandbox, "run", FakeSandbox().run)

    @contextmanager
    def dying(cmd: Sequence[str], cwd: Path, log_path: Path,
              env: Mapping[str, str] | None = None) -> Iterator[Any]:
        log_path.write_text("FATAL: could not bind")
        yield SimpleNamespace(poll=lambda: 1, returncode=1)

    monkeypatch.setattr(sandbox, "background", dying)
    cfg = replace(FactoryConfig(), postgres_backend="local", postgres_bin_dir=str(bin_dir))
    with (pytest.raises(PostgresUnavailable, match="could not bind"),
          postgres_server(cfg, tmp_path / "run", "r")):
        pytest.fail("must not start")
    assert not (tmp_path / "run" / "postgres").exists()


# --- real server (opt in) -----------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("FACTORY_E2E") != "1", reason="set FACTORY_E2E=1")
def test_real_server_template_and_clone(tmp_path: Path) -> None:
    """Uses FACTORY_POSTGRES_BACKEND (default docker); `local` works without Docker."""
    cfg = FactoryConfig.from_env()
    with postgres_server(cfg, tmp_path / "run", "pgtest") as server:
        server.create_database("tmpl")
        server.query("CREATE TABLE things (id serial PRIMARY KEY)", "tmpl")
        server.query("INSERT INTO things DEFAULT VALUES", "tmpl")
        server.create_database("check_1", template="tmpl")
        server.query("INSERT INTO things DEFAULT VALUES", "check_1")
        assert server.query("SELECT count(*) FROM things", "check_1") == "2"
        assert server.query("SELECT count(*) FROM things", "tmpl") == "1"
        server.drop_database("check_1")
        assert server.query("SELECT count(*) FROM pg_database WHERE datname = 'check_1'") == "0"
