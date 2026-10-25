"""Write run.json (machine-readable) and report.md (human-readable) for a run."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from factory.report.models import CheckResult, Iteration, RunRecord

_env = Environment(
    loader=PackageLoader("factory", "templates/report"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,  # noqa: S701 - renders Markdown, never HTML
)


def check_cell(it: Iteration, name: str) -> str:
    result = next((c for c in it.checks if c.name == name), None)
    if result is None:
        return "–"
    if result.skipped:
        return "skip"
    if result.passed:
        return "✅"
    return f"❌ {len(result.failures)}"


def fmt_cost(cost: float | None) -> str:
    return "n/a" if cost is None else f"${cost:.4f}"


def duration(record: RunRecord) -> str:
    if record.ended_at is None:
        return "running"
    secs = (record.ended_at - record.started_at).total_seconds()
    return f"{secs:.0f}s" if secs < 120 else f"{secs / 60:.1f} min"


def render_markdown(record: RunRecord) -> str:
    return _env.get_template("report.md.j2").render(
        r=record,
        check_cell=check_cell,
        fmt_cost=fmt_cost,
        duration=duration(record),
        check_names=("ruff", "mypy", "bandit", "pytest", "schemathesis"),
        contract=record.contract or CheckResult(name="contract", passed=False, summary="not run"),
    )


def write_report(record: RunRecord, run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(record.model_dump_json(indent=2))
    (run_dir / "report.md").write_text(render_markdown(record))


def load_record(run_dir: Path) -> RunRecord:
    return RunRecord.model_validate_json((run_dir / "run.json").read_text())
