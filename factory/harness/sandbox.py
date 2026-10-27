"""Run commands inside a run directory with timeouts, a scrubbed environment and guaranteed cleanup.

Generated code never sees the factory's environment: API keys and other secrets are not passed
through. Network isolation is documented but not enforced here (planned: Docker with no network).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess  # the sandbox is the one place the factory spawns processes
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

# Only these variables reach child processes. Everything else (notably *_API_KEY) is dropped.
PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "UV_CACHE_DIR",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


@dataclass(frozen=True)
class CommandResult:
    cmd: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()


def scrubbed_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in PASSTHROUGH_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def _kill_group(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def run(
    cmd: Sequence[str],
    cwd: Path,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run ``cmd`` in its own process group; on timeout the whole group is killed."""
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - argv list built by the factory, no shell
        list(cmd),
        cwd=cwd,
        env=scrubbed_env(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        timed_out = True
    except BaseException:
        # Ctrl-C, SIGTERM or any other interruption: the child has its own session, so it would
        # otherwise keep running (uv, pytest, uvicorn...) after the factory has stopped.
        _kill_group(proc)
        proc.communicate()
        raise
    return CommandResult(
        cmd=tuple(cmd),
        returncode=None if timed_out else proc.returncode,
        stdout=stdout,
        stderr=stderr + (f"\n[factory] killed after {timeout_s}s timeout" if timed_out else ""),
        duration_s=round(time.monotonic() - start, 2),
        timed_out=timed_out,
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def find_uv() -> str:
    """Locate the uv binary (uv sets $UV for processes it launches)."""
    candidates = [os.environ.get("UV"), shutil.which("uv"), str(Path.home() / ".local/bin/uv")]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise FileNotFoundError("uv not found; install it from https://docs.astral.sh/uv/")


@contextlib.contextmanager
def background(
    cmd: Sequence[str],
    cwd: Path,
    log_path: Path,
    env: Mapping[str, str] | None = None,
) -> Iterator[subprocess.Popen[str]]:
    """Run ``cmd`` in its own process group, output to ``log_path``; always stopped on exit."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        proc = subprocess.Popen(  # noqa: S603 - argv list built by the factory, no shell
            list(cmd),
            cwd=cwd,
            env=scrubbed_env(env),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            yield proc
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                proc.wait()


@dataclass
class Server:
    base_url: str
    log_path: Path


@contextlib.contextmanager
def serve(
    python: Path,
    project: Path,
    log_path: Path,
    env: Mapping[str, str] | None = None,
    start_timeout_s: float = 30,
    health_path: str = "/openapi.json",
) -> Iterator[Server]:
    """Start ``uvicorn app.main:app`` on a free port, wait for health, always shut it down."""
    port = free_port()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        proc = subprocess.Popen(  # noqa: S603 - argv list built by the factory, no shell
            [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=project,
            env=scrubbed_env(env),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + start_timeout_s
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(f"server exited with code {proc.returncode}; see {log_path}")
                try:
                    if httpx.get(base_url + health_path, timeout=1.0).status_code < 500:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"server not healthy after {start_timeout_s}s; see {log_path}"
                    )
                time.sleep(0.2)
            yield Server(base_url=base_url, log_path=log_path)
        finally:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                proc.wait()
