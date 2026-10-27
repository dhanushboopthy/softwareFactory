"""Station 5: final contract test (Schemathesis against the live server, on a fresh database), and
for the entities profile the deploy check: does the generated project actually come up with
`docker compose` and serve one authenticated round trip?
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, route_of
from factory.harness import checks, sandbox
from factory.harness.check_env import CheckEnv
from factory.harness.postgres import DOCKER_ENV, docker_info_cmd
from factory.report.models import CheckResult
from factory.stations.scaffold_entities import create_payload


def run_contract(
    project: Path, cfg: FactoryConfig, run_dir: Path, check_env: CheckEnv | None = None,
) -> CheckResult:
    return checks.run_schemathesis(project, cfg, run_dir / "contract", name="contract",
                                   check_env=check_env)


# --- deploy check (OV#13) -------------------------------------------------------------------------


def compose_project_name(run_id: str) -> str:
    """A per-run compose project name, so concurrent runs and leftovers never collide."""
    return "factory-" + re.sub(r"[^a-z0-9]+", "-", run_id.lower()).strip("-")[:40]


def compose_up_cmd(project_name: str, timeout_s: int) -> list[str]:
    return ["docker", "compose", "-p", project_name, "up", "-d", "--build", "--wait",
            "--wait-timeout", str(timeout_s)]


def compose_logs_cmd(project_name: str) -> list[str]:
    return ["docker", "compose", "-p", project_name, "logs", "--no-color", "--tail", "200"]


def compose_down_cmd(project_name: str) -> list[str]:
    return ["docker", "compose", "-p", project_name, "down", "-v", "--remove-orphans"]


def deploy_env(host_env: Mapping[str, str], app_port: int) -> dict[str, str]:
    """What compose needs: fresh secrets (never written to a file under runs/), a free host port
    for the app (the database is not published), and the Docker client settings."""
    env = {k: host_env[k] for k in DOCKER_ENV if k in host_env}
    env["JWT_SECRET"] = secrets.token_urlsafe(32)
    env["POSTGRES_PASSWORD"] = secrets.token_urlsafe(24).replace("=", "")
    env["APP_PORT"] = str(app_port)
    return env


def round_trip(client: httpx.Client, doc: EntitiesDoc) -> list[str]:
    """/health, then sign up, log in, create the first top-level entity and read it back.

    Returns the problems found (empty when the deployed app is healthy and usable)."""
    problems: list[str] = []
    health = client.get("/health")
    if health.status_code != 200:
        return [f"GET /health returned {health.status_code}: {health.text[:200]}"]
    credentials = {"email": f"deploy-{secrets.token_hex(4)}@example.com",
                   "password": secrets.token_urlsafe(16)}
    signup = client.post("/auth/signup", json=credentials)
    if signup.status_code != 201:
        return [f"POST /auth/signup returned {signup.status_code}: {signup.text[:200]}"]
    login = client.post("/auth/login", json=credentials)
    if login.status_code != 200:
        return [f"POST /auth/login returned {login.status_code}: {login.text[:200]}"]
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    entity = doc.ordered()[0]  # parents come first, so this is a top-level entity
    collection = route_of(doc, entity).collection
    created = client.post(collection, json=create_payload(doc, entity, 1), headers=headers)
    if created.status_code != 201:
        return [f"POST {collection} returned {created.status_code}: {created.text[:200]}"]
    body: dict[str, Any] = created.json()
    fetched = client.get(f"{collection}/{body['id']}", headers=headers)
    if fetched.status_code != 200 or fetched.json() != body:
        problems.append(f"GET {collection}/{body['id']} did not return what was created "
                        f"({fetched.status_code})")
    if client.get(collection).status_code != 401:
        problems.append(f"GET {collection} without a token did not return 401")
    return problems


def run_deploy_check(
    project: Path, cfg: FactoryConfig, run_dir: Path, run_id: str, doc: EntitiesDoc,
) -> CheckResult:
    """Build and start the generated project with `docker compose`, exercise it, tear it down.

    The compose project always goes `down -v` in a finally, and has its own timeout."""
    name = "deploy"
    log_dir = run_dir / "deploy"
    log_dir.mkdir(parents=True, exist_ok=True)
    port = sandbox.free_port()
    env = deploy_env(os.environ, port)
    project_name = compose_project_name(run_id)

    def fail(summary: str, *details: str, log: bool = True) -> CheckResult:
        return CheckResult(name=name, passed=False, summary=summary, failures=list(details),
                           log_path=str(log_dir / "compose.log") if log else None)

    info = sandbox.run(docker_info_cmd(), cwd=project, timeout_s=30, env=env)
    if not info.ok:
        return fail("Docker is not reachable", info.output[-300:], log=False)
    try:
        up = sandbox.run(compose_up_cmd(project_name, cfg.deploy_timeout_s), cwd=project,
                         timeout_s=cfg.deploy_timeout_s + 60, env=env)
        logs = sandbox.run(compose_logs_cmd(project_name), cwd=project, timeout_s=60, env=env)
        (log_dir / "compose.log").write_text(
            f"$ {' '.join(compose_up_cmd(project_name, cfg.deploy_timeout_s))}\n"
            f"[exit {up.returncode}]\n{up.output}\n\n--- service logs ---\n{logs.output}\n")
        if not up.ok:
            return fail("docker compose up failed" + (" (timed out)" if up.timed_out else ""),
                        up.output[-1500:], logs.output[-1500:])
        # Plain HTTP to localhost: no TLS, so there is no reason to load a CA bundle.
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15,
                          verify=False) as client:  # noqa: S501
            problems = round_trip(client, doc)
        if problems:
            return fail("the deployed app is not healthy", *problems, logs.output[-1500:])
        return CheckResult(name=name, passed=True, log_path=str(log_dir / "compose.log"),
                           summary=f"compose project {project_name} healthy; "
                                   "signed up, logged in, created and read back a row")
    except httpx.HTTPError as exc:
        return fail("the deployed app could not be reached", str(exc))
    finally:
        sandbox.run(compose_down_cmd(project_name), cwd=project, timeout_s=120, env=env)
