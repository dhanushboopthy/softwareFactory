"""Anti-cheating gates for the test writer's suite.

1. Static quality (AST): every operation exercised, a stateful create→read-back test per
   resource, and at least one test with varied inputs.
2. Red check: the suite must fail against the unimplemented stubs.
3. Cheat probe: the suite must fail against a deliberately hardcoded implementation that returns
   the spec's response examples. A suite that passes against it cannot tell real from fake.
"""

from __future__ import annotations

import ast
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from factory.config import FactoryConfig
from factory.harness import checks
from factory.report.models import CheckResult
from factory.spec import ApiSpec, Operation
from factory.stations.scaffold import render_cheat_services

HTTP_CALLS = {"get", "post", "put", "patch", "delete"}


@dataclass
class TestFunction:
    name: str
    calls: list[tuple[str, str]] = field(default_factory=list)  # (method, url template)
    parametrized: bool = False


def _url_template(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{x}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _url_template(node.left), _url_template(node.right)
        return (left or "{x}") + (right or "{x}")
    return None


def extract_tests(source: str) -> list[TestFunction]:
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test"):
            continue
        fn = TestFunction(name=node.name)
        fn.parametrized = any("parametrize" in ast.unparse(d) for d in node.decorator_list)
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in HTTP_CALLS and sub.args):
                url = _url_template(sub.args[0])
                if url is not None:
                    fn.calls.append((sub.func.attr, url.split("?", 1)[0]))
        out.append(fn)
    return out


def path_regex(path: str) -> re.Pattern[str]:
    parts = re.split(r"\{[^}]+\}", path)
    return re.compile("^" + "[^/]+".join(re.escape(p) for p in parts) + "/?$")


def _hits(op: Operation, fn: TestFunction) -> bool:
    rx = path_regex(op.path)
    return any(m == op.method and rx.match(u) for m, u in fn.calls)


def assess_tests(files: Mapping[str, str], spec: ApiSpec) -> CheckResult:
    failures: list[str] = []
    tests: list[TestFunction] = []
    for path, source in sorted(files.items()):
        try:
            tests.extend(extract_tests(source))
        except SyntaxError as exc:
            failures.append(f"{path}: syntax error: {exc}")
    if not tests:
        failures.append("no test functions found in tests/test_*.py")

    for op in spec.operations:
        if not any(_hits(op, t) for t in tests):
            failures.append(f"no test calls {op.label} ({op.operation_id})")

    # Stateful: per resource with a create (POST) and a read (GET), one test must do both.
    resources: dict[str, list[Operation]] = {}
    for op in spec.operations:
        resources.setdefault(op.path.strip("/").split("/")[0], []).append(op)
    for resource, ops in sorted(resources.items()):
        creates = [o for o in ops if o.method == "post"]
        reads = [o for o in ops if o.method == "get"]
        if not creates or not reads:
            continue
        stateful = any(
            any(_hits(c, t) for c in creates) and any(_hits(r, t) for r in reads) for t in tests
        )
        if not stateful:
            failures.append(
                f"resource /{resource}: no stateful test that creates (POST) and reads back (GET) "
                "in the same test"
            )

    varied = any(
        t.parametrized or sum(1 for m, _ in t.calls if m in ("post", "put", "patch")) >= 2
        for t in tests
    )
    if tests and not varied:
        failures.append("no test with varied inputs (parametrize, or several writes in one test)")

    summary = f"{len(tests)} tests; " + ("ok" if not failures else f"{len(failures)} problem(s)")
    return CheckResult(name="test_quality", passed=not failures, summary=summary,
                       failures=failures, details={"tests": len(tests)})


def red_check(project: Path, cfg: FactoryConfig, log_dir: Path) -> CheckResult:
    """The suite must fail against the current (unimplemented or previous) code."""
    result = checks.run_pytest(project, cfg, log_dir, name="red_check")
    failed = int(result.details.get("failures", 0)) + int(result.details.get("errors", 0))
    caught = failed > 0
    verdict = "fails as expected" if caught else "PASSES, so it cannot detect missing work"
    return result.model_copy(update={
        "passed": caught,
        "summary": f"{result.summary} against current code → {verdict}",
        "failures": [] if caught else ["the suite passes against unimplemented code; add tests "
                                       "that require the real behaviour"],
    })


def cheat_probe(project: Path, spec: ApiSpec, cfg: FactoryConfig, log_dir: Path) -> CheckResult:
    """Run the suite against services that just return the spec's examples."""
    probe = log_dir / "cheat-project"
    if probe.exists():
        shutil.rmtree(probe)
    shutil.copytree(project, probe, ignore=shutil.ignore_patterns(
        ".venv", "client", ".factory", "__pycache__", "*.db"))
    for module in spec.tags:
        target = probe / "app" / "services" / f"{module}.py"
        target.write_text(render_cheat_services(spec, module))
    result = checks.run_pytest(probe, cfg, log_dir, name="cheat_probe",
                               python=project / ".venv" / "bin" / "python")
    failed = int(result.details.get("failures", 0)) + int(result.details.get("errors", 0))
    caught = failed > 0
    passed_n = int(result.details.get("passed", 0))
    return result.model_copy(update={
        "passed": caught,
        "summary": f"hardcoded implementation: {passed_n} passed, {failed} failed → "
                   + ("caught" if caught else "NOT caught"),
        "failures": [] if caught else [
            "every test passed against an implementation that ignores its inputs and returns the "
            "spec's example responses; add stateful tests (create → read back) and tests whose "
            "expected output depends on varied inputs"
        ],
    })
