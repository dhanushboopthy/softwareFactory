"""What the dashboard says about this machine: can a build actually run here?

Each check answers one question with a plain sentence and, when it fails, the fix. Nothing here
prints a secret: the provider check reports names ("OPENAI_API_KEY is not set"), never values.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from factory.harness import sandbox
from factory.harness.postgres import DOCKER_ENV
from factory.llm import ProviderConfigError, get_provider
from factory.ui.security import UiSettings


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: not applicable or not conclusive
    detail: str
    hint: str = ""


def check_provider(settings: UiSettings) -> Check:
    try:
        provider = get_provider(settings.env)
    except ProviderConfigError as exc:
        return Check("LLM provider", False, str(exc),
                     "Set LLM_PROVIDER (and the matching API key) in .env and restart the UI. "
                     "Builds without agents (--no-llm) work without one.")
    except Exception as exc:  # noqa: BLE001 - any SDK failure is just "not usable"
        return Check("LLM provider", False, f"{type(exc).__name__} while setting it up",
                     "Check LLM_PROVIDER, LLM_MODEL and the API key in .env.")
    return Check("LLM provider", True, f"{provider.name} / {provider.model}")


def check_postgres(settings: UiSettings) -> Check:
    cfg = settings.cfg
    if cfg.postgres_backend == "local":
        missing = [t for t in ("initdb", "postgres", "pg_isready", "psql")
                   if not (Path(cfg.postgres_bin_dir) / t).exists()]
        if missing:
            return Check("Postgres (local binaries)", False,
                         f"{', '.join(missing)} not found in {cfg.postgres_bin_dir}",
                         "Install PostgreSQL 16 or set FACTORY_POSTGRES_BIN_DIR.")
        return Check("Postgres (local binaries)", True, cfg.postgres_bin_dir)
    env = {k: settings.env[k] for k in DOCKER_ENV if k in settings.env}
    try:
        result = sandbox.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                             cwd=Path.cwd(), timeout_s=5, env=env)
    except FileNotFoundError:
        return Check("Postgres (Docker)", False, "the docker command was not found",
                     "Install Docker, or set FACTORY_POSTGRES_BACKEND=local "
                     "(and FACTORY_DEPLOY_CHECK=0).")
    if result.ok:
        return Check("Postgres (Docker)", True, f"Docker {result.stdout.strip()}")
    reason = (result.output.splitlines() or ["no answer"])[0][:160]
    return Check("Postgres (Docker)", False, reason,
                 "Start Docker, or set FACTORY_POSTGRES_BACKEND=local "
                 "(and FACTORY_DEPLOY_CHECK=0).")


def check_deploy(settings: UiSettings, postgres: Check) -> Check:
    cfg = settings.cfg
    if not cfg.deploy_check:
        return Check("Deploy check", None, "switched off")
    if cfg.postgres_backend == "local":
        return Check("Deploy check", None, "on, but it needs Docker and the local Postgres "
                     "backend suggests there is none",
                     "Set FACTORY_DEPLOY_CHECK=0, or the last station of every build will fail.")
    return Check("Deploy check", postgres.ok, "docker compose up, then a round trip"
                 if postgres.ok else "needs a working Docker")


def check_uv() -> Check:
    try:
        return Check("uv", True, sandbox.find_uv())
    except FileNotFoundError:
        return Check("uv", False, "not found", "Install uv: https://docs.astral.sh/uv/")


def check_runs_dir(settings: UiSettings) -> Check:
    runs = settings.cfg.runs_dir
    if not runs.exists():
        return Check("Runs directory", None, f"{runs} (created by the first build)")
    return Check("Runs directory", runs.is_dir(), str(runs),
                 "" if runs.is_dir() else "This path exists but is not a directory.")


class Environment:
    """The checks, cached briefly so a refreshing page does not run `docker info` every time."""

    def __init__(self, settings: UiSettings, ttl_s: float = 30,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.settings, self.ttl_s, self.clock = settings, ttl_s, clock
        self._cached: tuple[float, list[Check]] | None = None

    def checks(self) -> list[Check]:
        now = self.clock()
        if self._cached is not None and now - self._cached[0] < self.ttl_s:
            return self._cached[1]
        postgres = check_postgres(self.settings)
        result = [check_provider(self.settings), postgres,
                  check_deploy(self.settings, postgres), check_uv(),
                  check_runs_dir(self.settings)]
        self._cached = (now, result)
        return result

    def ready_to_build(self) -> bool:
        """True when nothing that a build needs is known to be broken."""
        return all(c.ok is not False for c in self.checks() if c.name != "LLM provider")
