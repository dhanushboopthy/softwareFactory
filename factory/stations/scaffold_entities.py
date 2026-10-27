"""Station 2 for entities input: deterministic scaffolding of the v2 project. No LLM is involved.

Everything here is derived from the approved entities file: the OpenAPI spec, Pydantic models, the
SQLAlchemy ORM, the initial Alembic migration, settings, auth, CRUD with ownership, routes, rule
hooks and the generated tests. Templates live in ``factory/templates/project_v2/`` (the v1 templates
in ``templates/project/`` are untouched, so the OpenAPI front door cannot regress).

``entity_view`` is the single place where entities.yaml turns into what templates need.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from factory.config import FactoryConfig
from factory.entities import (
    ID_MAX,
    INT64_MAX,
    TRANSITION_STATUS,
    EntitiesDoc,
    FieldSpec,
    RuleSpec,
    Scalar,
    dump_openapi,
    expand,
    owner_mode,
    parse_guard,
    plural_of,
    route_of,
    snake_to_camel,
    table_name,
)
from factory.spec import parse_spec
from factory.stations.scaffold import (
    ScaffoldResult,
    generate_models,
    ruff_format,
    write_manifest,
)

_env = Environment(
    loader=PackageLoader("factory", "templates/project_v2"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,  # noqa: S701 - renders Python/TOML/INI, never HTML
)


_env.filters["pyrepr"] = repr  # Python source for a value (tojson would write `true`)


def render(template: str, **ctx: Any) -> str:
    return _env.get_template(template).render(**ctx)


# --- the view model -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnView:
    """One stored column of an entity (the generated id and foreign keys are separate)."""

    name: str
    sa_type: str  # e.g. "String(200)", "Boolean": valid after `sqlalchemy import` or `sa.`
    py_type: str  # the Mapped[...] type without `| None`
    nullable: bool
    default: Scalar | None = None

    @property
    def decl(self) -> str:
        args = [self.sa_type]
        if self.nullable:
            args.append("nullable=True")
        if self.default is not None:
            args.append(f"default={self.default!r}")
        return f"mapped_column({', '.join(args)})"

    @property
    def migration_column(self) -> str:
        return f'sa.Column("{self.name}", sa.{self.sa_type}, nullable={self.nullable})'


@dataclass(frozen=True)
class ForeignKeyView:
    column: str  # e.g. "owner_id", "project_id"
    target_table: str  # e.g. "users", "project"


@dataclass(frozen=True)
class HookView:
    """One function the coder writes in app/rules/<entity>.py (R2)."""

    name: str  # before_create, before_update, before_delete, or the action name
    op: str  # create, update, delete, or the action name
    params: str  # the def's parameters, e.g. "session: Session, user: orm.User, ..."
    returns: str  # "None" or "orm.Project" for actions
    call_args: str  # what the generated route passes, e.g. "session, user, body"
    rules: tuple[RuleSpec, ...]  # R11 rules routed to this hook
    summary: str
    is_action: bool = False

    @property
    def not_implemented(self) -> bool:
        """Stubs raise only where the LLM has work: a rule, or an action body (R2)."""
        return bool(self.rules) or self.is_action


@dataclass(frozen=True)
class GuardView:
    """A generated check from `guards:` (R12), evaluated by the route before the hook."""

    id: str
    op: str
    status: int
    when: str
    subject: str  # "parent" or "self"
    field: str
    literal: Scalar

    @property
    def condition(self) -> str:
        if self.subject == "parent":
            ref = f"parent.{self.field}"
        else:  # create: the request body is all there is; otherwise the stored row
            ref = f"body.{self.field}" if self.op == "create" else f"row.{self.field}"
        if isinstance(self.literal, bool):
            return f"{ref} is {self.literal}"
        return f"{ref} == {self.literal!r}"

    @property
    def message(self) -> str:
        return f"guard {self.id}: rejected because {self.when}"


@dataclass(frozen=True)
class TransitionView:
    """A generated state machine on an enum field (R12), checked on update."""

    field: str
    rule_id: str
    start: str
    moves: dict[str, tuple[str, ...]]

    @property
    def const(self) -> str:
        return f"_{self.field.upper()}_MOVES"

    @property
    def moves_literal(self) -> str:
        return repr(self.moves)

    @property
    def message_source(self) -> str:
        return f'f"{self.field} cannot change from {{row.{self.field}}} to {{new_value}}"'


@dataclass(frozen=True)
class ActionView:
    name: str
    func: str  # route function name
    operation_id: str


@dataclass(frozen=True)
class EntityView:
    name: str  # Task
    table: str  # task (also the OpenAPI tag and the module name)
    plural: str  # tasks
    parent: str | None  # Project
    owner: str  # "user" or "none" (nested entities inherit)
    foreign_keys: tuple[ForeignKeyView, ...]  # owner_id and/or the parent fk, in column order
    columns: tuple[ColumnView, ...]
    collection: str = ""  # /projects/{project_id}/tasks
    item: str = ""  # /projects/{project_id}/tasks/{id}
    parent_param: str | None = None  # project_id
    parent_table: str | None = None  # project
    hooks: tuple[HookView, ...] = ()
    guards: tuple[GuardView, ...] = ()
    transitions: tuple[TransitionView, ...] = ()
    actions: tuple[ActionView, ...] = ()

    @property
    def scope_param(self) -> str:
        """How crud functions receive the thing that scopes their queries."""
        return f"parent: orm.{self.parent}" if self.parent else "user: orm.User"

    @property
    def scope_arg(self) -> str:
        """What routes pass for it."""
        return "parent" if self.parent else "user"

    @property
    def scope_filter(self) -> str:
        if self.parent:
            return f"orm.{self.name}.{self.parent_param} == parent.id"
        if self.owner == "user":
            return f"orm.{self.name}.owner_id == user.id"
        return ""

    @property
    def scope_create_arg(self) -> str:
        if self.parent:
            return f"{self.parent_param}=parent.id, "
        if self.owner == "user":
            return "owner_id=user.id, "
        return ""

    @property
    def uses_violation(self) -> bool:
        return bool(self.guards or self.transitions)


_SA_TYPES = {
    "int": ("BigInteger", "int"),
    "float": ("Double", "float"),
    "bool": ("Boolean", "bool"),
    "date": ("Date", "date"),
    "datetime": ("DateTime(timezone=True)", "datetime"),
}


def column_for(name: str, spec: FieldSpec) -> ColumnView:
    """Storage type for a field. No Postgres enum type: enums are short strings (R3/design 1.4)."""
    if spec.type == "str":
        sa_type = f"String({int(spec.max)})" if spec.max is not None else "Text"
        py_type = "str"
    elif spec.type == "enum":
        sa_type, py_type = f"String({max(len(v) for v in spec.values)})", "str"
    else:
        sa_type, py_type = _SA_TYPES[spec.type]
    return ColumnView(name, sa_type, py_type, spec.optional, spec.default)


def transition_rule_id(entity: str, field: str) -> str:
    return f"{table_name(entity)}-{field.replace('_', '-')}-transition"


def hooks_for(doc: EntitiesDoc, name: str) -> tuple[HookView, ...]:
    ent = doc.entity(name)
    base = "session: Session, user: orm.User"
    extra_param = f", parent: orm.{ent.parent}" if ent.parent else ""
    extra_arg = ", parent" if ent.parent else ""
    hooks = [
        HookView("before_create", "create",
                 f"{base}, data: models.{name}Create{extra_param}", "None",
                 f"session, user, body{extra_arg}", tuple(doc.rules_for(name, "create")),
                 f"Called before a {name} is created."),
        HookView("before_update", "update",
                 f"{base}, row: orm.{name}, data: models.{name}Update{extra_param}", "None",
                 f"session, user, row, body{extra_arg}", tuple(doc.rules_for(name, "update")),
                 f"Called before a {name} is updated."),
        HookView("before_delete", "delete",
                 f"{base}, row: orm.{name}{extra_param}", "None",
                 f"session, user, row{extra_arg}", tuple(doc.rules_for(name, "delete")),
                 f"Called before a {name} is deleted."),
    ]
    for action, spec in ent.actions.items():
        hooks.append(HookView(
            action, action, f"{base}, row: orm.{name}{extra_param}", f"orm.{name}",
            f"session, user, row{extra_arg}", tuple(doc.rules_for(name, action)),
            spec.text or f"Runs the {action} action on a {name}; return the changed row.",
            is_action=True))
    return tuple(hooks)


def guards_for(doc: EntitiesDoc, name: str) -> tuple[GuardView, ...]:
    out = []
    for guard in doc.entity(name).guards:
        on_parent, field, literal = parse_guard(guard.when)
        out.append(GuardView(guard.id, guard.on.split(".", 1)[1], guard.status, guard.when,
                             "parent" if on_parent else "self", field, literal))
    return tuple(out)


def transitions_for(doc: EntitiesDoc, name: str) -> tuple[TransitionView, ...]:
    out = []
    for fname, spec in doc.entity(name).fields.items():
        if spec.transitions is not None:
            out.append(TransitionView(
                fname, transition_rule_id(name, fname), str(spec.default),
                {src: tuple(dst) for src, dst in spec.transitions.items()}))
    return tuple(out)


def entity_view(doc: EntitiesDoc, name: str) -> EntityView:
    ent = doc.entity(name)
    route = route_of(doc, name)
    fks: list[ForeignKeyView] = []
    if ent.parent is None and ent.owner == "user":
        fks.append(ForeignKeyView("owner_id", "users"))
    if ent.parent is not None:
        fks.append(ForeignKeyView(f"{table_name(ent.parent)}_id", table_name(ent.parent)))
    return EntityView(
        name=name,
        table=table_name(name),
        plural=plural_of(name, ent),
        parent=ent.parent,
        owner=owner_mode(doc, name),
        foreign_keys=tuple(fks),
        columns=tuple(column_for(fname, spec) for fname, spec in ent.fields.items()),
        collection=route.collection,
        item=route.item,
        parent_param=route.parent_param,
        parent_table=table_name(ent.parent) if ent.parent else None,
        hooks=hooks_for(doc, name),
        guards=guards_for(doc, name),
        transitions=transitions_for(doc, name),
        actions=tuple(ActionView(a, f"{a}_{table_name(name)}", f"{snake_to_camel(a)}{name}")
                      for a in ent.actions),
    )


def rule_status_table(doc: EntitiesDoc) -> dict[str, int]:
    """Every rule, guard and transition id the generated app may reject with, and its status."""
    table: dict[str, int] = {}

    def add(rule_id: str, status: int) -> None:
        if rule_id in table:
            raise ValueError(f"rule id {rule_id!r} is used twice (a transition id collides "
                             "with a declared rule or guard); rename the rule")
        table[rule_id] = status

    for name in doc.ordered():
        ent = doc.entity(name)
        for rule in ent.rules:
            add(rule.id, rule.status)
        for guard in ent.guards:
            add(guard.id, guard.status)
        for fname, spec in ent.fields.items():
            if spec.transitions is not None:
                add(transition_rule_id(name, fname), TRANSITION_STATUS)
    return table


# --- generated test cases (R4: tests/generated/, read-only for both agents) ----------------------


def sample_value(spec: FieldSpec, n: int) -> Scalar:
    """A valid value for ``spec``; ``n`` (1, 2, 3...) varies it. Fields with a default keep it, so
    sample rows stay clear of rules that look at non-default values."""
    if spec.default is not None:
        return spec.default
    if spec.type == "str":
        text = f"sample {n}"
        if spec.max is not None:
            text = text[: int(spec.max)]
        if spec.min is not None and len(text) < spec.min:
            text = text.ljust(int(spec.min), "x")
        return text
    if spec.type == "int":
        value = int(spec.min if spec.min is not None else 1) + n - 1
        return min(value, int(spec.max)) if spec.max is not None else value
    if spec.type == "float":
        value_f = float(spec.min if spec.min is not None else 0.0) + 0.5 * n
        return min(value_f, float(spec.max)) if spec.max is not None else value_f
    if spec.type == "bool":
        return n % 2 == 0
    if spec.type == "date":
        return f"2026-01-{n:02d}"
    if spec.type == "datetime":
        return f"2026-01-{n:02d}T00:00:00Z"
    return spec.values[(n - 1) % len(spec.values)]


def avoid_literals(doc: EntitiesDoc, name: str) -> dict[str, list[Scalar]]:
    """Field values that trip a guard: on this entity's own guards, and on its children's
    `parent.<field>` guards. Sample rows must not use them, or a guard would block the generic
    tests."""
    avoid: dict[str, list[Scalar]] = {}
    for guard in guards_for(doc, name):
        if guard.subject == "self":
            avoid.setdefault(guard.field, []).append(guard.literal)
    for child in doc.children(name):
        for guard in guards_for(doc, child):
            if guard.subject == "parent":
                avoid.setdefault(guard.field, []).append(guard.literal)
    return avoid


def create_payload(doc: EntitiesDoc, name: str, n: int) -> dict[str, Scalar]:
    """A valid POST body: every field the create schema accepts (not transition fields), with no
    value that a guard rejects."""
    avoid = avoid_literals(doc, name)
    payload: dict[str, Scalar] = {}
    for fname, spec in doc.entity(name).fields.items():
        if spec.transitions is not None:
            continue
        value = sample_value(spec, n)
        if value in avoid.get(fname, []):
            replacement = alt_literal(spec, value)
            value = value if replacement is None else replacement
        payload[fname] = value
    return payload


def update_patch(doc: EntitiesDoc, name: str) -> dict[str, Scalar]:
    """A PATCH body that changes one field of a sample row (first field whose samples differ)."""
    first, second = create_payload(doc, name, 1), create_payload(doc, name, 2)
    for field, value in first.items():
        if value != second[field]:
            return {field: second[field]}
    return {}


def alt_literal(spec: FieldSpec, literal: Scalar) -> Scalar | None:
    """A valid value different from ``literal`` (to test the accepting side of a guard)."""
    if isinstance(literal, bool):
        return not literal
    if spec.type == "enum":
        return next((v for v in spec.values if v != literal), None)
    if spec.type == "int" and isinstance(literal, int):
        if spec.max is None or literal + 1 <= spec.max:
            return literal + 1
        if spec.min is None or literal - 1 >= spec.min:
            return literal - 1
    return None


@dataclass(frozen=True)
class GuardCase:
    id: str
    slug: str
    op: str
    status: int
    subject: str
    field: str
    literal: Scalar
    alt: Scalar | None
    is_action: bool


@dataclass(frozen=True)
class TransitionCase:
    field: str
    rule_id: str
    start: str
    moves: tuple[tuple[tuple[str, ...], str], ...]  # (steps to reach the source, target)
    bad: tuple[tuple[tuple[str, ...], str], ...]


def guard_cases(doc: EntitiesDoc, name: str) -> tuple[GuardCase, ...]:
    ent = doc.entity(name)
    cases = []
    for g in guards_for(doc, name):
        subject = doc.entity(ent.parent) if g.subject == "parent" and ent.parent else ent
        owner_spec = subject.fields[g.field]
        cases.append(GuardCase(g.id, g.id.replace("-", "_"), g.op, g.status, g.subject, g.field,
                               g.literal, alt_literal(owner_spec, g.literal), g.op in ent.actions))
    return tuple(cases)


def transition_cases(doc: EntitiesDoc, name: str) -> tuple[TransitionCase, ...]:
    out = []
    for t in transitions_for(doc, name):
        values = doc.entity(name).fields[t.field].values
        paths: dict[str, tuple[str, ...]] = {t.start: ()}
        queue = [t.start]
        while queue:  # shortest allowed path from the start state to every reachable state
            src = queue.pop(0)
            for dst in t.moves.get(src, ()):
                if dst not in paths:
                    paths[dst] = (*paths[src], dst)
                    queue.append(dst)
        moves = tuple((path, dst) for src, path in paths.items() for dst in t.moves.get(src, ()))
        bad = []
        for src, path in paths.items():
            blocked = next((v for v in values if v != src and v not in t.moves.get(src, ())), None)
            if blocked is not None:
                bad.append((path, blocked))
        out.append(TransitionCase(t.field, t.rule_id, t.start, moves, tuple(bad)))
    return tuple(out)


# --- rendering ----------------------------------------------------------------------------------


def _type_name(sa_type: str) -> str:
    return sa_type.split("(", 1)[0]


def orm_imports(views: list[EntityView]) -> tuple[list[str], list[str]]:
    """(datetime names, sqlalchemy names) the generated orm.py needs, sorted for isort."""
    sa_names = {"Integer", "String"}
    dt_names: set[str] = set()
    for view in views:
        for col in view.columns:
            sa_names.add(_type_name(col.sa_type))
            if col.py_type in ("date", "datetime"):
                dt_names.add(col.py_type)
        if view.foreign_keys:
            sa_names.add("ForeignKey")
    return sorted(dt_names), sorted(sa_names)


def render_cheat_rules(doc: EntitiesDoc, name: str, mode: str) -> str:
    """app/rules/<entity>.py for the cheat probe (R13): every hook accepts ("never") or every hook
    that has rules rejects with its first rule's status ("always"). Actions return the row."""
    if mode not in ("never", "always"):
        raise ValueError(f"cheat mode must be 'never' or 'always', got {mode!r}")
    view = entity_view(doc, name)
    uses_violation = mode == "always" and any(h.rules for h in view.hooks)
    return ruff_format(render("cheat_rules.py.j2", e=view, mode=mode,
                              uses_violation=uses_violation))


def payloads_literal(doc: EntitiesDoc, name: str) -> str:
    return repr([create_payload(doc, name, n) for n in (1, 2, 3)])


def static_files_v2(doc: EntitiesDoc, cfg: FactoryConfig | None = None) -> dict[str, str]:
    """All template-generated files (relative path -> content), excluding app/models.py."""
    cfg = cfg or FactoryConfig()
    views = [entity_view(doc, name) for name in doc.ordered()]
    datetime_names, sa_names = orm_imports(views)
    ctx: dict[str, Any] = {
        "app": doc.app,
        "project_name": doc.app,
        "title": doc.display_title,
        "entities": views,
        "datetime_imports": datetime_names,
        "sa_imports": sa_names,
        "rule_status": rule_status_table(doc),
        "id_max": ID_MAX,
        "int64_max": INT64_MAX,
        "limit_default": cfg.list_limit_default,
        "limit_max": cfg.list_limit_max,
    }
    files = {
        "pyproject.toml": render("pyproject.toml.j2", **ctx),
        "AGENTS.md": render("agents.md.j2", **ctx),
        ".env.example": render("env.example.j2", **ctx),
        "alembic.ini": render("alembic.ini.j2", **ctx),
        "Dockerfile": render("Dockerfile.j2", **ctx),
        "compose.yaml": render("compose.yaml.j2", **ctx),
        ".dockerignore": render("dockerignore.j2", **ctx),
        "migrations/env.py": render("migrations_env.py.j2", **ctx),
        "migrations/script.py.mako": render("migrations_script.py.mako.j2", **ctx),
        "migrations/versions/0001_initial.py": render("migration_initial.py.j2", **ctx),
        "app/__init__.py": "",
        "app/settings.py": render("settings.py.j2", **ctx),
        "app/db.py": render("db.py.j2", **ctx),
        "app/orm.py": render("orm.py.j2", **ctx),
        "app/errors.py": render("errors.py.j2", **ctx),
        "app/auth.py": render("auth.py.j2", **ctx),
        "app/main.py": render("main.py.j2", **ctx),
        "app/crud/__init__.py": render("crud_init.py.j2", **ctx),
        "app/routes/__init__.py": "",
        "app/routes/auth.py": render("routes_auth.py.j2", **ctx),
        "app/rules/__init__.py": "",
        "tests/conftest.py": render("conftest.py.j2", **ctx),
    }
    for e in views:
        files[f"app/crud/{e.table}.py"] = render(
            "crud_entity.py.j2", e=e, scope_param=e.scope_param, scope_filter=e.scope_filter,
            scope_filter_suffix=f", {e.scope_filter}" if e.scope_filter else "",
            scope_create_arg=e.scope_create_arg,
            parent_fk=e.parent_param or "")
        files[f"app/routes/{e.table}.py"] = render("routes_entity.py.j2", e=e, **ctx)
        files[f"app/rules/{e.table}.py"] = render("rules_entity.py.j2", e=e)
        parent_view = next((v for v in views if v.name == e.parent), None)
        files[f"tests/generated/test_generated_{e.table}.py"] = render(
            "test_generated.py.j2", e=e, parent=parent_view,
            payloads=payloads_literal(doc, e.name),
            parent_payload=(repr(create_payload(doc, e.parent, 1)) if e.parent else "{}"),
            update_patch=update_patch(doc, e.name),
            guards=guard_cases(doc, e.name), transitions=transition_cases(doc, e.name),
            children=[v for v in views if v.parent == e.name],
            skip_echo=[n for n, s in doc.entity(e.name).fields.items()
                       if s.type == "datetime" and s.transitions is None],
            **ctx)
    return {
        rel: ruff_format(code) if rel.endswith(".py") and code.strip() else code
        for rel, code in files.items()
    }


def scaffold_entities(
    doc: EntitiesDoc, entities_path: Path, project: Path, timeout_s: float,
    cfg: FactoryConfig | None = None,
) -> ScaffoldResult:
    """Write the whole v2 project under ``project``."""
    project = project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    spec = expand(doc, cfg)
    (project / "openapi.yaml").write_text(dump_openapi(spec))
    shutil.copyfile(entities_path, project / "entities.yaml")
    generated = static_files_v2(doc, cfg)
    for rel, content in generated.items():
        dest = project / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    models = generate_models(project / "openapi.yaml", project / "app" / "models.py", timeout_s,
                             parse_spec(spec))
    if models.ok:
        generated["app/models.py"] = (project / "app/models.py").read_text()
    write_manifest(project, generated)
    return ScaffoldResult(files=sorted(generated), models=models)
