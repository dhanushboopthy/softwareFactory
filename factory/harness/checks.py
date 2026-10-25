"""Verification the model cannot fake: wrappers around ruff, mypy, bandit, pytest and Schemathesis.

Each wrapper runs a real command through the sandbox, keeps the full log on disk, and returns a
CheckResult with parsed, individually addressable failures. Parsers are pure functions so they can
be unit-tested against captured tool output.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from factory.config import FactoryConfig
from factory.harness import sandbox
from factory.report.models import CheckResult

CHECK_ORDER = ("ruff", "mypy", "bandit", "pytest", "schemathesis")
# Feedback priority: behaviour first, then contract, then types, then style/security.
FEEDBACK_PRIORITY = ("pytest", "schemathesis", "mypy", "ruff", "bandit", "pytest_collect")


def _tool(name: str) -> str:
    return str(Path(sys.executable).parent / name)


def _venv_python(project: Path) -> str:
    return str(project / ".venv" / "bin" / "python")


def _save_log(log_dir: Path, name: str, res: sandbox.CommandResult) -> str:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{name}.log"
    header = f"$ {' '.join(res.cmd)}\n[exit {res.returncode}, {res.duration_s}s]"
    path.write_text(f"{header}\n\n{res.output}\n")
    return str(path)


def _result(
    name: str, res: sandbox.CommandResult, log_dir: Path, passed: bool, summary: str,
    failures: list[str],
) -> CheckResult:
    if res.timed_out:
        passed = False
        summary = f"timed out after {res.duration_s}s"
        failures = failures or [f"{name} timed out"]
    return CheckResult(
        name=name,
        passed=passed,
        summary=summary,
        failures=failures,
        duration_s=res.duration_s,
        returncode=res.returncode,
        timed_out=res.timed_out,
        log_path=_save_log(log_dir, name, res),
    )


# --- ruff ------------------------------------------------------------------------------------


def parse_ruff_json(stdout: str) -> list[str]:
    try:
        items = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return [stdout.strip()[:2000]] if stdout.strip() else []
    out = []
    for it in items:
        loc = it.get("location") or {}
        out.append(
            f"{it.get('filename', '?')}:{loc.get('row', '?')}:{loc.get('column', '?')} "
            f"{it.get('code', '')} {it.get('message', '')}"
        )
    return out


def run_ruff(project: Path, cfg: FactoryConfig, log_dir: Path) -> CheckResult:
    res = sandbox.run(
        [_tool("ruff"), "check", "app", "--output-format", "json", "--no-cache"],
        cwd=project, timeout_s=cfg.command_timeout_s,
    )
    failures = [f.replace(str(project) + "/", "") for f in parse_ruff_json(res.stdout)]
    if res.returncode not in (0, 1) and not failures:
        failures = [res.output[-2000:]]
    passed = res.returncode == 0
    return _result("ruff", res, log_dir, passed,
                   "clean" if passed else f"{len(failures)} issue(s)", failures)


# --- mypy ------------------------------------------------------------------------------------

_MYPY_LINE = re.compile(r"^(?P<loc>[^:\s]+:\d+(?::\d+)?): (?P<sev>error|note): (?P<msg>.*)$")


def parse_mypy(output: str) -> list[str]:
    errors: list[str] = []
    for line in output.splitlines():
        m = _MYPY_LINE.match(line.strip())
        if not m:
            continue
        if m["sev"] == "error":
            errors.append(f"{m['loc']}: {m['msg']}")
        elif errors:  # attach notes to the preceding error
            errors[-1] += f"\n    note: {m['msg']}"
    return errors


def run_mypy(project: Path, cfg: FactoryConfig, log_dir: Path) -> CheckResult:
    res = sandbox.run(
        [_venv_python(project), "-m", "mypy", "--show-error-codes", "--no-color-output",
         "--no-error-summary", "--cache-dir", "/dev/null"],
        cwd=project, timeout_s=cfg.command_timeout_s,
    )
    failures = parse_mypy(res.output)
    if res.returncode not in (0, 1) and not failures:
        failures = [res.output[-2000:]]
    passed = res.returncode == 0
    return _result("mypy", res, log_dir, passed,
                   "clean" if passed else f"{len(failures)} error(s)", failures)


# --- bandit ----------------------------------------------------------------------------------


def parse_bandit_json(stdout: str) -> list[str]:
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return [stdout.strip()[:2000]] if stdout.strip() else []
    return [
        f"{r.get('filename')}:{r.get('line_number')} [{r.get('test_id')} "
        f"{r.get('issue_severity')}/{r.get('issue_confidence')}] {r.get('issue_text')}"
        for r in data.get("results", [])
    ]


def run_bandit(project: Path, cfg: FactoryConfig, log_dir: Path) -> CheckResult:
    res = sandbox.run(
        [_tool("bandit"), "-r", "app", "-f", "json", "-q", "--severity-level", "medium",
         "-x", "app/models.py"],
        cwd=project, timeout_s=cfg.command_timeout_s,
    )
    failures = [f.replace(str(project) + "/", "") for f in parse_bandit_json(res.stdout)]
    passed = res.returncode == 0 and not failures
    if not passed and not failures:
        failures = [res.output[-2000:]]
    return _result("bandit", res, log_dir, passed,
                   "clean" if passed else f"{len(failures)} issue(s)", failures)


# --- pytest ----------------------------------------------------------------------------------


def parse_junit(xml_text: str) -> tuple[dict[str, int], list[str]]:
    """Return ({tests, failures, errors, skipped, passed}, [failure descriptions])."""
    root = ET.fromstring(xml_text)  # noqa: S314 - junit written by our own pytest run
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failures: list[str] = []
    for case in root.iter("testcase"):
        counts["tests"] += 1
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is not None:
                counts["failures" if kind == "failure" else "errors"] += 1
                text = (node.text or "").strip()
                msg = node.get("message", "")
                failures.append(f"{kind.upper()} {name}: {msg}\n{text[-1500:]}")
        if case.find("skipped") is not None:
            counts["skipped"] += 1
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return counts, failures


def run_pytest(
    project: Path, cfg: FactoryConfig, log_dir: Path, name: str = "pytest",
    extra_args: Iterable[str] = (), python: Path | None = None,
) -> CheckResult:
    """Run the suite in ``project``; ``python`` lets a copied project reuse the original venv."""
    junit = log_dir / f"{name}.junit.xml"
    log_dir.mkdir(parents=True, exist_ok=True)
    junit.unlink(missing_ok=True)
    interpreter = str(python) if python else _venv_python(project)
    res = sandbox.run(
        [interpreter, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider",
         f"--junitxml={junit}", *extra_args],
        cwd=project, timeout_s=cfg.command_timeout_s,
    )
    if res.returncode == 5:
        return _result(name, res, log_dir, False, "no tests collected", ["no tests collected"])
    if not junit.exists():
        return _result(name, res, log_dir, False, f"pytest crashed (exit {res.returncode})",
                       [res.output[-3000:]])
    counts, failures = parse_junit(junit.read_text())
    if counts["skipped"]:
        failures.append(f"{counts['skipped']} test(s) skipped; skipping is not allowed")
    passed = res.returncode == 0 and not failures and counts["tests"] > 0
    if res.returncode not in (0, 1) and not failures:  # collection/internal error
        failures = [res.output[-3000:]]
    summary = (f"{counts['passed']} passed, {counts['failures']} failed, "
               f"{counts['errors']} errors, {counts['skipped']} skipped")
    result = _result(name, res, log_dir, passed, summary, failures)
    result.details = counts
    return result


def collect_tests(project: Path, cfg: FactoryConfig, log_dir: Path) -> CheckResult:
    """Collection only: proves tests import and parse, without running them."""
    res = sandbox.run(
        [_venv_python(project), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=project, timeout_s=cfg.command_timeout_s,
    )
    ids = [ln for ln in res.stdout.splitlines() if "::" in ln]
    passed = res.returncode == 0 and bool(ids)
    failures = [] if passed else [res.output[-3000:]]
    result = _result("pytest_collect", res, log_dir, passed, f"{len(ids)} test(s) collected",
                     failures)
    result.details = {"collected": len(ids)}
    return result


# --- schemathesis ----------------------------------------------------------------------------


def parse_schemathesis_text(output: str) -> list[str]:
    """Split the FAILURES section of `st run` output into one entry per operation block."""
    start = output.find(" FAILURES ")
    end = output.find(" SUMMARY ")
    if start == -1:
        return []
    section = output[start:end if end != -1 else None]
    blocks = re.split(r"\n_{5,} (.+?) _{5,}\n", section)
    out = []
    # re.split with a group yields [prefix, title1, body1, title2, body2, ...]
    for title, body in zip(blocks[1::2], blocks[2::2], strict=False):
        body = re.sub(r"\n\s*st replay \S+", "", body).strip()
        out.append(f"{title.strip()}\n{body[:1500]}")
    return out


def summarise_schemathesis_json(data: dict[str, Any]) -> tuple[str, dict[str, object]]:
    tc: dict[str, Any] = data.get("test_cases") or {}
    failures: list[Any] = data.get("failures") or []
    errors: list[Any] = data.get("errors") or []
    phases: dict[str, Any] = data.get("phases") or {}
    by_type = {str(f.get("title")): int(f.get("count", 0))
               for f in failures if isinstance(f, dict)}
    details: dict[str, object] = {
        "generated": tc.get("generated", 0),
        "unique_failures": tc.get("unique_failures", 0),
        "failures_by_type": by_type,
        "errors": len(errors),
        "phases": {k: v.get("status") for k, v in phases.items() if isinstance(v, dict)},
    }
    summary = (f"{details['generated']} cases, {details['unique_failures']} unique failures, "
               f"{details['errors']} errors")
    return summary, details


def run_schemathesis(
    project: Path, cfg: FactoryConfig, log_dir: Path, name: str = "schemathesis",
) -> CheckResult:
    report_dir = log_dir / f"{name}-report"
    db = log_dir / f"{name}.db"
    db.unlink(missing_ok=True)
    try:
        with sandbox.serve(
            project / ".venv/bin/python", project, log_dir / f"{name}-server.log",
            env={"DATABASE_URL": f"sqlite:///{db}"},
            start_timeout_s=cfg.server_start_timeout_s,
        ) as server:
            # Run from a per-run working dir so Hypothesis/Schemathesis caches stay out of the
            # project; sharing it across iterations lets known failures replay first.
            st_home = log_dir.parent / ".schemathesis-home"
            st_home.mkdir(parents=True, exist_ok=True)
            res = sandbox.run(
                [_tool("st"), "run", str(project / "openapi.yaml"), "--url", server.base_url,
                 "--checks", cfg.schemathesis_checks,
                 "--max-examples", str(cfg.schemathesis_max_examples),
                 "--seed", str(cfg.schemathesis_seed),
                 "--workers", "1",
                 "--report", "json", "--report-json-path", str(report_dir / "report.json"),
                 "--no-color"],
                cwd=st_home, timeout_s=cfg.command_timeout_s * 3,
            )
    except (RuntimeError, TimeoutError) as exc:
        server_log = log_dir / f"{name}-server.log"
        tail = server_log.read_text()[-3000:] if server_log.exists() else ""
        return CheckResult(name=name, passed=False, summary="server failed to start",
                           failures=[f"{exc}\n{tail}"])
    failures = parse_schemathesis_text(res.stdout)
    report = report_dir / "report.json"
    details: dict[str, object] = {}
    summary = f"exit {res.returncode}"
    if report.exists():
        summary, details = summarise_schemathesis_json(json.loads(report.read_text()))
    passed = res.returncode == 0
    if not passed and not failures:
        failures = [res.output[-3000:]]
    result = _result(name, res, log_dir, passed, summary, failures)
    result.details = details
    return result


# --- aggregation -----------------------------------------------------------------------------


def run_all(project: Path, cfg: FactoryConfig, log_dir: Path) -> list[CheckResult]:
    """Static checks + tests; Schemathesis only runs once the behaviour tests pass (it is slow
    and its findings are noise while basic behaviour is still broken)."""
    results = [run_ruff(project, cfg, log_dir), run_mypy(project, cfg, log_dir),
               run_bandit(project, cfg, log_dir)]
    pytest_result = run_pytest(project, cfg, log_dir)
    results.append(pytest_result)
    if pytest_result.passed:
        results.append(run_schemathesis(project, cfg, log_dir))
    else:
        results.append(CheckResult(name="schemathesis", passed=False, skipped=True,
                                   summary="skipped until pytest passes"))
    return results


def summarize_failures(results: Iterable[CheckResult], max_chars: int) -> str:
    """Most important failures first, truncated to a budget the next prompt can afford."""
    failing = [r for r in results if not r.passed and not r.skipped]
    failing.sort(key=lambda r: FEEDBACK_PRIORITY.index(r.name)
                 if r.name in FEEDBACK_PRIORITY else len(FEEDBACK_PRIORITY))
    if not failing:
        return ""
    per_check = max(500, max_chars // len(failing))
    parts = []
    for r in failing:
        header = f"### {r.name}: {r.summary}"
        body, used = [], len(header)
        for i, f in enumerate(r.failures):
            if used + len(f) > per_check:
                body.append(f"... and {len(r.failures) - i} more (truncated)")
                break
            body.append(f)
            used += len(f)
        parts.append("\n".join([header, *body]))
    text = "\n\n".join(parts)
    return text if len(text) <= max_chars else text[: max_chars - 15] + "\n...(truncated)"
