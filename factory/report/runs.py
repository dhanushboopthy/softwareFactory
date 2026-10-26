"""Reading runs back: safe paths and cheap summaries for a list of runs.

``run.json`` is rewritten after every station and every agent iteration, so anything that watches
a run (the UI, scripts) can just read it. Nothing here writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from factory.report.models import RunRecord
from factory.report.writer import load_record

RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # used with fullmatch: `$` would also
# accept a trailing newline
# A run that says "running" but has not written anything for this long probably died.
STALE_AFTER_S = 15 * 60


def valid_run_id(value: str) -> bool:
    return RUN_ID.fullmatch(value) is not None


def run_dir_for(runs_dir: Path, run_id: str) -> Path:
    """The directory of ``run_id`` under ``runs_dir``; ValueError for anything that is not a
    plain run id or that resolves (through a symlink, say) outside ``runs_dir``."""
    if not valid_run_id(run_id):
        raise ValueError(f"{run_id!r} is not a valid run id")
    root = runs_dir.resolve()
    path = (root / run_id).resolve()
    if path.parent != root:
        raise ValueError(f"{run_id!r} is outside the runs directory")
    return path


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    updated_at: datetime  # when run.json was last written
    readable: bool = True
    error: str = ""  # why it is not readable
    spec_name: str = ""
    profile: str = ""
    status: str = ""
    status_reason: str = ""
    provider: str = ""
    model: str = ""
    started_at: datetime | None = None
    ended_at: datetime | None = None
    current_station: str | None = None
    stations: int = 0
    stations_failed: int = 0
    iterations: int = 0
    cost_usd: float | None = None
    approved: bool | None = None

    @property
    def running(self) -> bool:
        return self.readable and self.status == "running"

    def stale(self, now: datetime | None = None, after_s: int = STALE_AFTER_S) -> bool:
        """A run that claims to be running but has not been written to for ``after_s`` seconds."""
        current = now or datetime.now(UTC)
        return self.running and (current - self.updated_at).total_seconds() > after_s


def summarize(run_id: str, record: RunRecord, updated_at: datetime) -> RunSummary:
    unfinished = next((s for s in record.stations if s.ended_at is None), None)
    last = record.stations[-1] if record.stations else None
    current = (unfinished or last)
    return RunSummary(
        run_id=run_id, updated_at=updated_at, spec_name=record.spec_name, profile=record.profile,
        status=record.status, status_reason=record.status_reason, provider=record.provider,
        model=record.model, started_at=record.started_at, ended_at=record.ended_at,
        current_station=current.name if current else None,
        stations=len(record.stations),
        stations_failed=sum(1 for s in record.stations if s.status in ("failed", "error")),
        iterations=len(record.iterations), cost_usd=record.usage.cost_usd,
        approved=record.approved,
    )


def read_summary(run_dir: Path) -> RunSummary | None:
    """Summary of the run in ``run_dir``; None if it is not a run; an unreadable placeholder if
    its run.json is damaged (so one bad file never hides the other runs)."""
    path = run_dir / "run.json"
    if not path.is_file():
        return None
    updated = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    try:
        return summarize(run_dir.name, load_record(run_dir), updated)
    except (ValidationError, ValueError, OSError) as exc:
        return RunSummary(run_id=run_dir.name, updated_at=updated, readable=False,
                          error=f"{type(exc).__name__}: {str(exc)[:200]}")


def list_summaries(runs_dir: Path) -> list[RunSummary]:
    """Every run under ``runs_dir``, newest first; unreadable ones (an oddity) come last."""
    if not runs_dir.is_dir():
        return []
    found = []
    for child in runs_dir.iterdir():
        if child.is_dir() and valid_run_id(child.name) and not child.is_symlink():
            summary = read_summary(child)
            if summary is not None:
                found.append(summary)
    return sorted(found, key=lambda s: (s.readable, s.started_at or s.updated_at), reverse=True)
