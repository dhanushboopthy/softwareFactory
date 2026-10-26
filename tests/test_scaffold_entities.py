"""The v2 project generator: view model, rendered files, and (opt in) the real migration."""

import importlib.util
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, FieldSpec, Scalar, expand, load_entities, parse_entities
from factory.harness import checks, sandbox
from factory.harness.check_env import CheckEnv
from factory.harness.guardrails import Guardrails, Role, glob_regex
from factory.harness.postgres import postgres_server
from factory.harness.test_quality import assess_tests_v2, cheat_probes_v2, red_check
from factory.report.models import CheckResult
from factory.stations.scaffold import setup_env
from factory.stations.scaffold_entities import (
    ColumnView,
    alt_literal,
    avoid_literals,
    column_for,
    create_payload,
    entity_view,
    guard_cases,
    orm_imports,
    render_cheat_rules,
    rule_status_table,
    sample_value,
    scaffold_entities,
    static_files_v2,
    transition_cases,
    transition_rule_id,
    update_patch,
)

from .conftest import ROOT

EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"


def example() -> EntitiesDoc:
    return load_entities(EXAMPLE)


def write_static(doc: EntitiesDoc, root: Path) -> dict[str, str]:
    files = static_files_v2(doc)
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    return files


# --- view model ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "sa_type", "py_type", "decl"),
    [
        ({"type": "str", "max": 200}, "String(200)", "str", "mapped_column(String(200))"),
        ({"type": "str"}, "Text", "str", "mapped_column(Text)"),
        ({"type": "int"}, "BigInteger", "int", "mapped_column(BigInteger)"),
        ({"type": "float"}, "Double", "float", "mapped_column(Double)"),
        ({"type": "bool", "default": False}, "Boolean", "bool",
         "mapped_column(Boolean, default=False)"),
        ({"type": "date", "optional": True}, "Date", "date",
         "mapped_column(Date, nullable=True)"),
        ({"type": "datetime"}, "DateTime(timezone=True)", "datetime",
         "mapped_column(DateTime(timezone=True))"),
        ({"type": "enum", "values": ["todo", "doing", "done"], "default": "todo"},
         "String(5)", "str", "mapped_column(String(5), default='todo')"),
        ({"type": "int", "optional": True, "default": 3}, "BigInteger", "int",
         "mapped_column(BigInteger, nullable=True, default=3)"),
    ],
)
def test_column_for(spec: dict[str, Any], sa_type: str, py_type: str, decl: str) -> None:
    column = column_for("f", FieldSpec.model_validate(spec))
    assert (column.sa_type, column.py_type, column.decl) == (sa_type, py_type, decl)


def test_migration_column_spelling() -> None:
    column = ColumnView("title", "String(200)", "str", False)
    assert column.migration_column == 'sa.Column("title", sa.String(200), nullable=False)'


def test_foreign_keys_follow_ownership_and_nesting() -> None:
    doc = example()
    project, task = entity_view(doc, "Project"), entity_view(doc, "Task")
    assert [(f.column, f.target_table) for f in project.foreign_keys] == [("owner_id", "users")]
    assert [(f.column, f.target_table) for f in task.foreign_keys] == [("project_id", "project")]
    assert (task.table, task.plural, task.parent, task.owner) == (
        "task", "tasks", "Project", "user")


def test_shared_entities_have_no_owner_column() -> None:
    raw = {"approved": True, "app": "shared", "entities": {
        "Tag": {"owner": "none", "fields": {"label": {"type": "str"}}},
        "Note": {"parent": "Tag", "fields": {"body": {"type": "str"}}}}}
    doc = parse_entities(raw)
    assert entity_view(doc, "Tag").foreign_keys == ()
    assert [f.column for f in entity_view(doc, "Note").foreign_keys] == ["tag_id"]
    assert entity_view(doc, "Note").owner == "none"


def test_orm_imports_are_minimal_and_sorted() -> None:
    doc = example()
    views = [entity_view(doc, n) for n in doc.ordered()]
    assert orm_imports(views) == (["date"], ["Boolean", "Date", "ForeignKey", "Integer", "String"])
    raw = {"approved": True, "app": "x", "entities": {"A": {"owner": "none", "fields": {
        "n": {"type": "int"}, "w": {"type": "datetime"}, "d": {"type": "date"}}}}}
    views = [entity_view(parse_entities(raw), "A")]
    assert orm_imports(views) == (
        ["date", "datetime"], ["BigInteger", "Date", "DateTime", "Integer", "String"])


def test_rule_status_table() -> None:
    assert rule_status_table(example()) == {
        "project-limit": 409,
        "already-archived": 409,
        "archived-project-rejects-tasks": 409,
        "task-status-transition": 409,
    }
    assert transition_rule_id("OrderItem", "ship_state") == "order_item-ship-state-transition"


def test_rule_status_table_rejects_a_colliding_transition_id() -> None:
    raw = {"approved": True, "app": "x", "entities": {"Job": {
        "owner": "user",
        "fields": {"state": {"type": "enum", "values": ["a", "b"], "default": "a",
                             "transitions": {"a": ["b"]}}},
        "rules": [{"id": "job-state-transition", "text": "t", "on": ["Job.create"],
                   "status": 409}]}}}
    with pytest.raises(ValueError, match="used twice"):
        rule_status_table(parse_entities(raw))


# --- rendered files -----------------------------------------------------------------------------


def test_persistence_files_are_generated() -> None:
    files = static_files_v2(example())
    assert {".env.example", "alembic.ini", "pyproject.toml", "app/__init__.py", "app/settings.py",
            "app/db.py", "app/orm.py", "app/errors.py", "migrations/env.py",
            "migrations/script.py.mako", "migrations/versions/0001_initial.py"} <= set(files)


def test_generated_python_compiles() -> None:
    for rel, source in static_files_v2(example()).items():
        if rel.endswith(".py"):
            compile(source, rel, "exec")


def test_orm_matches_the_entities() -> None:
    orm = static_files_v2(example())["app/orm.py"]
    assert 'owner_id: Mapped[int] = mapped_column(\n        Integer, ForeignKey("users.id"' in orm
    assert 'ForeignKey("project.id", ondelete="CASCADE"), index=True' in orm
    assert "name: Mapped[str] = mapped_column(String(200))" in orm
    assert "archived: Mapped[bool] = mapped_column(Boolean, default=False)" in orm
    assert 'status: Mapped[str] = mapped_column(String(5), default="todo")' in orm  # ruff style
    assert "due: Mapped[date | None] = mapped_column(Date, nullable=True)" in orm
    assert "create_all" not in orm


def test_migration_indexes_every_owner_and_parent_key() -> None:
    migration = static_files_v2(example())["migrations/versions/0001_initial.py"]
    assert 'op.create_index("ix_project_owner_id", "project", ["owner_id"])' in migration
    assert 'op.create_index("ix_task_project_id", "task", ["project_id"])' in migration
    assert migration.count("ondelete=") == 2
    down = migration.split("def downgrade")[1]
    assert down.index('"task"') < down.index('"project"') < down.index('"users"')


def test_errors_module_lists_every_declared_rule() -> None:
    errors = static_files_v2(example())["app/errors.py"]
    for rule_id in ("project-limit", "already-archived", "archived-project-rejects-tasks",
                    "task-status-transition"):
        assert f'"{rule_id}": 409' in errors


def test_compose_file_deploys_the_app_with_its_own_database() -> None:
    compose = yaml.safe_load(static_files_v2(example())["compose.yaml"])
    db, app_service = compose["services"]["db"], compose["services"]["app"]
    assert set(compose["services"]) == {"db", "app"}
    assert "ports" not in db                                      # the database is not published
    assert app_service["ports"] == ["127.0.0.1:${APP_PORT:-8000}:8000"]   # app: localhost only
    assert app_service["depends_on"]["db"]["condition"] == "service_healthy"
    assert "healthcheck" in db and "healthcheck" in app_service
    assert db["environment"]["POSTGRES_PASSWORD"].startswith("${POSTGRES_PASSWORD:?")
    assert app_service["environment"]["JWT_SECRET"].startswith("${JWT_SECRET:?")  # no default
    assert "@db:5432" in app_service["environment"]["DATABASE_URL"]
    assert "change-me" not in str(compose)                        # no literal secret anywhere


def test_dockerfile_applies_migrations_then_serves_as_a_normal_user() -> None:
    files = static_files_v2(example())
    dockerfile = files["Dockerfile"]
    assert "alembic upgrade head && uvicorn app.main:app" in dockerfile
    assert "USER app" in dockerfile and "uv sync --no-dev" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    ignored = files[".dockerignore"].split()
    assert {".venv", ".env", ".git", "tests", "client"} <= set(ignored)  # no secrets or local state


def test_env_example_covers_the_compose_variables() -> None:
    env = static_files_v2(example())[".env.example"]
    assert "POSTGRES_PASSWORD=\n" in env and "JWT_SECRET=\n" in env


def test_env_example_has_no_secret() -> None:
    env = static_files_v2(example())[".env.example"]
    assert "JWT_SECRET=\n" in env
    assert "DATABASE_URL=postgresql+psycopg://" in env


def test_pyproject_registers_the_rule_marker_and_postgres_driver() -> None:
    pyproject = static_files_v2(example())["pyproject.toml"]
    assert 'markers = ["rule(id)' in pyproject
    assert '"psycopg[binary]' in pyproject and '"alembic' in pyproject and '"pyjwt' in pyproject
    assert 'name = "tasks"' in pyproject


def test_generation_is_deterministic() -> None:
    assert static_files_v2(example()) == static_files_v2(example())


def test_generated_code_passes_the_projects_ruff_config(tmp_path: Path) -> None:
    write_static(example(), tmp_path)
    ruff = str(Path(sys.executable).parent / "ruff")
    result = sandbox.run([ruff, "check", "app", "--no-cache"], cwd=tmp_path, timeout_s=60)
    assert result.ok, result.output


def load_settings(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("generated_settings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_settings_refuse_to_start_without_a_secret(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_static(example(), tmp_path)
    path = tmp_path / "app" / "settings.py"
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET is not set"):
        load_settings(path)
    monkeypatch.setenv("JWT_SECRET", "")
    with pytest.raises(RuntimeError, match="JWT_SECRET is not set"):
        load_settings(path)
    monkeypatch.setenv("JWT_SECRET", "s3cret")
    monkeypatch.setenv("PASSWORD_HASH_ITERATIONS", "1000")
    settings = load_settings(path)
    assert settings.JWT_SECRET == "s3cret" and settings.PASSWORD_HASH_ITERATIONS == 1000  # noqa: S105
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        load_settings(path)


def test_scaffold_writes_the_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    result = scaffold_entities(example(), EXAMPLE, project, 300)
    assert result.models.ok, result.models.output
    assert (project / "entities.yaml").read_text() == EXAMPLE.read_text()
    assert "operationId: createProject" in (project / "openapi.yaml").read_text()
    assert "class ProjectCreate" in (project / "app" / "models.py").read_text()
    assert "app/models.py" in result.files and "app/orm.py" in result.files
    assert (project / ".factory" / "manifest.json").exists()


# --- T4b-T4d: auth, CRUD, routes, hooks, cheat variants, generated tests -------------------------

EXPECTED_FILES = {
    ".env.example", "AGENTS.md", "alembic.ini", "pyproject.toml", "Dockerfile", "compose.yaml",
    ".dockerignore",
    "migrations/env.py", "migrations/script.py.mako", "migrations/versions/0001_initial.py",
    "app/__init__.py", "app/auth.py", "app/crud/__init__.py", "app/crud/project.py",
    "app/crud/task.py", "app/db.py", "app/errors.py", "app/main.py", "app/orm.py",
    "app/routes/__init__.py", "app/routes/auth.py", "app/routes/project.py",
    "app/routes/task.py", "app/rules/__init__.py", "app/rules/project.py", "app/rules/task.py",
    "app/settings.py", "tests/conftest.py", "tests/generated/test_generated_project.py",
    "tests/generated/test_generated_task.py",
}


def test_exact_file_list() -> None:
    assert set(static_files_v2(example())) == EXPECTED_FILES


def test_the_coder_may_write_only_rules_files() -> None:
    config = FactoryConfig().for_profile("entities")
    for rel in static_files_v2(example()):
        writable = any(glob_regex(p).match(rel) for p in config.coder_writable)
        assert writable == rel.startswith("app/rules/"), rel


def test_rule_stubs_only_raise_where_the_llm_has_work() -> None:
    files = static_files_v2(example())
    project, task = files["app/rules/project.py"], files["app/rules/task.py"]
    assert 'raise NotImplementedError("project.before_create")' in project
    assert 'raise NotImplementedError("project.archive")' in project
    assert "project.before_update" not in project and "project.before_delete" not in project
    assert "NotImplementedError" not in task
    assert task.count("return None") == 3


def test_rule_stubs_document_their_rules_and_signatures() -> None:
    files = static_files_v2(example())
    project, task = files["app/rules/project.py"], files["app/rules/task.py"]
    assert "project-limit (status 409)" in project and "already-archived (status 409)" in project

    def flat(source: str) -> str:  # the formatter may wrap long signatures
        return " ".join(source.split()).replace("( ", "(").replace(" )", ")")

    assert "def before_create(session: Session, user: orm.User, data: models.ProjectCreate)" \
        in flat(project)
    assert "def archive(session: Session, user: orm.User, row: orm.Project) -> orm.Project:" \
        in flat(project)
    assert "data: models.TaskCreate, parent: orm.Project" in flat(task)
    assert "def before_delete(session: Session, user: orm.User, row: orm.Task, " \
        "parent: orm.Project) -> None:" in flat(task)


def test_routes_follow_the_documented_order() -> None:
    task = static_files_v2(example())["app/routes/task.py"]
    create = task.split("def create_task")[1].split("@router")[0]
    assert create.index("parent_crud.get") < create.index("archived is True") \
        < create.index("rules.before_create(session, user, body, parent)") \
        < create.index("crud.create") < create.index("session.commit()")
    update = task.split("def update_task")[1].split("@router")[0]
    assert update.index("crud.get(session, parent, id)") < update.index("_STATUS_MOVES") \
        < update.index("rules.before_update") < update.index("crud.update")
    assert '_STATUS_MOVES: dict[str, tuple[str, ...]] = {"todo": ("doing",), "doing": ("done",)}' \
        in task
    assert '"task-status-transition"' in task and "raise RuleViolation(" in task


def test_action_route_assigns_the_hooks_result() -> None:
    project = static_files_v2(example())["app/routes/project.py"]
    assert '"/projects/{id}/archive"' in project
    assert "row = rules.archive(session, user, row)" in project
    assert "response_model=None" in project and "status_code=204" in project


def test_top_level_routes_scope_by_user_and_nested_by_parent() -> None:
    files = static_files_v2(example())
    assert "crud.get(session, user, id)" in files["app/routes/project.py"]
    assert "orm.Project.owner_id == user.id" in files["app/crud/project.py"]
    assert "orm.Task.project_id == parent.id" in files["app/crud/task.py"]
    assert "owner_id=user.id, " in files["app/crud/project.py"]


def test_list_limits_come_from_the_config() -> None:
    cfg = replace(FactoryConfig(), list_limit_default=5, list_limit_max=10)
    routes = static_files_v2(example(), cfg)["app/routes/project.py"]
    assert "Query(ge=1, le=10)] = 5" in routes
    spec = expand(example(), cfg)
    params = {p["name"]: p["schema"] for p in spec["paths"]["/projects"]["get"]["parameters"]}
    assert params["limit"] == {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
    assert "`limit` (1-10, default 5)" in spec["paths"]["/projects"]["get"]["description"]
    assert "le=100" in static_files_v2(example())["app/routes/project.py"]


def test_main_wires_every_router_and_the_error_handlers() -> None:
    main = static_files_v2(example())["app/main.py"]
    for needle in ("auth_routes.router", "project_routes.router", "task_routes.router",
                   '"/health"', "RuleViolation", "NotImplementedError", "Allow"):
        assert needle in main, needle
    assert "create_all" not in main


def test_auth_module_has_one_current_user_dependency() -> None:
    auth = static_files_v2(example())["app/auth.py"]
    assert "pbkdf2_hmac" in auth and "compare_digest" in auth
    assert "HTTPBearer(auto_error=False)" in auth and "status_code=401" in auth
    assert "CurrentUser = Annotated[orm.User, Depends(current_user)]" in auth
    assert "JWT_SECRET" in auth and "algorithms=[settings.JWT_ALGORITHM]" in auth


def test_project_agents_md_limits_the_coder_to_rules() -> None:
    agents = static_files_v2(example())["AGENTS.md"]
    assert "the only files you write" in agents and "app/rules/*.py" in agents
    assert "RuleViolation" in agents and "entities.yaml" in agents
    assert "back to a human for approval" in agents


def test_conftest_truncates_every_table_and_offers_one_user_helper() -> None:
    conftest = static_files_v2(example())["tests/conftest.py"]
    assert 'TABLES = "users, project, task"' in conftest
    assert "RESTART IDENTITY CASCADE" in conftest and "lock_timeout" in conftest
    assert "def make_user" in conftest and "def user_a" in conftest and "def user_b" in conftest
    assert "create_all" not in conftest


@pytest.mark.parametrize(("spec", "n", "expected"), [
    ({"type": "str", "min": 1, "max": 200}, 2, "sample 2"),
    ({"type": "str", "max": 4}, 1, "samp"),
    ({"type": "str", "min": 12}, 1, "sample 1xxxx"),
    ({"type": "int"}, 3, 3),
    ({"type": "int", "min": 10, "max": 11}, 3, 11),
    ({"type": "float", "min": 1}, 2, 2.0),
    ({"type": "bool"}, 1, False),
    ({"type": "bool"}, 2, True),
    ({"type": "bool", "default": True}, 1, True),
    ({"type": "date"}, 3, "2026-01-03"),
    ({"type": "datetime"}, 2, "2026-01-02T00:00:00Z"),
    ({"type": "enum", "values": ["a", "b"]}, 3, "a"),
    ({"type": "enum", "values": ["a", "b"], "default": "b"}, 1, "b"),
])
def test_sample_value(spec: dict[str, Any], n: int, expected: Scalar) -> None:
    assert sample_value(FieldSpec.model_validate(spec), n) == expected


@pytest.mark.parametrize(("spec", "literal", "expected"), [
    ({"type": "bool"}, True, False),
    ({"type": "enum", "values": ["a", "b", "c"]}, "a", "b"),
    ({"type": "int", "max": 5}, 5, 4),
    ({"type": "int", "min": 0, "max": 5}, 0, 1),
    ({"type": "int", "min": 5, "max": 5}, 5, None),
    ({"type": "str"}, "x", None),
])
def test_alt_literal(spec: dict[str, Any], literal: Scalar, expected: Scalar | None) -> None:
    assert alt_literal(FieldSpec.model_validate(spec), literal) == expected


def test_example_payloads_and_cases() -> None:
    doc = example()
    assert create_payload(doc, "Project", 1) == {"name": "sample 1", "archived": False}
    assert create_payload(doc, "Task", 2) == {"title": "sample 2", "due": "2026-01-02"}  # no status
    assert update_patch(doc, "Project") == {"name": "sample 2"}
    (guard,) = guard_cases(doc, "Task")
    assert (guard.id, guard.op, guard.subject, guard.field, guard.literal, guard.alt) == (
        "archived-project-rejects-tasks", "create", "parent", "archived", True, False)
    (case,) = transition_cases(doc, "Task")
    assert case.start == "todo"
    assert case.moves == (((), "doing"), (("doing",), "done"))
    assert case.bad == (((), "done"), (("doing",), "todo"), (("doing", "done"), "todo"))


def ledger() -> EntitiesDoc:
    return parse_entities(ledger_raw())


def ledger_raw() -> dict[str, Any]:
    """Every template branch: own-field and parent guards on each op, an action, a cyclic state
    machine, an LLM rule on a nested entity, and a shared (owner: none) entity."""
    def guard(gid: str, on: str, when: str, status: int = 409) -> dict[str, Any]:
        return {"id": gid, "on": on, "when": when, "status": status}

    return {"approved": True, "app": "ledger", "entities": {
        "Account": {
            "owner": "user",
            "fields": {"name": {"type": "str", "min": 1, "max": 50},
                       "tier": {"type": "enum", "values": ["free", "pro"], "default": "free"},
                       "credits": {"type": "int", "min": 0, "max": 1000, "default": 5}},
            "guards": [guard("no-jackpot-signup", "Account.create", "credits == 999", 403),
                       guard("frozen-account", "Account.update", "credits == 0"),
                       guard("pro-undeletable", "Account.delete", 'tier == "pro"'),
                       guard("empty-cannot-close", "Account.close", "credits == 0")],
            "actions": {"close": {"text": "Closes the account."}}},
        "Line": {
            "parent": "Account",
            "fields": {"amount": {"type": "int", "min": 1, "max": 100},
                       "note": {"type": "str", "optional": True, "max": 40},
                       "state": {"type": "enum", "values": ["new", "open", "closed"],
                                 "default": "new",
                                 "transitions": {"new": ["open"], "open": ["closed", "new"]}}},
            "guards": [guard("pro-lines-frozen", "Line.update", 'parent.tier == "pro"'),
                       guard("five-credit-lock", "Line.delete", "parent.credits == 5")],
            "rules": [{"id": "big-line", "text": "Lines over 90 need approval.",
                       "on": ["Line.create", "Line.update"], "status": 422}]},
        "Tag": {"owner": "none", "fields": {"label": {"type": "str", "max": 30}}},
    }}


def test_every_template_branch_renders_valid_python(tmp_path: Path) -> None:
    files = write_static(ledger(), tmp_path)
    for rel, source in files.items():
        if rel.endswith(".py"):
            compile(source, rel, "exec")
    ruff = str(Path(sys.executable).parent / "ruff")
    result = sandbox.run([ruff, "check", "app", "--no-cache"], cwd=tmp_path, timeout_s=60)
    assert result.ok, result.output


def test_guard_conditions_per_operation_and_subject() -> None:
    files = static_files_v2(ledger())
    account, line = files["app/routes/account.py"], files["app/routes/line.py"]
    assert "body.credits == 999" in account           # create: the request body
    assert "row.credits == 0" in account              # update / action: the stored row
    assert 'row.tier == "pro"' in account             # delete
    assert 'parent.tier == "pro"' in line and "parent.credits == 5" in line
    assert "def close_account" in account and "row = rules.close(session, user, row)" in account
    assert "_STATE_MOVES" in line and '"open": ("closed", "new")' in line
    tag_crud = files["app/crud/tag.py"]
    assert "owner_id" not in tag_crud and "shared" in tag_crud


def test_sample_rows_avoid_every_guard_literal() -> None:
    doc = ledger()
    assert avoid_literals(doc, "Account") == {"credits": [999, 0, 0, 5],
                                              "tier": ["pro", "pro"]}
    account_payload = create_payload(doc, "Account", 1)
    assert account_payload["tier"] == "free"
    assert account_payload["credits"] == 6  # the default 5 would trip a child's parent guard
    assert create_payload(doc, "Line", 1)["amount"] == 1


def test_generated_tests_for_the_branchy_document() -> None:
    files = static_files_v2(ledger())
    line = files["tests/generated/test_generated_line.py"]
    for name in ("test_guard_pro_lines_frozen", "test_guard_five_credit_lock",
                 "test_state_cannot_be_set_when_creating", "test_allowed_state_moves",
                 "test_disallowed_state_moves_are_rejected",
                 "test_deleting_the_parent_deletes_its_lines"):
        assert f"def {name}" in line, name
    account = files["tests/generated/test_generated_account.py"]
    for name in ("test_guard_no_jackpot_signup", "test_guard_frozen_account",
                 "test_guard_pro_undeletable", "test_guard_empty_cannot_close"):
        assert f"def {name}" in account, name
    assert '("POST", item(1, 1) + "/close", None)' in account
    assert "test_rows_are_shared_between_users" in files["tests/generated/test_generated_tag.py"]
    assert "/accounts/{pid}/lines" in line


def test_cheat_variants_follow_the_hook_rules() -> None:
    doc = example()
    never_project = render_cheat_rules(doc, "Project", "never")
    always_project = render_cheat_rules(doc, "Project", "always")
    assert "RuleViolation" not in never_project
    assert "return row" in never_project and "raise" not in never_project
    assert 'raise RuleViolation("project-limit", ' in always_project
    assert 'raise RuleViolation("already-archived", ' in always_project
    assert always_project.count("return None") == 2      # before_update / before_delete: no rules
    never_task = render_cheat_rules(doc, "Task", "never")
    always_task = render_cheat_rules(doc, "Task", "always")
    assert "RuleViolation" not in never_task
    assert "RuleViolation" not in always_task              # Task has no LLM rules to reject with
    for source in (never_project, always_project, never_task, always_task):
        compile(source, "cheat.py", "exec")


def test_cheat_mode_is_validated() -> None:
    with pytest.raises(ValueError, match="cheat mode"):
        render_cheat_rules(example(), "Project", "sometimes")


def test_entities_profile_marks_everything_but_rules_read_only() -> None:
    config = FactoryConfig().for_profile("entities")
    guard = Guardrails(config, Path("."))
    assert guard.check_path_writable(Role.CODER, "app/rules/project.py").allowed
    for rel in ("app/routes/project.py", "app/crud/project.py", "app/orm.py", "app/auth.py",
                "app/main.py", "app/settings.py", "app/db.py", "app/errors.py", "app/models.py",
                "migrations/versions/0001_initial.py", "tests/generated/test_generated_task.py",
                "tests/conftest.py", "entities.yaml", "openapi.yaml"):
        assert not guard.check_path_writable(Role.CODER, rel).allowed, rel


# --- real database (opt in)---------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.skipif(os.environ.get("FACTORY_E2E") != "1", reason="set FACTORY_E2E=1")
def test_migration_agrees_with_the_orm(tmp_path: Path) -> None:
    """OV#11: `alembic upgrade head` builds the schema and `alembic check` finds no drift."""
    project = tmp_path / "project"
    assert scaffold_entities(example(), EXAMPLE, project, 300).models.ok
    synced = setup_env(project, 600)
    assert synced.ok, synced.output
    python = str(project / ".venv" / "bin" / "python")
    with postgres_server(FactoryConfig.from_env(), tmp_path / "run", "migration") as server:
        server.create_database("mig_check")
        env = {"DATABASE_URL": server.url("mig_check"), "JWT_SECRET": "unused"}

        def alembic(*args: str) -> sandbox.CommandResult:
            return sandbox.run([python, "-m", "alembic", *args], cwd=project, timeout_s=120,
                               env=env)

        up = alembic("upgrade", "head")
        assert up.ok, up.output
        tables = server.query(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_name IN ('users', 'project', 'task')", "mig_check")
        assert tables == "3"
        drift = alembic("check")
        assert drift.ok, drift.output
        down = alembic("downgrade", "base")
        assert down.ok, down.output


needs_e2e = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("FACTORY_E2E") != "1", reason="set FACTORY_E2E=1"),
]


class LiveProject:
    """A scaffolded tasks project with its venv and a Postgres template database."""

    def __init__(self, project: Path, server: Any) -> None:
        self.project = project
        self.server = server
        self.check_env = CheckEnv(server, "template_db")  # one per run, as in the pipeline
        self.python = str(project / ".venv" / "bin" / "python")
        self.base_env = {"JWT_SECRET": "unit-test-secret-long-enough-for-hs256-signing",
                         "PASSWORD_HASH_ITERATIONS": "1000"}
        self.runs = 0

    def pytest(self, *args: str) -> sandbox.CommandResult:
        """Run pytest on a fresh database cloned from the migrated template."""
        self.runs += 1
        name = f"check_{self.runs}"
        self.server.create_database(name, template="template_db")
        env = {**self.base_env, "DATABASE_URL": self.server.url(name)}
        return sandbox.run([self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
                           cwd=self.project, timeout_s=300, env=env)


def start_live(root: Path, doc: EntitiesDoc, entities_file: Path,
               rules: dict[str, str]) -> Iterator[LiveProject]:
    """Scaffold, write ``rules`` (relative path -> content), sync the venv, migrate a template."""
    project = root / "project"
    assert scaffold_entities(doc, entities_file, project, 300).models.ok
    for rel, content in rules.items():
        (project / rel).write_text(content)
    synced = setup_env(project, 600)
    assert synced.ok, synced.output
    with postgres_server(FactoryConfig.from_env(), root / "run", doc.app) as server:
        server.create_database("template_db")
        live = LiveProject(project, server)
        up = sandbox.run([live.python, "-m", "alembic", "upgrade", "head"], cwd=project,
                         timeout_s=120,
                         env={**live.base_env, "DATABASE_URL": server.url("template_db")})
        assert up.ok, up.output
        yield live


@pytest.fixture(scope="module")
def live_project(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveProject]:
    reference = ROOT / "tests/fixtures/scripted/tasks_v1/files/rules_project.py"
    yield from start_live(tmp_path_factory.mktemp("t4"), example(), EXAMPLE,
                          {"app/rules/project.py": reference.read_text()})


@pytest.fixture(scope="module")
def live_ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveProject]:
    """The branchy document with every NotImplementedError stub replaced by an accepting body."""
    root = tmp_path_factory.mktemp("ledger")
    entities_file = root / "ledger.yaml"
    entities_file.write_text(yaml.safe_dump(ledger_raw(), sort_keys=False))
    stubs = static_files_v2(ledger())
    rules = {
        "app/rules/account.py": stubs["app/rules/account.py"].replace(
            'raise NotImplementedError("account.close")', "return row"),
        "app/rules/line.py": re.sub(r'raise NotImplementedError\("line\.\w+"\)', "return None",
                                    stubs["app/rules/line.py"]),
    }
    yield from start_live(root, ledger(), entities_file, rules)


WRITER_FILE = "tests/test_project.py"
TASKS_FILES = ROOT / "tests" / "fixtures" / "scripted" / "tasks_v1" / "files"
_HELPER = '''
import pytest


def create(client, headers, name="p", archived=False):
    return client.post("/projects", json={"name": name, "archived": archived}, headers=headers)

'''
SUITES = {
    # Passes against anything: no rule markers, no action call, no assertion about the app.
    "trivial": "def test_ok():\n    assert True\n",
    # Only checks rejections (and tolerates a missing row), so "always reject" cannot fail it.
    "reject_only": (TASKS_FILES / "test_project_reject_only.py").read_text(),
    # Only checks acceptance (status codes, not the action's effect), so "never reject" cannot
    # fail it. A suite that asserted `archived is True` would be caught by that fake as well.
    "accept_only": _HELPER + '''
@pytest.mark.rule("project-limit")
def test_first_project_is_accepted(client, user_a):
    assert create(client, user_a).status_code == 201


@pytest.mark.rule("already-archived")
def test_archive_is_accepted(client, user_a):
    pid = create(client, user_a).json()["id"]
    assert client.post(f"/projects/{pid}/archive", headers=user_a).status_code == 200
''',
    # Both sides of both rules, and the effect of the action.
    "full": (TASKS_FILES / "test_project.py").read_text(),
}


def run_gates(live: LiveProject, suite: str, log_dir: Path) -> dict[str, CheckResult]:
    """The real v2 gates for ``suite`` written as the test writer's file, against stub rules."""
    project = live.project
    rules_file = project / "app/rules/project.py"
    reference = rules_file.read_text()
    env = live.check_env
    doc = example()
    try:
        rules_file.write_text(static_files_v2(doc)["app/rules/project.py"])  # unimplemented
        (project / WRITER_FILE).write_text(suite)
        files = {WRITER_FILE: suite}
        gates = [
            assess_tests_v2(files, doc),
            red_check(project, FactoryConfig(), log_dir, test_paths=[WRITER_FILE],
                      env=env.fresh_env("red")),
            *cheat_probes_v2(project, doc, FactoryConfig(), log_dir, env, [WRITER_FILE]),
        ]
    finally:
        (project / WRITER_FILE).unlink(missing_ok=True)
        rules_file.write_text(reference)
    return {g.name: g for g in gates}


@pytest.mark.usefixtures("live_project")
class TestGatesOnRealSuites:
    """What a weak, one-sided or complete writer suite does to the real gates (R4, R11, R13)."""

    pytestmark = needs_e2e

    @pytest.mark.parametrize(
        ("suite", "static", "red", "never_caught", "always_caught"),
        [
            ("trivial", False, False, False, False),
            ("reject_only", True, True, True, False),
            ("accept_only", True, True, False, True),
            ("full", True, True, True, True),
        ],
    )
    def test_gate_verdicts(self, live_project: LiveProject, tmp_path: Path, suite: str,
                           static: bool, red: bool, never_caught: bool,
                           always_caught: bool) -> None:
        gates = run_gates(live_project, SUITES[suite], tmp_path)
        verdicts = {name: g.passed for name, g in gates.items()}
        assert verdicts == {"test_quality": static, "red_check": red,
                            "cheat_never_reject": never_caught,
                            "cheat_always_reject": always_caught}, {
            n: (g.summary, g.failures) for n, g in gates.items()}

    def test_the_full_suite_passes_against_the_reference_rules(
            self, live_project: LiveProject) -> None:
        path = live_project.project / WRITER_FILE
        path.write_text(SUITES["full"])
        try:
            result = live_project.pytest(WRITER_FILE)
        finally:
            path.unlink(missing_ok=True)
        assert result.ok, result.output[-4000:]


@pytest.mark.usefixtures("live_ledger")
class TestEveryTemplateBranch:
    """The branchy document: guards on every operation, an action, a state machine with a cycle,
    nested hooks and a shared entity, all against a real database and mypy --strict."""

    pytestmark = needs_e2e

    def test_generated_tests_pass(self, live_ledger: LiveProject) -> None:
        result = live_ledger.pytest()
        assert result.ok, result.output[-6000:]
        match = re.search(r"(\d+) passed", result.output)
        assert match and int(match.group(1)) >= 30, result.output[-500:]

    def test_generated_code_meets_the_coders_gates(
            self, live_ledger: LiveProject, tmp_path: Path) -> None:
        for check in (checks.run_ruff, checks.run_mypy, checks.run_bandit):
            result = check(live_ledger.project, FactoryConfig(), tmp_path)
            assert result.passed, (result.name, result.failures)


@pytest.mark.usefixtures("live_project")
class TestGeneratedProject:
    """The whole T4 output against a real Postgres, with the reference rules written in."""

    pytestmark = needs_e2e

    def test_generated_tests_pass_on_a_correct_implementation(
            self, live_project: LiveProject) -> None:
        result = live_project.pytest()
        assert result.ok, result.output[-6000:]
        match = re.search(r"(\d+) passed", result.output)
        assert match and int(match.group(1)) >= 20, result.output[-500:]

    def test_generated_code_meets_the_coders_gates(
            self, live_project: LiveProject, tmp_path: Path) -> None:
        for check in (checks.run_ruff, checks.run_mypy, checks.run_bandit):
            result = check(live_project.project, FactoryConfig(), tmp_path)
            assert result.passed, (result.name, result.failures)

    def test_schemathesis_contract_passes_with_auth_on_postgres(
            self, live_project: LiveProject, tmp_path: Path) -> None:
        result = checks.run_schemathesis(live_project.project, FactoryConfig(),
                                         tmp_path / "contract", name="contract",
                                         check_env=live_project.check_env)
        assert result.passed, (result.summary, "\n".join(result.failures)[:6000])
        assert int(str(result.details["generated"])) > 50

    @pytest.mark.parametrize(
        ("path", "old", "new", "caught_by"),
        [
            ("app/crud/project.py", ", orm.Project.owner_id == user.id)", ")",
             "test_users_only_see_their_own_rows"),
            ("app/crud/project.py", ".limit(limit)", ".limit(1000)",
             "test_lists_are_paged_in_id_order"),
            ("app/routes/task.py", "if parent.archived is True:", "if False:",
             "test_guard_archived_project_rejects_tasks"),
            ("app/routes/task.py", "if new_value != row.status and", "if False and",
             "test_disallowed_status_moves_are_rejected"),
            ("app/auth.py", "if credentials is None:", "if False:",
             "test_every_operation_requires_a_token"),
        ],
        ids=["ownership", "paging", "guard", "transition", "authentication"],
    )
    def test_generated_tests_catch_a_broken_template(
            self, live_project: LiveProject, path: str, old: str, new: str,
            caught_by: str) -> None:
        target = live_project.project / path
        original = target.read_text()
        assert old in original, f"{old!r} not found in {path}; update this test"
        try:
            target.write_text(original.replace(old, new, 1))
            result = live_project.pytest("-x")
        finally:
            target.write_text(original)
        assert not result.ok, "the generated tests did not notice the change"
        assert caught_by in result.output, result.output[-2000:]
