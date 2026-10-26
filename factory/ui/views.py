"""Turning run summaries into what a template shows. Pure functions, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from factory.entities import EntitiesDoc
from factory.report.models import CheckResult, RunRecord, StationResult
from factory.report.runs import RunSummary

# CSS class per run status (see ui.css); anything else is shown neutrally.
STATUS_CLASSES = {
    "passed": "ok", "running": "run", "failed": "bad", "error": "bad",
    "budget_exhausted": "warn", "cancelled": "warn",
}
# Seconds between page refreshes while something is running.
REFRESH_S = 4


def fmt_cost(cost: float | None) -> str:
    return "n/a" if cost is None else f"${cost:.2f}"


def fmt_time(value: datetime | None) -> str:
    return "—" if value is None else value.astimezone().strftime("%Y-%m-%d %H:%M")


def fmt_duration(start: datetime | None, end: datetime | None, now: datetime) -> str:
    if start is None:
        return "—"
    seconds = int(((end or now) - start).total_seconds())
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 90 * 60:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


@dataclass(frozen=True)
class RunRow:
    run_id: str
    title: str
    kind: str
    status: str
    status_class: str
    detail: str
    started: str
    duration: str
    iterations: int
    cost: str
    stale: bool
    readable: bool


def run_row(summary: RunSummary, now: datetime | None = None) -> RunRow:
    current = now or datetime.now(UTC)
    if not summary.readable:
        return RunRow(summary.run_id, summary.run_id, "?", "unreadable", "bad", summary.error,
                      fmt_time(summary.updated_at), "—", 0, "n/a", False, False)
    stale = summary.stale(current)
    if summary.running:
        detail = f"at {summary.current_station}" if summary.current_station else "starting"
        if stale:
            detail += " — no update for a long time; it may have crashed"
    else:
        detail = summary.status_reason
    return RunRow(
        run_id=summary.run_id, title=summary.spec_name or summary.run_id,
        kind="entities" if summary.profile == "entities" else "OpenAPI",
        status="stalled" if stale else summary.status,
        status_class="warn" if stale else STATUS_CLASSES.get(summary.status, ""),
        detail=detail, started=fmt_time(summary.started_at),
        duration=fmt_duration(summary.started_at, summary.ended_at, current),
        iterations=summary.iterations, cost=fmt_cost(summary.cost_usd), stale=stale, readable=True,
    )


def check_chip(check: CheckResult) -> tuple[str, str]:
    """(css class, label) for one check result."""
    if check.skipped:
        return "na", "skipped"
    return ("ok", "pass") if check.passed else ("bad", "fail")


def station_chip(station: StationResult, run_active: bool,
                 run_stalled: bool = False) -> tuple[str, str]:
    """(css class, label) for a station; the unfinished one of an active run is 'running'
    (a station is recorded as passed until it ends, so `ended_at` is what tells)."""
    if station.ended_at is None:
        if run_active:
            return "run", "running"
        if run_stalled:
            return "warn", "stalled"
    return {"passed": ("ok", "passed"), "failed": ("bad", "failed"), "error": ("bad", "error"),
            "skipped": ("na", "skipped")}.get(station.status, ("", station.status))


def budget_bars(record: RunRecord) -> list[tuple[str, float, float, str]]:
    """(label, used, limit, text) for the cost and iteration budgets that apply to the run."""
    config = record.config
    bars = []
    coder_iterations = sum(1 for it in record.iterations if it.agent == "coder")
    if isinstance(config.get("max_iters"), int) and config["max_iters"] > 0:
        limit = float(config["max_iters"])
        bars.append(("Coder iterations", float(coder_iterations), limit,
                     f"{coder_iterations} of {int(limit)}"))
    cost, max_cost = record.usage.cost_usd, config.get("max_cost_usd_per_run")
    if cost is not None and isinstance(max_cost, int | float) and max_cost > 0:
        bars.append(("Cost", float(cost), float(max_cost), f"{fmt_cost(cost)} of "
                     f"{fmt_cost(float(max_cost))}"))
    return bars


@dataclass(frozen=True)
class EntitySummary:
    name: str
    owner: str
    parent: str
    fields: list[str]
    rules: list[str]  # what each rule says, with the operations it applies to
    guards: list[str]
    actions: list[str]


@dataclass(frozen=True)
class DraftSummary:
    app: str
    title: str
    entities: list[EntitySummary]
    gaps: list[str]
    llm_items: int


def draft_summary(doc: EntitiesDoc) -> DraftSummary:
    """What the reviewer must read, in plain words: every entity, owner, rule, guard and gap."""
    entities = []
    for name, spec in doc.entities.items():
        owner = "the signed-in user" if spec.owner == "user" else (
            "its parent's owner" if spec.parent else "nobody (shared)")
        entities.append(EntitySummary(
            name=name, owner=owner, parent=spec.parent or "",
            fields=[f"{field} ({definition.type})" for field, definition in spec.fields.items()],
            rules=[f"{r.text} — on {', '.join(r.on)}, rejects with {r.status}"
                   for r in spec.rules],
            guards=[f"{g.when} — on {g.on}, rejects with {g.status}" for g in spec.guards],
            actions=[f"{action}: {a.text or 'no description'}"
                     for action, a in spec.actions.items()]))
    return DraftSummary(doc.app, doc.display_title, entities, list(doc.gaps), doc.llm_items())


def refresh_seconds(summaries: list[RunSummary]) -> int | None:
    """How often a page that lists these runs should reload, or None when nothing is running."""
    return REFRESH_S if any(s.running and not s.stale() for s in summaries) else None
