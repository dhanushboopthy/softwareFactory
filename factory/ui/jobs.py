"""Running builds for the UI.

A build runs as a separate process (`python -m factory build ...`): a crash cannot take the server
down, and Cancel is an ordinary Ctrl-C, so the build's own cleanup (Postgres, docker compose) still
runs. Only one build runs at a time; it uses Postgres, `uv`, maybe Docker, and LLM money.
Progress needs no extra state: the build writes `runs/<id>/run.json` as it goes.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from factory.entities import EntitiesDoc, EntitiesError, load_entities_text, parse_entities
from factory.pipeline import NOT_APPROVED, new_run_id
from factory.report.runs import valid_run_id
from factory.ui.security import UiSettings

LOG_DIR = ".ui"  # under the runs directory; not a valid run id, so it never lists as a run
GRACE_S = 60  # how long a cancelled build gets to clean up before "force stop" is offered
Spawn = Callable[..., "subprocess.Popen[bytes]"]


class BuildBusy(RuntimeError):
    """A build is already running."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"build {run_id} is still running")
        self.run_id = run_id


@dataclass
class Job:
    run_id: str
    proc: subprocess.Popen[bytes]
    log_path: Path
    started_at: datetime
    cancel_requested_at: datetime | None = None
    argv: tuple[str, ...] = field(default=(), repr=False)

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.proc.poll()

    @property
    def cancelling(self) -> bool:
        return self.running and self.cancel_requested_at is not None

    def force_offered(self, now: datetime | None = None) -> bool:
        """True once a cancelled build has had GRACE_S seconds and is still not gone."""
        if not self.cancelling or self.cancel_requested_at is None:
            return False
        return ((now or datetime.now(UTC)) - self.cancel_requested_at).total_seconds() > GRACE_S


def build_argv(source: Path, run_id: str, no_llm: bool) -> list[str]:
    argv = [sys.executable, "-m", "factory", "build", str(source), "--run-id", run_id]
    return [*argv, "--no-llm"] if no_llm else argv


def build_env(settings: UiSettings) -> dict[str, str]:
    """What the build process sees: this server's environment (it needs the LLM keys, as the
    command line would), with the runs directory pinned so both sides look in the same place."""
    env = dict(settings.env)
    env["FACTORY_RUNS_DIR"] = str(settings.cfg.runs_dir.resolve())
    env.pop("FACTORY_UI_TOKEN", None)  # the build has no use for the UI's access token
    return env


def validate_for_build(text: str) -> tuple[EntitiesDoc | None, list[str]]:
    """(document, []) for a valid, approved file; else (the document if it parsed, problems)."""
    try:
        doc = parse_entities(load_entities_text(text))
    except EntitiesError as exc:
        return None, exc.problems
    if not doc.approved:
        return doc, [NOT_APPROVED]
    return doc, []


def unique_run_id(settings: UiSettings, source: Path, app: str) -> str:
    """`<timestamp>-<app>`, with a numeric suffix if that run (or its saved input) exists."""
    base = new_run_id(source, app)
    candidate, n = base, 1
    while (settings.cfg.runs_dir / candidate).exists() or (
            settings.cfg.drafts_dir / candidate).exists():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


class BuildRunner:
    def __init__(self, settings: UiSettings, spawn: Spawn = subprocess.Popen,
                 argv: Callable[[Path, str, bool], Sequence[str]] = build_argv,
                 shutdown_wait_s: float = 30) -> None:
        self.settings, self._spawn, self._argv = settings, spawn, argv
        self.shutdown_wait_s = shutdown_wait_s
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._latest: str | None = None

    @property
    def log_dir(self) -> Path:
        return self.settings.cfg.runs_dir / LOG_DIR

    def current(self) -> Job | None:
        """The running build, if any."""
        with self._lock:
            job = self._jobs.get(self._latest) if self._latest else None
            return job if job is not None and job.running else None

    def job_for(self, run_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(run_id)

    def start(self, source: Path, run_id: str, no_llm: bool = False) -> Job:
        if not valid_run_id(run_id):
            raise ValueError(f"{run_id!r} is not a valid run id")
        with self._lock:
            running = self._jobs.get(self._latest) if self._latest else None
            if running is not None and running.running:
                raise BuildBusy(running.run_id)
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.log_dir / f"{run_id}.log"
            argv = tuple(self._argv(source, run_id, no_llm))
            with log_path.open("wb") as log:
                proc = self._spawn(  # noqa: S603 - an argv list built here, no shell
                    list(argv), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env=build_env(self.settings), start_new_session=True)
            job = Job(run_id, proc, log_path, datetime.now(UTC), argv=argv)
            self._jobs[run_id] = job
            self._latest = run_id
            return job

    def cancel(self, run_id: str, force: bool = False) -> bool:
        """Ask the build to stop (Ctrl-C, so it cleans up); ``force`` kills it at once.
        False if there is no running build with that id."""
        job = self.job_for(run_id)
        if job is None or not job.running:
            return False
        if job.cancel_requested_at is None:
            job.cancel_requested_at = datetime.now(UTC)
        self._signal(job, signal.SIGKILL if force else signal.SIGINT)
        return True

    @staticmethod
    def _signal(job: Job, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):  # it may have just exited
            os.killpg(job.proc.pid, sig)

    def shutdown(self, wait_s: float | None = None) -> None:
        """The server is stopping: cancel a running build and give it time to clean up."""
        job = self.current()
        if job is None:
            return
        self.cancel(job.run_id)
        deadline = time.monotonic() + (self.shutdown_wait_s if wait_s is None else wait_s)
        while job.running and time.monotonic() < deadline:
            time.sleep(0.2)
        if job.running:
            self.cancel(job.run_id, force=True)

    def log_tail(self, run_id: str, limit: int = 8000) -> str | None:
        """The end of the build's console output, or None if this UI never started ``run_id``."""
        if not valid_run_id(run_id):
            return None
        path = self.log_dir / f"{run_id}.log"
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", errors="replace")
