"""The deploy check: command lines, the round trip, and cleanup (fake Docker, in-memory app)."""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, load_entities
from factory.harness import sandbox
from factory.stations import contract
from factory.stations.contract import (
    compose_down_cmd,
    compose_logs_cmd,
    compose_project_name,
    compose_up_cmd,
    deploy_env,
    round_trip,
    run_deploy_check,
)

from .conftest import ROOT

EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"


def example() -> EntitiesDoc:
    return load_entities(EXAMPLE)


# --- pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize(("run_id", "expected"), [
    ("20261026-120000-tasks", "factory-20261026-120000-tasks"),
    ("My_Run!", "factory-my-run"),
    ("a" * 80, "factory-" + "a" * 40),
])
def test_compose_project_name(run_id: str, expected: str) -> None:
    assert compose_project_name(run_id) == expected


def test_compose_command_lines() -> None:
    assert compose_up_cmd("factory-x", 600) == [
        "docker", "compose", "-p", "factory-x", "up", "-d", "--build", "--wait",
        "--wait-timeout", "600"]
    assert compose_down_cmd("factory-x") == [
        "docker", "compose", "-p", "factory-x", "down", "-v", "--remove-orphans"]
    assert compose_logs_cmd("factory-x")[:5] == ["docker", "compose", "-p", "factory-x", "logs"]


def test_deploy_env_has_fresh_secrets_a_port_and_only_docker_settings() -> None:
    host = {"DOCKER_HOST": "unix:///x", "DOCKER_CONTEXT": "c", "PATH": "/bin", "API_KEY": "k"}
    first, second = deploy_env(host, 18080), deploy_env(host, 18080)
    assert set(first) == {"DOCKER_HOST", "DOCKER_CONTEXT", "JWT_SECRET", "POSTGRES_PASSWORD",
                          "APP_PORT"}
    assert first["APP_PORT"] == "18080" and first["DOCKER_HOST"] == "unix:///x"
    assert first["JWT_SECRET"] != second["JWT_SECRET"]
    assert first["POSTGRES_PASSWORD"] != second["POSTGRES_PASSWORD"]
    assert len(first["JWT_SECRET"]) >= 32
    assert "=" not in first["POSTGRES_PASSWORD"]  # it sits inside a URL in compose.yaml


# --- the round trip against an in-memory app ----------------------------------------------------


def fake_app(broken: str | None = None) -> httpx.Client:
    """A tiny stand-in for the deployed tasks app; ``broken`` switches one behaviour off."""
    rows: dict[int, dict[str, Any]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        authed = request.headers.get("authorization") == "Bearer token-123"
        if path == "/health":
            return httpx.Response(500 if broken == "health" else 200, json={"status": "ok"})
        if path == "/auth/signup":
            return httpx.Response(409 if broken == "signup" else 201, json={"id": 1, "email": "x"})
        if path == "/auth/login":
            if broken == "login":
                return httpx.Response(401, json={"detail": "no"})
            return httpx.Response(200, json={"access_token": "token-123", "token_type": "bearer"})
        if path == "/projects" and method == "POST":
            if not authed:
                return httpx.Response(401, json={"detail": "no"})
            if broken == "create":
                return httpx.Response(500, json={"detail": "boom"})
            row = {"id": len(rows) + 1, **json.loads(request.content)}
            rows[row["id"]] = row
            return httpx.Response(201, json=row)
        if path.startswith("/projects/") and method == "GET":
            found: dict[str, Any] | None = rows.get(int(path.rsplit("/", 1)[1]))
            if broken == "readback" and found:
                found = {**found, "name": "changed"}
            return httpx.Response(200 if found else 404, json=found or {"detail": "nope"})
        if path == "/projects" and method == "GET":
            return httpx.Response(200 if broken == "open" else 401, json=[])
        return httpx.Response(404, json={"detail": path})

    return httpx.Client(base_url="http://app", transport=httpx.MockTransport(handler))


def test_a_healthy_app_passes_the_round_trip() -> None:
    assert round_trip(fake_app(), example()) == []


@pytest.mark.parametrize(("broken", "expected"), [
    ("health", "GET /health returned 500"),
    ("signup", "POST /auth/signup returned 409"),
    ("login", "POST /auth/login returned 401"),
    ("create", "POST /projects returned 500"),
    ("readback", "did not return what was created"),
    ("open", "without a token did not return 401"),
])
def test_each_way_the_deployed_app_can_be_broken_is_reported(broken: str, expected: str) -> None:
    problems = round_trip(fake_app(broken), example())
    assert any(expected in p for p in problems), problems


def test_the_round_trip_uses_the_first_top_level_entity() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(500)

    round_trip(httpx.Client(base_url="http://app", transport=httpx.MockTransport(handler)),
               example())
    assert seen == ["GET /health"]  # stops at the first failure


# --- run_deploy_check with a fake Docker --------------------------------------------------------


Handler = Callable[[list[str]], sandbox.CommandResult]


def result(stdout: str = "", returncode: int = 0, timed_out: bool = False) -> sandbox.CommandResult:
    return sandbox.CommandResult((), None if timed_out else returncode, stdout, "", 0.1, timed_out)


class FakeDocker:
    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler or (lambda cmd: result("ok"))
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def run(self, cmd: Sequence[str], cwd: Path, timeout_s: float,
            env: dict[str, str] | None = None) -> sandbox.CommandResult:
        self.calls.append((list(cmd), dict(env or {})))
        return self.handler(list(cmd))

    @property
    def commands(self) -> list[list[str]]:
        return [c for c, _ in self.calls]

    def compose(self, verb: str) -> list[list[str]]:
        return [c for c in self.commands if c[:2] == ["docker", "compose"] and verb in c]


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(sandbox, "run", fake.run)
    monkeypatch.setattr(contract, "round_trip", lambda client, doc: [])
    return fake


def deploy(tmp_path: Path, **cfg: Any) -> Any:
    return run_deploy_check(tmp_path / "project", FactoryConfig(**cfg), tmp_path / "run",
                            "20261026-tasks", example())


def test_a_healthy_deploy_passes_and_is_always_torn_down(docker: FakeDocker,
                                                         tmp_path: Path) -> None:
    check = deploy(tmp_path)
    assert check.passed and check.name == "deploy"
    assert "factory-20261026-tasks" in check.summary and "created and read back" in check.summary
    assert docker.commands[0][:2] == ["docker", "info"]
    up = docker.compose("up")[0]
    assert up[:4] == ["docker", "compose", "-p", "factory-20261026-tasks"]
    assert docker.commands[-1] == compose_down_cmd("factory-20261026-tasks")  # down -v, last
    assert (tmp_path / "run" / "deploy" / "compose.log").exists()


def test_secrets_travel_only_in_the_environment(docker: FakeDocker, tmp_path: Path) -> None:
    deploy(tmp_path)
    up_env = next(env for cmd, env in docker.calls if "up" in cmd and cmd[1] == "compose")
    assert set(up_env) >= {"JWT_SECRET", "POSTGRES_PASSWORD", "APP_PORT"}
    for cmd in docker.commands:
        joined = " ".join(cmd)
        assert up_env["JWT_SECRET"] not in joined and up_env["POSTGRES_PASSWORD"] not in joined
    log = (tmp_path / "run" / "deploy" / "compose.log").read_text()
    assert up_env["JWT_SECRET"] not in log and up_env["POSTGRES_PASSWORD"] not in log
    assert not list((tmp_path / "run").rglob(".env"))  # nothing secret on disk under runs/


def test_the_app_gets_an_unprivileged_host_port(docker: FakeDocker, tmp_path: Path) -> None:
    deploy(tmp_path)
    port = int(next(env["APP_PORT"] for _, env in docker.calls if "APP_PORT" in env))
    assert 1024 < port < 65536


def test_docker_unreachable_fails_without_starting_anything(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeDocker(lambda cmd: result("Cannot connect to the Docker daemon", 1))
    monkeypatch.setattr(sandbox, "run", fake.run)
    check = deploy(tmp_path)
    assert not check.passed and check.summary == "Docker is not reachable"
    assert "Cannot connect" in check.failures[0] and check.log_path is None
    assert fake.compose("up") == [] and fake.compose("down") == []


def test_a_failed_build_is_reported_with_logs_and_cleaned_up(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def handler(cmd: list[str]) -> sandbox.CommandResult:
        if "up" in cmd:
            return result("failed to solve: RUN uv sync", 1)
        if "logs" in cmd:
            return result("app-1 | Traceback ...")
        return result("ok")

    fake = FakeDocker(handler)
    monkeypatch.setattr(sandbox, "run", fake.run)
    monkeypatch.setattr(contract, "round_trip", lambda client, doc: pytest.fail("not reached"))
    check = deploy(tmp_path)
    assert not check.passed and check.summary == "docker compose up failed"
    assert "failed to solve" in check.failures[0] and "Traceback" in check.failures[1]
    log = (tmp_path / "run" / "deploy" / "compose.log").read_text()
    assert "failed to solve" in log and "--- service logs ---" in log and "Traceback" in log
    assert fake.commands[-1] == compose_down_cmd("factory-20261026-tasks")


def test_a_compose_timeout_is_called_out(monkeypatch: pytest.MonkeyPatch,
                                         tmp_path: Path) -> None:
    fake = FakeDocker(lambda cmd: result("", timed_out=True) if "up" in cmd else result("ok"))
    monkeypatch.setattr(sandbox, "run", fake.run)
    check = deploy(tmp_path, deploy_timeout_s=5)
    assert check.summary == "docker compose up failed (timed out)"
    assert fake.compose("down")


def test_the_timeout_comes_from_the_config(docker: FakeDocker, tmp_path: Path) -> None:
    deploy(tmp_path, deploy_timeout_s=42)
    assert docker.compose("up")[0][-2:] == ["--wait-timeout", "42"]


def test_an_unhealthy_app_fails_with_the_problems_and_the_logs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeDocker(lambda cmd: result("app-1 | boom") if "logs" in cmd else result("ok"))
    monkeypatch.setattr(sandbox, "run", fake.run)
    monkeypatch.setattr(contract, "round_trip", lambda client, doc: ["GET /health returned 500"])
    check = deploy(tmp_path)
    assert not check.passed and check.summary == "the deployed app is not healthy"
    assert check.failures[0] == "GET /health returned 500" and "boom" in check.failures[1]
    assert fake.compose("down")


def test_a_connection_error_is_reported_and_still_torn_down(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeDocker()
    monkeypatch.setattr(sandbox, "run", fake.run)

    def refuse(client: httpx.Client, doc: EntitiesDoc) -> list[str]:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(contract, "round_trip", refuse)
    check = deploy(tmp_path)
    assert not check.passed and check.summary == "the deployed app could not be reached"
    assert fake.commands[-1] == compose_down_cmd("factory-20261026-tasks")
