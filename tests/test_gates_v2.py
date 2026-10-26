"""Gates for the entities profile: static check, red check, cheat probes, context and station."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, load_entities, parse_entities
from factory.harness import checks, sandbox, test_quality
from factory.harness.check_env import CheckEnv
from factory.harness.context import build_coder_context, build_test_writer_context, llm_work
from factory.harness.guardrails import Guardrails
from factory.harness.postgres import PostgresServer
from factory.harness.test_quality import assess_tests_v2, cheat_probes_v2, extract_tests, red_check
from factory.report.models import CheckResult
from factory.stations import test_writer
from factory.stations.scaffold_entities import static_files_v2

from .conftest import ROOT

EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"


def example() -> EntitiesDoc:
    return load_entities(EXAMPLE)


GOOD_SUITE = '''
import pytest

@pytest.mark.rule("project-limit")
def test_limit(client, user_a):
    client.post("/projects", json={"name": "x"}, headers=user_a)

@pytest.mark.rule("already-archived")
def test_archive_twice(client, user_a):
    row = client.post("/projects", json={"name": "x"}, headers=user_a).json()
    client.post(f"/projects/{row['id']}/archive", headers=user_a)
'''


# --- rule markers -------------------------------------------------------------------------------


def test_extract_tests_reads_rule_markers() -> None:
    source = (
        "import pytest\n"
        "@pytest.mark.rule('a')\n"
        "@pytest.mark.rule('b')\n"
        "@pytest.mark.parametrize('n', [1])\n"
        "def test_x(n): pass\n"
        "@pytest.mark.slow\n"
        "def test_y(): pass\n"
        "@pytest.mark.rule(some_variable)\n"
        "def test_z(): pass\n"
    )
    x, y, z = extract_tests(source)
    assert x.rules == ["a", "b"] and x.parametrized
    assert y.rules == [] and z.rules == []


# --- static gate --------------------------------------------------------------------------------


def test_a_suite_naming_every_rule_and_calling_the_action_passes() -> None:
    result = assess_tests_v2({"tests/test_project.py": GOOD_SUITE}, example())
    assert result.passed, result.failures
    assert result.details == {"tests": 2, "rules_covered": 2}


def test_a_trivial_suite_is_rejected_per_entity() -> None:
    result = assess_tests_v2({"tests/test_project.py": "def test_ok():\n    assert True\n"},
                             example())
    assert not result.passed
    assert result.failures == [
        'Project: no test carries @pytest.mark.rule("project-limit") (A user may have at most 20 '
        "non-archived projects; creating the 21st returns 409.)",
        'Project: no test carries @pytest.mark.rule("already-archived") (Archiving a project that '
        "is already archived returns 409.)",
        "Project: no test calls POST /projects/{id}/archive",
    ]


def test_every_failure_class_is_reported() -> None:
    source = (
        "import pytest\n"
        '@pytest.mark.rule("project-limit")\n'
        '@pytest.mark.rule("no-such-rule")\n'
        "def test_a(client):\n"
        "    client.get('/projects')\n"
    )
    failures = assess_tests_v2({"tests/test_project.py": source}, example()).failures
    assert any("no-such-rule" in f and "not in entities.yaml" in f for f in failures)
    assert any('"already-archived"' in f for f in failures)
    assert any("archive" in f and "no test calls" in f for f in failures)
    assert not any('"project-limit")' in f and "no test carries" in f for f in failures)


def test_markers_of_generated_rules_are_known_but_never_required() -> None:
    source = GOOD_SUITE + '''
@pytest.mark.rule("task-status-transition")
def test_t(client): pass
'''
    assert assess_tests_v2({"tests/test_project.py": source}, example()).passed


def test_no_tests_and_syntax_errors() -> None:
    empty = assess_tests_v2({}, example())
    assert "no test functions found in tests/test_*.py" in empty.failures
    broken = assess_tests_v2({"tests/test_project.py": "def test_(:\n"}, example())
    assert "syntax error" in broken.failures[0]


def test_action_calls_are_found_through_fstrings_and_concatenation() -> None:
    for call in ("client.post(f'/projects/{pid}/archive')",
                 "client.post('/projects/' + str(pid) + '/archive')",
                 "client.post(f'/projects/{pid}/archive?x=1')"):
        limit_test = GOOD_SUITE.split('@pytest.mark.rule("already-archived")')[0]
        source = (limit_test + '@pytest.mark.rule("already-archived")\n'
                  f"def test_a(client, pid):\n    {call}\n")
        result = assess_tests_v2({"tests/test_project.py": source}, example())
        assert result.passed, (call, result.failures)


def test_nested_action_urls_match_too() -> None:
    raw = {"approved": True, "app": "x", "entities": {
        "A": {"owner": "user", "fields": {"n": {"type": "str"}}},
        "B": {"parent": "A", "fields": {"n": {"type": "str"}},
              "actions": {"close": {}},
              "rules": [{"id": "r", "text": "t", "on": ["B.close"], "status": 409}]}}}
    source = ('import pytest\n@pytest.mark.rule("r")\n'
              "def test_a(client, a, b):\n    client.post(f'/as/{a}/bs/{b}/close')\n")
    assert assess_tests_v2({"tests/test_b.py": source}, parse_entities(raw)).passed


# --- red check and cheat probes (fake pytest runner) --------------------------------------------


def passing(details: Mapping[str, int]) -> CheckResult:
    return CheckResult(name="x", passed=True, summary="s", details=dict(details))


class FakePytest:
    """Stands in for checks.run_pytest; records arguments and what the probe project contained."""

    def __init__(self, outcomes: list[CheckResult]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def __call__(self, project: Path, cfg: FactoryConfig, log_dir: Path, name: str = "pytest",
                 **kwargs: Any) -> CheckResult:
        rules = project / "app" / "rules" / "project.py"
        self.calls.append({"project": project, "name": name, "kwargs": kwargs,
                           "rules": rules.read_text() if rules.exists() else None})
        return self.outcomes[len(self.calls) - 1].model_copy(update={"name": name})


def make_check_env() -> tuple[CheckEnv, list[str]]:
    statements: list[str] = []

    def sql(statement: str, database: str) -> sandbox.CommandResult:
        statements.append(statement)
        return sandbox.CommandResult((), 0, "", "", 0.0, False)

    server = PostgresServer(port=5544, password="pw", _sql=sql)  # noqa: S106
    return CheckEnv(server, "template_db"), statements


def make_project_copy(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for rel, content in static_files_v2(example()).items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(content)
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").write_text("")
    return project


def test_red_check_runs_only_the_given_tests_in_the_given_env(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakePytest([passing({"tests": 3, "failures": 2, "errors": 0, "passed": 1})])
    monkeypatch.setattr(checks, "run_pytest", fake)
    result = red_check(tmp_path, FactoryConfig(), tmp_path / "logs",
                       test_paths=["tests/test_project.py"], env={"DATABASE_URL": "u"})
    assert result.passed and "fails as expected" in result.summary
    assert fake.calls[0]["kwargs"] == {"env": {"DATABASE_URL": "u"},
                                       "test_paths": ["tests/test_project.py"]}


def test_red_check_rejects_a_suite_that_passes_against_stubs(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakePytest([passing({"tests": 1, "failures": 0, "errors": 0, "passed": 1})])
    monkeypatch.setattr(checks, "run_pytest", fake)
    result = red_check(tmp_path, FactoryConfig(), tmp_path / "logs")
    assert not result.passed and "cannot detect missing work" in result.summary


def run_probes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
               outcomes: list[CheckResult]) -> tuple[list[CheckResult], FakePytest, list[str]]:
    fake = FakePytest(outcomes)
    monkeypatch.setattr(checks, "run_pytest", fake)
    env, statements = make_check_env()
    project = make_project_copy(tmp_path)
    results = cheat_probes_v2(project, example(), FactoryConfig(), tmp_path / "logs", env,
                              ["tests/test_project.py"])
    return results, fake, statements


def test_both_fake_rule_sets_are_run_on_the_writers_tests_only(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failed = passing({"tests": 2, "failures": 1, "errors": 0, "passed": 1})
    results, fake, statements = run_probes(monkeypatch, tmp_path, [failed, failed])
    assert [r.name for r in results] == ["cheat_never_reject", "cheat_always_reject"]
    assert all(r.passed for r in results)
    never, always = fake.calls
    assert "RuleViolation" not in never["rules"] and "return row" in never["rules"]
    assert 'raise RuleViolation("project-limit"' in always["rules"]
    for call in fake.calls:
        assert call["kwargs"]["test_paths"] == ["tests/test_project.py"]
        assert call["kwargs"]["python"].name == "python"
        assert call["kwargs"]["env"]["DATABASE_URL"].startswith("postgresql+psycopg://")
        assert call["project"] != tmp_path / "project"       # a copy, not the real project
    assert len(statements) == 2                              # one cloned database per probe
    assert "pytest" not in (tmp_path / "project" / "app/rules/project.py").read_text()


def test_the_real_project_is_never_modified_by_probes(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    project = make_project_copy(tmp_path)
    before = (project / "app/rules/project.py").read_text()
    failed = passing({"tests": 1, "failures": 1, "errors": 0, "passed": 0})
    monkeypatch.setattr(checks, "run_pytest", FakePytest([failed, failed]))
    env, _ = make_check_env()
    cheat_probes_v2(project, example(), FactoryConfig(), tmp_path / "logs", env, ["t.py"])
    assert (project / "app/rules/project.py").read_text() == before


@pytest.mark.parametrize(("outcomes", "verdicts"), [
    # rejection-only suite: fails against "never", passes against "always"
    (({"tests": 2, "failures": 1, "errors": 0, "passed": 1},
      {"tests": 2, "failures": 0, "errors": 0, "passed": 2}), (True, False)),
    # acceptance-only suite: passes against "never", fails against "always"
    (({"tests": 2, "failures": 0, "errors": 0, "passed": 2},
      {"tests": 2, "failures": 2, "errors": 0, "passed": 0}), (False, True)),
    (({"tests": 1, "failures": 0, "errors": 1, "passed": 0},
      {"tests": 1, "failures": 0, "errors": 1, "passed": 0}), (True, True)),
])
def test_probe_verdicts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                        outcomes: tuple[dict[str, int], dict[str, int]],
                        verdicts: tuple[bool, bool]) -> None:
    results, _, _ = run_probes(monkeypatch, tmp_path, [passing(o) for o in outcomes])
    assert tuple(r.passed for r in results) == verdicts
    never, always = results
    if not never.passed:
        assert "never reject" in never.failures[0] and "NOT caught" in never.summary
    if not always.passed:
        assert "valid input must be accepted" in always.failures[0]


def test_a_probe_that_could_not_run_is_inconclusive_not_caught(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    crashed = CheckResult(name="x", passed=False, summary="pytest crashed", failures=["boom"])
    results, _, _ = run_probes(monkeypatch, tmp_path, [crashed, crashed])
    assert [r.passed for r in results] == [False, False]
    assert results[0].failures == ["boom"]


# --- context ------------------------------------------------------------------------------------


@pytest.fixture
def v2_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    files = static_files_v2(example())
    files["entities.yaml"] = EXAMPLE.read_text()
    files["openapi.yaml"] = "openapi: 3.1.0\n"
    for rel, content in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(content)
    (project / ".venv" / "lib").mkdir(parents=True)
    (project / ".venv" / "lib" / "x.py").write_text("secret = 1\n")
    return project


def test_llm_work_lists_rules_and_actions() -> None:
    rules, actions = llm_work(example())
    assert [r["id"] for r in rules] == ["project-limit", "already-archived"]
    assert rules[0]["on"] == ["Project.create"] and rules[0]["status"] == 409
    assert actions == [{"entity": "Project", "name": "archive", "path": "/projects/{id}/archive",
                        "text": "Marks the project archived."}]


def test_test_writer_v2_context(v2_project: Path) -> None:
    cfg = FactoryConfig().for_profile("entities")
    bundle = build_test_writer_context(
        project=v2_project, cfg=cfg, guard=Guardrails(cfg, v2_project), attempt=2,
        feedback="cheat_always_reject: NOT caught", modules=["Project"], entities=example())
    user = bundle.user
    assert "`project-limit`" in user and "POST /projects/{id}/archive" in user
    assert "cheat_always_reject: NOT caught" in user
    assert "### tests/conftest.py" in user and "### tests/generated/test_generated_task.py" in user
    assert "app/rules/project.py" not in user          # the implementation stays hidden
    assert 'raise NotImplementedError("project.' not in user
    assert "secret = 1" not in user
    assert "pytest.mark.rule" in bundle.system and "tests/test_*.py" in bundle.system
    assert set(bundle.prompt_versions) == {"test_writer_v2.system.j2", "test_writer_v2.user.j2"}


def test_coder_v2_context(v2_project: Path) -> None:
    cfg = FactoryConfig().for_profile("entities")
    bundle = build_coder_context(
        project=v2_project, cfg=cfg, guard=Guardrails(cfg, v2_project), iteration=2,
        last_errors="### pytest: 1 failed", entities=example())
    assert "Iteration 2 of 8" in bundle.user and "### pytest: 1 failed" in bundle.user
    assert "### app/rules/project.py" in bundle.user      # the file the coder edits
    assert "### app/routes/project.py" in bundle.user     # and the code that calls it
    assert "### tests/generated/test_generated_project.py" in bundle.user
    assert "action `Project.archive`" in bundle.user
    assert "app/rules/**" in bundle.system and "DDL" in bundle.system
    assert set(bundle.prompt_versions) == {"coder_v2.system.j2", "coder_v2.user.j2"}


def test_the_openapi_profile_still_uses_the_v1_prompts(v2_project: Path) -> None:
    cfg = FactoryConfig()
    bundle = build_coder_context(project=v2_project, cfg=cfg, guard=Guardrails(cfg, v2_project),
                                 iteration=1, last_errors="")
    assert set(bundle.prompt_versions) == {"coder.system.j2", "coder.user.j2"}


# --- station helpers ----------------------------------------------------------------------------


def no_llm_doc() -> EntitiesDoc:
    return parse_entities({"approved": True, "app": "plain", "entities": {
        "Note": {"owner": "user", "fields": {"body": {"type": "str"}}}}})


def ctx_for(doc: EntitiesDoc | None, **extra: Any) -> Any:
    return SimpleNamespace(entities=doc, spec=SimpleNamespace(tags=["orders"]), **extra)


def test_the_station_is_skipped_when_there_is_nothing_for_the_llm() -> None:
    reason = test_writer.skip_reason(ctx_for(no_llm_doc()))
    assert reason is not None and "nothing to test" in reason
    assert test_writer.skip_reason(ctx_for(example())) is None
    assert test_writer.skip_reason(ctx_for(None)) is None


def test_modules_are_the_entities_with_llm_work() -> None:
    assert test_writer._modules(ctx_for(example())) == ["Project"]
    assert test_writer._modules(ctx_for(None)) == ["orders"]
    raw = {"approved": True, "app": "x", "entities": {
        "A": {"owner": "user", "fields": {"n": {"type": "str"}},
              "rules": [{"id": "r", "text": "t", "on": ["B.create"], "status": 409}]},
        "B": {"parent": "A", "fields": {"n": {"type": "str"}}}}}
    assert test_writer._modules(ctx_for(parse_entities(raw))) == ["B"]  # targeted, not declared


def station_ctx(tmp_path: Path, doc: EntitiesDoc, writer_files: Mapping[str, str]) -> Any:
    project = make_project_copy(tmp_path)
    for rel, content in writer_files.items():
        (project / rel).write_text(content)
    cfg = FactoryConfig().for_profile("entities")
    env, _ = make_check_env()
    return ctx_for(doc, cfg=cfg, guard=Guardrails(cfg, project), project=project,
                   run_dir=tmp_path / "run", check_env=env)


def test_v2_gates_run_in_order_and_use_only_the_writers_files(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def ok(name: str) -> CheckResult:
        return CheckResult(name=name, passed=True, summary="ok")

    def fake_collect(project: Path, cfg: FactoryConfig, log_dir: Path,
                     env: Mapping[str, str] | None = None) -> CheckResult:
        seen["collect_env"] = env
        return ok("pytest_collect")

    def fake_red(project: Path, cfg: FactoryConfig, log_dir: Path,
                 test_paths: Sequence[str] = (), env: Mapping[str, str] | None = None
                 ) -> CheckResult:
        seen["red_paths"] = list(test_paths)
        return ok("red_check")

    def fake_probes(*args: Any) -> list[CheckResult]:
        seen["probe_paths"] = list(args[-1])
        return [ok("cheat_never_reject"), ok("cheat_always_reject")]

    monkeypatch.setattr(checks, "collect_tests", fake_collect)
    monkeypatch.setattr(test_quality, "red_check", fake_red)
    monkeypatch.setattr(test_quality, "cheat_probes_v2", fake_probes)
    ctx = station_ctx(tmp_path, example(), {"tests/test_project.py": GOOD_SUITE})
    gates = test_writer._gates(ctx, "attempt-1")
    assert [g.name for g in gates] == ["pytest_collect", "test_quality", "red_check",
                                       "cheat_never_reject", "cheat_always_reject"]
    assert seen["red_paths"] == seen["probe_paths"] == ["tests/test_project.py"]
    assert "tests/generated" not in " ".join(seen["red_paths"])
    assert seen["collect_env"]["DATABASE_URL"].startswith("postgresql+psycopg://")


def test_a_failed_collection_stops_the_gates(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    bad = CheckResult(name="pytest_collect", passed=False, summary="0 collected")
    monkeypatch.setattr(checks, "collect_tests", lambda *a, **k: bad)
    ctx = station_ctx(tmp_path, example(), {})
    assert test_writer._gates(ctx, "a") == [bad]


def test_without_writer_files_the_probes_are_not_run(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ok = CheckResult(name="pytest_collect", passed=True, summary="ok")
    monkeypatch.setattr(checks, "collect_tests", lambda *a, **k: ok)
    monkeypatch.setattr(checks, "run_pytest", FakePytest([passing({"tests": 0})] * 3))
    monkeypatch.setattr(test_quality, "cheat_probes_v2",
                        lambda *a: pytest.fail("probes must not run"))
    ctx = station_ctx(tmp_path, example(), {})
    names = [g.name for g in test_writer._gates(ctx, "a")]
    assert names == ["pytest_collect", "test_quality", "red_check"]


def test_gates_v2_need_the_entities_document_and_the_database(tmp_path: Path) -> None:
    ctx = station_ctx(tmp_path, example(), {})
    ctx.check_env = None
    with pytest.raises(ValueError, match="ctx.check_env"):
        test_writer._gates(ctx, "a")


def test_the_openapi_profile_still_takes_the_v1_gates(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(test_writer, "_gates_v1", lambda ctx, name: [])
    monkeypatch.setattr(test_writer, "_gates_v2", lambda ctx, name: pytest.fail("not v2"))
    ctx = ctx_for(None, cfg=replace(FactoryConfig(), profile="openapi"))
    assert test_writer._gates(ctx, "a") == []
