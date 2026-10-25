import json

from factory.harness.checks import (
    parse_bandit_json,
    parse_junit,
    parse_mypy,
    parse_ruff_json,
    parse_schemathesis_text,
    summarise_schemathesis_json,
    summarize_failures,
)
from factory.report.models import CheckResult


def test_parse_ruff_json() -> None:
    out = json.dumps([{"filename": "app/x.py", "location": {"row": 3, "column": 5},
                       "code": "F401", "message": "`os` imported but unused"}])
    assert parse_ruff_json(out) == ["app/x.py:3:5 F401 `os` imported but unused"]
    assert parse_ruff_json("") == []
    assert parse_ruff_json("not json") == ["not json"]


def test_parse_mypy_attaches_notes() -> None:
    out = (
        "app/services/orders.py:10: error: Incompatible return value type  [return-value]\n"
        "app/services/orders.py:10: note: Expected \"Order\"\n"
        "app/db.py:3:1: error: Missing type parameters  [type-arg]\n"
        "Found 2 errors in 2 files\n"
    )
    errors = parse_mypy(out)
    assert len(errors) == 2
    assert errors[0].startswith("app/services/orders.py:10: Incompatible return value")
    assert "note: Expected" in errors[0]
    assert errors[1] == "app/db.py:3:1: Missing type parameters  [type-arg]"


def test_parse_bandit_json() -> None:
    out = json.dumps({"results": [{"filename": "app/x.py", "line_number": 4, "test_id": "B608",
                                   "issue_severity": "MEDIUM", "issue_confidence": "LOW",
                                   "issue_text": "Possible SQL injection"}]})
    assert parse_bandit_json(out) == ["app/x.py:4 [B608 MEDIUM/LOW] Possible SQL injection"]


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.test_orders" name="test_ok" time="0.01"/>
  <testcase classname="tests.test_orders" name="test_total" time="0.01">
    <failure message="assert 10.75 == 22.75">tests/test_orders.py:22: AssertionError</failure>
  </testcase>
  <testcase classname="tests.test_orders" name="test_boom" time="0.01">
    <error message="fixture failed">E   RuntimeError</error>
  </testcase>
  <testcase classname="tests.test_orders" name="test_skip" time="0.0">
    <skipped message="later"/>
  </testcase>
</testsuite></testsuites>"""


def test_parse_junit() -> None:
    counts, failures = parse_junit(JUNIT)
    assert counts == {"tests": 4, "failures": 1, "errors": 1, "skipped": 1, "passed": 1}
    assert failures[0].startswith("FAILURE tests.test_orders::test_total: assert 10.75 == 22.75")
    assert failures[1].startswith("ERROR tests.test_orders::test_boom")


ST_OUTPUT = """
=================================== FAILURES ===================================
_________________________________ GET /orders __________________________________
1. Test Case ID: xbfM88

- Server error

[501] Not Implemented:

    `{"detail":"Not implemented: list_orders"}`

Reproduce with:

    curl -X GET 'http://127.0.0.1:54289/orders?status=cancelled'

    st replay xbfM88

_________________________________ POST /orders _________________________________
1. Test Case ID: pVLewx

- API accepted schema-violating request

=================================== SUMMARY ====================================
"""


def test_parse_schemathesis_text() -> None:
    failures = parse_schemathesis_text(ST_OUTPUT)
    assert len(failures) == 2
    assert failures[0].startswith("GET /orders\n1. Test Case ID")
    assert "Not implemented: list_orders" in failures[0]
    assert "st replay" not in failures[0]
    assert failures[1].startswith("POST /orders")
    assert parse_schemathesis_text("all good") == []


def test_summarise_schemathesis_json() -> None:
    summary, details = summarise_schemathesis_json({
        "test_cases": {"generated": 108, "unique_failures": 8},
        "failures": [{"title": "Server error", "count": 3}],
        "errors": [],
        "phases": {"examples": {"status": "failure"}},
    })
    assert summary == "108 cases, 8 unique failures, 0 errors"
    assert details["failures_by_type"] == {"Server error": 3}
    assert details["phases"] == {"examples": "failure"}


def test_summarize_failures_prioritises_behaviour_and_truncates() -> None:
    results = [
        CheckResult(name="ruff", passed=False, summary="1 issue", failures=["E501 long line"]),
        CheckResult(name="pytest", passed=False, summary="1 failed",
                    failures=["FAILURE test_total"] + ["x" * 400] * 20),
        CheckResult(name="mypy", passed=True, summary="clean"),
        CheckResult(name="schemathesis", passed=False, skipped=True, summary="skipped"),
    ]
    text = summarize_failures(results, max_chars=2000)
    assert text.index("### pytest") < text.index("### ruff")
    assert "mypy" not in text and "schemathesis" not in text
    assert "more (truncated)" in text
    assert len(text) <= 2000
    assert summarize_failures([CheckResult(name="ruff", passed=True, summary="")], 100) == ""
