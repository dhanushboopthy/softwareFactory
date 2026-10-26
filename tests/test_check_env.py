"""Entities-profile check environment: cloned databases, secrets, and how checks use them."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from factory.config import FactoryConfig
from factory.harness import checks, sandbox
from factory.harness.check_env import CHECK_HASH_ITERATIONS, CheckEnv
from factory.harness.postgres import PostgresServer
from factory.report.models import CheckResult


def result(stdout: str = "", returncode: int = 0) -> sandbox.CommandResult:
    return sandbox.CommandResult(cmd=(), returncode=returncode, stdout=stdout, stderr="",
                                 duration_s=0.0, timed_out=False)


class RecordingServer:
    """A PostgresServer whose SQL is recorded instead of run."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.server = PostgresServer(port=5544, password="pw/1", _sql=self.sql)  # noqa: S106

    def sql(self, statement: str, database: str) -> sandbox.CommandResult:
        self.statements.append(statement)
        return result()


def make_env() -> tuple[CheckEnv, RecordingServer]:
    recorder = RecordingServer()
    return CheckEnv(recorder.server, "template_db"), recorder


def test_fresh_databases_are_cloned_from_the_template_and_unique() -> None:
    env, recorder = make_env()
    first, second = env.fresh_db("pytest"), env.fresh_db("pytest")
    assert (first, second) == ("pytest_1", "pytest_2")
    assert recorder.statements == ['CREATE DATABASE "pytest_1" TEMPLATE "template_db"',
                                   'CREATE DATABASE "pytest_2" TEMPLATE "template_db"']


def test_labels_are_made_safe() -> None:
    env, recorder = make_env()
    assert env.fresh_db("Contract Run!") == "contract_run_1"
    assert recorder.statements[0].startswith('CREATE DATABASE "contract_run_1"')


def test_env_for_has_exactly_what_the_app_reads() -> None:
    env, _ = make_env()
    variables = env.env_for("pytest_1")
    assert set(variables) == {"DATABASE_URL", "JWT_SECRET", "PASSWORD_HASH_ITERATIONS"}
    assert variables["DATABASE_URL"] == "postgresql+psycopg://postgres:pw%2F1@127.0.0.1:5544/pytest_1"
    assert variables["PASSWORD_HASH_ITERATIONS"] == str(CHECK_HASH_ITERATIONS)
    assert len(variables["JWT_SECRET"]) >= 32  # secrets.token_urlsafe(32)


def test_the_secret_is_random_per_run_and_hidden_from_repr() -> None:
    first, _ = make_env()
    second, _ = make_env()
    assert first.jwt_secret != second.jwt_secret
    assert first.jwt_secret not in repr(first)


def test_fresh_env_creates_a_database_and_points_at_it() -> None:
    env, recorder = make_env()
    variables = env.fresh_env("pytest")
    assert variables["DATABASE_URL"].endswith("/pytest_1")
    assert len(recorder.statements) == 1


# --- the Schemathesis command line ----------------------------------------------------------


def test_schemathesis_command_without_auth_is_the_v1_command() -> None:
    cmd = checks.schemathesis_cmd(Path("p/openapi.yaml"), "http://127.0.0.1:9", FactoryConfig(),
                                  Path("r/report.json"))
    assert cmd[1:] == ["run", "p/openapi.yaml", "--url", "http://127.0.0.1:9",
                       "--checks", "all", "--max-examples", "30", "--seed", "42",
                       "--workers", "1", "--report", "json", "--report-json-path",
                       "r/report.json", "--no-color"]
    assert cmd[0].endswith("/st")


def test_schemathesis_command_with_auth_adds_one_header() -> None:
    cmd = checks.schemathesis_cmd(Path("p/openapi.yaml"), "http://127.0.0.1:9", FactoryConfig(),
                                  Path("r/report.json"), "Bearer tok123")
    assert cmd[-2:] == ["-H", "Authorization: Bearer tok123"]
    assert cmd.count("-H") == 1


def test_schemathesis_command_follows_the_config() -> None:
    cfg = replace(FactoryConfig(), schemathesis_max_examples=5, schemathesis_seed=7,
                  schemathesis_checks="not_a_server_error")
    cmd = checks.schemathesis_cmd(Path("o"), "u", cfg, Path("r"))
    assert cmd[cmd.index("--max-examples") + 1] == "5"
    assert cmd[cmd.index("--seed") + 1] == "7"
    assert cmd[cmd.index("--checks") + 1] == "not_a_server_error"


@pytest.mark.parametrize(("cmd", "expected"), [
    (["st", "-H", "Authorization: Bearer secret"], ["st", "-H", "Authorization: ***"]),
    (["st", "--header", "authorization: Basic abc"], ["st", "--header", "Authorization: ***"]),
    (["st", "-H", "X-Other: keep"], ["st", "-H", "X-Other: keep"]),
    (["st", "Authorization: Bearer not-a-header-value"],
     ["st", "Authorization: Bearer not-a-header-value"]),
])
def test_redact_cmd(cmd: list[str], expected: list[str]) -> None:
    assert checks.redact_cmd(cmd) == expected


def test_the_token_never_reaches_a_log_file(tmp_path: Path) -> None:
    res = sandbox.CommandResult(("st", "-H", "Authorization: Bearer topsecret"), 0, "", "", 0.1,
                                False)
    log = Path(checks._save_log(tmp_path, "contract", res)).read_text()
    assert "topsecret" not in log and "Authorization: ***" in log


# --- how pytest and run_all use the environment ---------------------------------------------


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, cmd: Sequence[str], cwd: Path, timeout_s: float,
                 env: Mapping[str, str] | None = None) -> sandbox.CommandResult:
        self.calls.append((list(cmd), dict(env or {})))
        return result()


def test_run_pytest_passes_env_and_test_paths(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeRun()
    monkeypatch.setattr(sandbox, "run", fake)
    checks.run_pytest(tmp_path, FactoryConfig(), tmp_path / "logs", name="red_check",
                      env={"DATABASE_URL": "x"}, test_paths=["tests/test_a.py", "tests/test_b.py"])
    cmd, env = fake.calls[0]
    assert cmd[-2:] == ["tests/test_a.py", "tests/test_b.py"]
    assert env == {"DATABASE_URL": "x"}


def test_run_pytest_default_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeRun()
    monkeypatch.setattr(sandbox, "run", fake)
    checks.run_pytest(tmp_path, FactoryConfig(), tmp_path / "logs")
    cmd, env = fake.calls[0]
    assert cmd[-1].startswith("--junitxml=") and env == {}


def test_collect_tests_passes_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeRun()
    monkeypatch.setattr(sandbox, "run", fake)
    checks.collect_tests(tmp_path, FactoryConfig(), tmp_path / "logs", env={"A": "1"})
    assert fake.calls[0][1] == {"A": "1"}


def test_run_all_gives_pytest_and_schemathesis_their_own_databases(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def ok(name: str) -> CheckResult:
        return CheckResult(name=name, passed=True, summary="ok")

    def fake_pytest(project: Path, cfg: FactoryConfig, log_dir: Path, **kwargs: Any) -> CheckResult:
        seen["pytest_env"] = kwargs["env"]
        return ok("pytest")

    def fake_schemathesis(project: Path, cfg: FactoryConfig, log_dir: Path,
                          check_env: CheckEnv | None = None, **kwargs: Any) -> CheckResult:
        seen["schemathesis_env"] = check_env
        return ok("schemathesis")

    monkeypatch.setattr(checks, "run_ruff", lambda *a, **k: ok("ruff"))
    monkeypatch.setattr(checks, "run_mypy", lambda *a, **k: ok("mypy"))
    monkeypatch.setattr(checks, "run_bandit", lambda *a, **k: ok("bandit"))
    monkeypatch.setattr(checks, "run_pytest", fake_pytest)
    monkeypatch.setattr(checks, "run_schemathesis", fake_schemathesis)
    env, recorder = make_env()
    results = checks.run_all(tmp_path, FactoryConfig(), tmp_path / "logs", env)
    assert [r.name for r in results] == ["ruff", "mypy", "bandit", "pytest", "schemathesis"]
    assert seen["pytest_env"]["DATABASE_URL"].endswith("/pytest_1")
    assert seen["schemathesis_env"] is env
    assert len(recorder.statements) == 1  # pytest's database; Schemathesis clones its own later


def test_run_all_without_check_env_is_the_openapi_profile(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def ok(name: str) -> CheckResult:
        return CheckResult(name=name, passed=True, summary="ok")

    def fake_pytest(project: Path, cfg: FactoryConfig, log_dir: Path, **kwargs: Any) -> CheckResult:
        seen["env"] = kwargs["env"]
        return ok("pytest")

    monkeypatch.setattr(checks, "run_ruff", lambda *a, **k: ok("ruff"))
    monkeypatch.setattr(checks, "run_mypy", lambda *a, **k: ok("mypy"))
    monkeypatch.setattr(checks, "run_bandit", lambda *a, **k: ok("bandit"))
    monkeypatch.setattr(checks, "run_pytest", fake_pytest)
    monkeypatch.setattr(checks, "run_schemathesis",
                        lambda *a, **k: seen.update(check_env=k["check_env"]) or ok("st"))
    checks.run_all(tmp_path, FactoryConfig(), tmp_path / "logs")
    assert seen == {"env": None, "check_env": None}
