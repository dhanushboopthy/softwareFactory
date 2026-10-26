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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, route_of, table_name
from factory.harness import checks
from factory.harness.check_env import CheckEnv
from factory.report.models import CheckResult
from factory.spec import ApiSpec, Operation
from factory.stations.scaffold import render_cheat_services
from factory.stations.scaffold_entities import render_cheat_rules, rule_status_table

HTTP_CALLS = {"get", "post", "put", "patch", "delete"}


@dataclass
class TestFunction:
    name: str
    calls: list[tuple[str, str]] = field(default_factory=list)  # (method, url template)
    parametrized: bool = False
    rules: list[str] = field(default_factory=list)  # ids from @pytest.mark.rule("<id>")


def _rule_marker(decorator: ast.expr) -> str | None:
    """The id in ``@pytest.mark.rule("<id>")``, else None."""
    if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "rule" and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)):
        return decorator.args[0].value
    return None


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
        fn.rules = [r for d in node.decorator_list if (r := _rule_marker(d)) is not None]
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


def red_check(
    project: Path, cfg: FactoryConfig, log_dir: Path, test_paths: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> CheckResult:
    """The suite must fail against the current (unimplemented or previous) code.

    Under the entities profile ``test_paths`` is the writer's files only: the generated tests
    cannot make a weak writer suite look good (R4)."""
    result = checks.run_pytest(project, cfg, log_dir, name="red_check", env=env,
                               test_paths=test_paths)
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


# --- entities profile (v2) ---------------------------------------------------------------------


def assess_tests_v2(files: Mapping[str, str], doc: EntitiesDoc) -> CheckResult:
    """Static gate for the test writer under the entities profile (R4, R11).

    Only what the LLM is responsible for is required: every `rules:` id must be named by a
    ``@pytest.mark.rule("<id>")`` marker on some test, and every action must be called. CRUD,
    ownership, guards and transitions are covered by the generated tests. Failures are grouped by
    entity, taken from entities.yaml and not from URL path segments.
    """
    failures: list[str] = []
    tests: list[TestFunction] = []
    for path, source in sorted(files.items()):
        try:
            tests.extend(extract_tests(source))
        except SyntaxError as exc:
            failures.append(f"{path}: syntax error: {exc}")
    if not tests:
        failures.append("no test functions found in tests/test_*.py")

    marked = {rule_id for t in tests for rule_id in t.rules}
    for unknown in sorted(marked - set(rule_status_table(doc))):
        failures.append(f'@pytest.mark.rule("{unknown}") names a rule id that is not in '
                        f"entities.yaml (known: {', '.join(sorted(rule_status_table(doc)))})")
    for entity in doc.ordered():
        spec = doc.entity(entity)
        for rule in spec.rules:
            if rule.id not in marked:
                failures.append(f'{entity}: no test carries @pytest.mark.rule("{rule.id}") '
                                f"({rule.text})")
        route = route_of(doc, entity)
        for action in spec.actions:
            pattern = path_regex(f"{route.item}/{action}")
            if not any(m == "post" and pattern.match(u) for t in tests for m, u in t.calls):
                failures.append(f"{entity}: no test calls POST {route.item}/{action}")

    summary = f"{len(tests)} tests; " + ("ok" if not failures else f"{len(failures)} problem(s)")
    return CheckResult(name="test_quality", passed=not failures, summary=summary,
                       failures=failures,
                       details={"tests": len(tests), "rules_covered": len(marked)})


_PROBE_IGNORE = shutil.ignore_patterns(
    ".venv", "client", ".factory", "__pycache__", "*.db", ".hypothesis", ".mypy_cache",
    ".ruff_cache", ".schemathesis")

_CHEAT_ADVICE = {
    "never": ("every test passed against rules that never reject anything; add tests in which a "
              "rule must reject (assert the status and the `rule` id in the response body)"),
    "always": ("every test passed against rules that reject everything a rule can reject; add "
               "tests in which valid input must be accepted and an action must succeed"),
}


def cheat_probes_v2(
    project: Path, doc: EntitiesDoc, cfg: FactoryConfig, log_dir: Path, check_env: CheckEnv,
    test_paths: Sequence[str],
) -> list[CheckResult]:
    """Run the writer's tests against two fake ``app/rules`` (R13); both must make them fail.

    "never": every hook accepts; "always": every hook that has a rule rejects with it. A suite that
    only checks the reject side passes the second, one that only checks the accept side the first.
    """
    results = []
    for mode in ("never", "always"):
        probe = log_dir / f"cheat-{mode}-project"
        if probe.exists():
            shutil.rmtree(probe)
        shutil.copytree(project, probe, ignore=_PROBE_IGNORE)
        for entity in doc.ordered():
            target = probe / "app" / "rules" / f"{table_name(entity)}.py"
            target.write_text(render_cheat_rules(doc, entity, mode))
        result = checks.run_pytest(
            probe, cfg, log_dir, name=f"cheat_{mode}_reject",
            python=project / ".venv" / "bin" / "python",
            env=check_env.fresh_env(f"cheat_{mode}"), test_paths=test_paths)
        if "tests" not in result.details:  # pytest did not run the suite: inconclusive, not caught
            results.append(result.model_copy(update={"passed": False}))
            continue
        failed = int(result.details.get("failures", 0)) + int(result.details.get("errors", 0))
        caught = failed > 0
        results.append(result.model_copy(update={
            "passed": caught,
            "summary": f"rules that {mode} reject: {result.details.get('passed', 0)} passed, "
                       f"{failed} failed → " + ("caught" if caught else "NOT caught"),
            "failures": [] if caught else [_CHEAT_ADVICE[mode]],
        }))
    return results
