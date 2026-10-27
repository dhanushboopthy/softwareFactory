"""One Postgres server per run, a migrated template database, and a cloned database per check.

Two backends, chosen by ``cfg.postgres_backend``:

* ``docker``: a throwaway container, published on 127.0.0.1 only, always removed in a ``finally``.
* ``local``: a server started from the PostgreSQL binaries in ``cfg.postgres_bin_dir``, with its
  data directory under the run directory. For machines without a Docker daemon.

The superuser password is random per run. It reaches the server and the generated project through
environment variables, never through a command line, and is never written to a file that outlives
``initdb``. Command lines are built by pure functions so they can be unit-tested.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from factory.config import FactoryConfig
from factory.harness import sandbox

BACKENDS = ("docker", "local")
HOST = "127.0.0.1"
SUPERUSER = "postgres"
# Docker client settings that must reach `docker` commands (and only those commands).
DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PostgresUnavailable(RuntimeError):
    """Postgres cannot be provided (Docker down, binaries missing, never became ready)."""


class PostgresError(RuntimeError):
    """A statement sent to the server failed."""


# --- pure helpers and command builders ----------------------------------------------------------


def safe_db_name(raw: str) -> str:
    """A lowercase SQL identifier derived from ``raw``: [a-z0-9_], not starting with a digit."""
    name = re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")
    if not name or name[0].isdigit():
        name = f"db_{name}"
    return name[:63]


def container_name(run_id: str) -> str:
    return "factory-pg-" + re.sub(r"[^a-zA-Z0-9_.-]+", "-", run_id).strip("-.")[:50]


def parse_docker_port(output: str) -> int:
    """The host port from ``docker port`` output such as ``127.0.0.1:49153``."""
    for line in output.splitlines():
        match = re.search(r":(\d+)\s*$", line.strip())
        if match:
            return int(match.group(1))
    raise PostgresUnavailable(f"could not read the published port from: {output.strip()!r}")


def docker_info_cmd() -> list[str]:
    return ["docker", "info", "--format", "{{.ServerVersion}}"]


def docker_run_cmd(name: str, image: str) -> list[str]:
    # `-e POSTGRES_PASSWORD` (no value) copies it from the docker client's environment.
    return ["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD",
            "-p", f"{HOST}::5432", image]


def docker_port_cmd(name: str) -> list[str]:
    return ["docker", "port", name, "5432/tcp"]


def docker_psql_cmd(name: str, sql: str, database: str = "postgres") -> list[str]:
    # Over TCP inside the container: the image's temporary init-time server has no TCP listener,
    # so this only succeeds once the real server is up.
    return ["docker", "exec", name, "psql", "-h", HOST, "-U", SUPERUSER, "-d", database,
            "-v", "ON_ERROR_STOP=1", "-tAc", sql]


def docker_rm_cmd(name: str) -> list[str]:
    return ["docker", "rm", "-f", name]


def docker_env(host_env: Mapping[str, str], password: str) -> dict[str, str]:
    env = {k: host_env[k] for k in DOCKER_ENV if k in host_env}
    env["POSTGRES_PASSWORD"] = password
    return env


def local_initdb_cmd(bin_dir: Path, data_dir: Path, pwfile: Path) -> list[str]:
    return [str(bin_dir / "initdb"), "-D", str(data_dir), "-U", SUPERUSER,
            "--auth=scram-sha-256", f"--pwfile={pwfile}", "-E", "UTF8", "--locale=C"]


def local_postgres_cmd(bin_dir: Path, data_dir: Path, port: int) -> list[str]:
    # Throwaway data: durability settings off for speed. No unix socket (`-k ""`): TCP only.
    return [str(bin_dir / "postgres"), "-D", str(data_dir), "-p", str(port), "-k", "",
            "-c", f"listen_addresses={HOST}", "-c", "max_connections=50",
            "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off"]


def local_ready_cmd(bin_dir: Path, port: int) -> list[str]:
    return [str(bin_dir / "pg_isready"), "-h", HOST, "-p", str(port), "-U", SUPERUSER]


def local_psql_cmd(bin_dir: Path, port: int, sql: str, database: str = "postgres") -> list[str]:
    return [str(bin_dir / "psql"), "-h", HOST, "-p", str(port), "-U", SUPERUSER, "-d", database,
            "-v", "ON_ERROR_STOP=1", "-tAc", sql]


# --- the server handle --------------------------------------------------------------------------


@dataclass(frozen=True)
class PostgresServer:
    """A running server. ``_sql`` runs one statement as the superuser."""

    port: int
    password: str
    _sql: Callable[[str, str], sandbox.CommandResult]

    def url(self, database: str) -> str:
        """SQLAlchemy URL (psycopg 3 driver) for ``database`` as the superuser."""
        return (f"postgresql+psycopg://{SUPERUSER}:{quote(self.password, safe='')}"
                f"@{HOST}:{self.port}/{database}")

    def query(self, sql: str, database: str = "postgres") -> str:
        """Run one statement, return its tuples-only output; raise PostgresError on failure."""
        result = self._sql(sql, database)
        if not result.ok:
            raise PostgresError(f"{sql!r} failed: {result.output[-500:]}")
        return result.stdout.strip()

    def create_database(self, name: str, template: str | None = None) -> None:
        sql = f'CREATE DATABASE "{_identifier(name)}"'
        if template is not None:
            sql += f' TEMPLATE "{_identifier(template)}"'
        self.query(sql)

    def drop_database(self, name: str) -> None:
        self.query(f'DROP DATABASE IF EXISTS "{_identifier(name)}" WITH (FORCE)')


def _identifier(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"{name!r} is not a safe database name; use safe_db_name()")
    return name


def _wait_until(check: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        if check():
            return
        if time.monotonic() >= deadline:
            raise PostgresUnavailable(f"{what} not ready after {timeout_s}s")
        time.sleep(0.3)


# --- backends -----------------------------------------------------------------------------------


@contextmanager
def _docker_server(cfg: FactoryConfig, run_dir: Path, run_id: str) -> Iterator[PostgresServer]:
    password = secrets.token_urlsafe(24)
    env = docker_env(os.environ, password)
    name = container_name(run_id)

    def run(cmd: list[str], timeout_s: float = 60) -> sandbox.CommandResult:
        return sandbox.run(cmd, cwd=run_dir, timeout_s=timeout_s, env=env)

    run_dir.mkdir(parents=True, exist_ok=True)
    info = run(docker_info_cmd(), 30)
    if not info.ok:
        raise PostgresUnavailable(f"Docker is not reachable: {info.output[-300:]}")
    try:
        started = run(docker_run_cmd(name, cfg.postgres_image), cfg.postgres_ready_timeout_s * 5)
        if not started.ok:
            raise PostgresUnavailable(
                f"could not start {cfg.postgres_image}: {started.output[-300:]}")
        port = parse_docker_port(run(docker_port_cmd(name)).stdout)
        _wait_until(lambda: run(docker_psql_cmd(name, "SELECT 1"), 10).ok,
                    cfg.postgres_ready_timeout_s, "Postgres container")
        yield PostgresServer(
            port=port, password=password,
            _sql=lambda sql, db: run(docker_psql_cmd(name, sql, db)),
        )
    finally:
        with contextlib.suppress(Exception):
            run(docker_rm_cmd(name), 30)


@contextmanager
def _local_server(cfg: FactoryConfig, run_dir: Path) -> Iterator[PostgresServer]:
    bin_dir = Path(cfg.postgres_bin_dir)
    for tool in ("initdb", "postgres", "pg_isready", "psql"):
        if not (bin_dir / tool).exists():
            raise PostgresUnavailable(
                f"PostgreSQL binary {tool!r} not found in {bin_dir}; set FACTORY_POSTGRES_BIN_DIR "
                "or use FACTORY_POSTGRES_BACKEND=docker")
    password = secrets.token_urlsafe(24)
    base = run_dir / "postgres"
    data_dir = base / "data"
    port = sandbox.free_port()
    env = {"PGPASSWORD": password}
    base.mkdir(parents=True, exist_ok=True)
    try:
        pwfile = base / "pw"
        pwfile.touch(mode=0o600)
        pwfile.write_text(password)
        try:
            init = sandbox.run(local_initdb_cmd(bin_dir, data_dir, pwfile), cwd=base,
                               timeout_s=cfg.postgres_ready_timeout_s)
        finally:
            pwfile.unlink(missing_ok=True)
        if not init.ok:
            raise PostgresUnavailable(f"initdb failed: {init.output[-500:]}")
        log = run_dir / "postgres.log"  # outside `base`, so it survives the cleanup below
        with sandbox.background(local_postgres_cmd(bin_dir, data_dir, port), cwd=base,
                                log_path=log) as proc:

            def ready() -> bool:
                if proc.poll() is not None:
                    tail = log.read_text()[-500:] if log.exists() else ""
                    raise PostgresUnavailable(f"postgres exited with {proc.returncode}: {tail}")
                return sandbox.run(local_ready_cmd(bin_dir, port), cwd=base, timeout_s=10).ok

            _wait_until(ready, cfg.postgres_ready_timeout_s, "Postgres")
            yield PostgresServer(
                port=port, password=password,
                _sql=lambda sql, db: sandbox.run(local_psql_cmd(bin_dir, port, sql, db),
                                                 cwd=base, timeout_s=60, env=env),
            )
    finally:
        shutil.rmtree(base, ignore_errors=True)


@contextmanager
def postgres_server(cfg: FactoryConfig, run_dir: Path, run_id: str) -> Iterator[PostgresServer]:
    """Start the configured backend; stop and clean up on exit, whatever happened."""
    if cfg.postgres_backend not in BACKENDS:
        raise ValueError(f"postgres_backend must be one of {BACKENDS}, got "
                         f"{cfg.postgres_backend!r}")
    if cfg.postgres_backend == "docker":
        with _docker_server(cfg, run_dir, run_id) as server:
            yield server
    else:
        with _local_server(cfg, run_dir) as server:
            yield server
