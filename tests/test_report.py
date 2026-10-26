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


def entities_record() -> RunRecord:
    r = record()
    r.profile, r.approved = "entities", True
    r.budgets = {"llm_items": 3.0, "max_iters": 10.0, "max_lines_changed_per_iter": 450.0,
                 "max_cost_usd_per_run": 3.25}
    r.cheat_probes = [
        CheckResult(name="cheat_never_reject", passed=True,
                    summary="rules that never reject: 1 passed, 2 failed → caught"),
        CheckResult(name="cheat_always_reject", passed=False,
                    summary="rules that always reject: 3 passed, 0 failed → NOT caught"),
    ]
    r.deploy = CheckResult(name="deploy", passed=True, log_path="runs/r1/deploy/compose.log",
                           summary="compose project factory-r1 healthy; signed up and read back")
    return r


def test_entities_report_shows_input_budgets_probes_and_deploy() -> None:
    md = render_markdown(entities_record())
    assert "| Input | entities file · approved: yes |" in md
    assert ("| Budgets | 3 rules+actions → 10 iterations · 450 lines/iteration · $3.2500 |" in md)
    assert "## Anti-cheating probes" in md
    assert "| `cheat_never_reject` | caught ✅ |" in md
    assert "| `cheat_always_reject` | NOT caught ❌ |" in md
    assert "## Deploy check (docker compose)" in md
    assert "**PASS** — compose project factory-r1 healthy" in md
    assert "Log: `runs/r1/deploy/compose.log`" in md


def test_a_failed_deploy_check_shows_its_problems() -> None:
    r = entities_record()
    r.deploy = CheckResult(name="deploy", passed=False, summary="docker compose up failed",
                           failures=["failed to solve: RUN uv sync", "app-1 | Traceback"])
    md = render_markdown(r)
    assert "**FAIL** — docker compose up failed" in md
    assert "failed to solve: RUN uv sync" in md and "app-1 | Traceback" in md


def test_an_unapproved_or_unfinished_entities_run_still_renders() -> None:
    r = entities_record()
    r.approved, r.budgets, r.cheat_probes, r.deploy = False, {}, [], None
    md = render_markdown(r)
    assert "approved: NO" in md and "| Budgets |" not in md
    assert "Not run (the test writer was skipped" in md
    assert "Not run (see the `deploy_check` station)." in md


def test_openapi_reports_have_none_of_the_entities_sections() -> None:
    md = render_markdown(record())
    for heading in ("Anti-cheating probes", "Deploy check", "| Input |", "| Budgets |"):
        assert heading not in md


def test_old_run_json_without_the_new_fields_still_loads(tmp_path: Path) -> None:
    import json

    write_report(record(), tmp_path)
    data = json.loads((tmp_path / "run.json").read_text())
    for key in ("profile", "approved", "budgets", "cheat_probes", "deploy"):
        data.pop(key)
    (tmp_path / "run.json").write_text(json.dumps(data))
    loaded = load_record(tmp_path)
    assert (loaded.profile, loaded.approved, loaded.budgets, loaded.deploy) == (
        "openapi", None, {}, None)


def test_the_new_fields_round_trip(tmp_path: Path) -> None:
    write_report(entities_record(), tmp_path)
    loaded = load_record(tmp_path)
    assert loaded.profile == "entities" and loaded.approved is True
    assert loaded.budgets["max_iters"] == 10.0
    assert [p.name for p in loaded.cheat_probes] == ["cheat_never_reject", "cheat_always_reject"]
    assert loaded.deploy is not None and loaded.deploy.passed


def test_run_json_round_trips(tmp_path: Path) -> None:
    write_report(record(), tmp_path)
    loaded = load_record(tmp_path)
    assert loaded.iterations[0].usage.cost_usd == 0.008
    assert (tmp_path / "report.md").read_text().startswith("# Factory run `r1`")
