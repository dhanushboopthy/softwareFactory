from pathlib import Path

from factory.report.models import (
    CheckResult,
    FileChange,
    IncrementalInfo,
    Iteration,
    RunRecord,
    StationResult,
    Usage,
    Violation,
    now,
)
from factory.report.writer import load_record, render_markdown, write_report


def record() -> RunRecord:
    r = RunRecord(run_id="r1", spec_name="orders.yaml", spec_path="/x", spec_sha256="ab" * 32,
                  provider="anthropic", model="claude-opus-5-5", status="passed")
    r.stations.append(StationResult(name="lint", status="passed"))
    v = Violation(rule="read_only", agent="coder", path="tests/test_orders.py",
                  detail="read-only | blocked", iteration=1)
    r.violations.append(v)
    r.iterations.append(Iteration(
        agent="coder", index=1, tool_calls=3, files_changed=["app/db.py"], lines_added=10,
        lines_removed=2, usage=Usage(llm_calls=2, input_tokens=1000, output_tokens=200,
                                     cost_usd=0.008),
        checks=[CheckResult(name="ruff", passed=True, summary="clean"),
                CheckResult(name="pytest", passed=False, summary="1 failed",
                            failures=["FAILURE test_total"]),
                CheckResult(name="schemathesis", passed=False, skipped=True, summary="skip")],
        failure_summary="### pytest: 1 failed", violations=[v]))
    r.usage = Usage(llm_calls=2, input_tokens=1000, output_tokens=200, cost_usd=0.008)
    r.contract = CheckResult(name="contract", passed=True, summary="300 cases, 0 failures",
                             details={"phases": {"fuzzing": "success"}})
    r.incremental = IncrementalInfo(previous_run="r0", spec_changes=["NEW operation X"],
                                    project_diff=[FileChange(path="app/db.py", change="modified",
                                                             lines_added=1)])
    r.ended_at = now()
    return r


def test_markdown_report_contains_the_essentials() -> None:
    md = render_markdown(record())
    assert "**passed**" in md
    row = "| coder | 1 | 3 | 1 | +10/−2 | ✅ | – | – | ❌ 1 | skip | 1000/200 | $0.0080 | 1 |"
    assert row in md
    assert "read-only \\| blocked" in md  # pipes escaped inside tables
    assert "### pytest: 1 failed" in md
    assert "**PASS** — 300 cases, 0 failures" in md
    assert "| fuzzing | success |" in md
    assert "Based on run `r0`." in md
    assert "| `app/db.py` | modified | 1 | 0 |" in md


def test_run_json_round_trips(tmp_path: Path) -> None:
    write_report(record(), tmp_path)
    loaded = load_record(tmp_path)
    assert loaded.iterations[0].usage.cost_usd == 0.008
    assert (tmp_path / "report.md").read_text().startswith("# Factory run `r1`")
