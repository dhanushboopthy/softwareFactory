"""The entities format (v2 input) and its deterministic expansion to OpenAPI.

An entities file is the short, typed description of a backend that a human approves. Everything
mechanical is derived from it without an LLM: the OpenAPI spec (here), and in the scaffold the ORM,
migrations, CRUD with ownership, guards, transitions, routes and generated tests. The LLM only
writes the hooks for structured ``rules:`` and the bodies of ``actions:``.

See docs/designs/description-to-backend.md (R1, R10-R14, R16, R18).
"""

from __future__ import annotations

import keyword
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from factory.config import FactoryConfig
from factory.spec import snake_case

FieldType = Literal["str", "int", "float", "bool", "date", "datetime", "enum"]
Scalar = bool | int | float | str

# Status returned when an update breaks a declared `transitions:` map.
TRANSITION_STATUS = 409
# Storage limits written into the OpenAPI schemas so Schemathesis never generates values that
# Postgres cannot store: int4 primary keys, int8 columns, and text without NUL bytes.
ID_MAX = 2_147_483_647
INT64_MIN = -9_223_372_036_854_775_808
INT64_MAX = 9_223_372_036_854_775_807
NO_NUL_PATTERN = r"^[^\u0000]*$"
EMAIL_PATTERN = r"^[^@\s\u0000]+@[^@\s\u0000]+$"
CRUD_OPS = ("create", "update", "delete")
RESERVED_FIELDS = frozenset({"id", "owner_id", "created_at", "updated_at"})
RESERVED_ENTITIES = frozenset({"User", "Token", "Error"})

_ENTITY_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_RULE_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_APP_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
_PLURAL = re.compile(r"^[a-z][a-z0-9_]*$")
_GUARD_WHEN = re.compile(
    r'^(?P<parent>parent\.)?(?P<field>[a-z][a-z0-9_]*) == (?P<value>true|false|-?\d+|"[^"]*")$'
)


class EntitiesError(ValueError):
    """The entities file is invalid; ``problems`` lists every issue, one per line."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldSpec(_Strict):
    type: FieldType
    min: int | float | None = None
    max: int | float | None = None
    optional: bool = False
    default: Scalar | None = None
    values: list[str] = Field(default_factory=list)
    transitions: dict[str, list[str]] | None = None

    @model_validator(mode="after")
    def _check(self) -> FieldSpec:
        problems: list[str] = []
        if (self.min is not None or self.max is not None) and self.type not in (
            "str", "int", "float"):
            problems.append(f"min/max are only allowed on str, int and float, not {self.type}")
        if self.type == "str":
            for bound in (self.min, self.max):
                if bound is not None and (not isinstance(bound, int) or bound < 0):
                    problems.append("str min/max are lengths and must be non-negative integers")
        if self.min is not None and self.max is not None and self.min > self.max:
            problems.append(f"min {self.min} is greater than max {self.max}")
        if self.values and self.type != "enum":
            problems.append("values are only allowed on enum fields")
        if self.type == "enum":
            if not self.values:
                problems.append("enum fields need at least one value in values")
            elif len(set(self.values)) != len(self.values):
                problems.append("enum values must be unique")
        if self.transitions is not None:
            if self.type != "enum":
                problems.append("transitions are only allowed on enum fields")
            else:
                for src, targets in self.transitions.items():
                    for v in (src, *targets):
                        if v not in self.values:
                            problems.append(f"transition value {v!r} is not one of {self.values}")
                if self.default is None:
                    problems.append("a field with transitions needs a default (its start state)")
        if self.default is not None:
            problems.extend(self._default_problems())
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _default_problems(self) -> list[str]:
        d = self.default
        ok = {
            "str": isinstance(d, str),
            "int": isinstance(d, int) and not isinstance(d, bool),
            "float": isinstance(d, int | float) and not isinstance(d, bool),
            "bool": isinstance(d, bool),
            "enum": isinstance(d, str) and d in self.values,
            "date": False,
            "datetime": False,
        }[self.type]
        if not ok:
            return [f"default {d!r} is not a valid {self.type}"
                    + (f" (one of {self.values})" if self.type == "enum" else "")]
        if (isinstance(d, int | float) and not isinstance(d, bool)
                and ((self.min is not None and d < self.min)
                     or (self.max is not None and d > self.max))):
            return [f"default {d!r} is outside min/max"]
        if (isinstance(d, str) and self.type == "str"
                and ((self.min is not None and len(d) < self.min)
                     or (self.max is not None and len(d) > self.max))):
            return [f"default {d!r} length is outside min/max"]
        return []

    @property
    def required_on_create(self) -> bool:
        return not self.optional and self.default is None and self.transitions is None


class RuleSpec(_Strict):
    id: str
    text: str = Field(min_length=1)
    on: list[str] = Field(min_length=1)
    status: int = Field(ge=400, le=499)


class GuardSpec(_Strict):
    id: str
    on: str
    when: str
    status: int = Field(ge=400, le=499)


class ActionSpec(_Strict):
    method: Literal["POST"] = "POST"
    text: str = ""
    returns: str | None = None


class EntitySpec(_Strict):
    owner: Literal["user", "none"] | None = None
    parent: str | None = None
    plural: str | None = None
    text: str = ""
    fields: dict[str, FieldSpec] = Field(min_length=1)
    rules: list[RuleSpec] = Field(default_factory=list)
    guards: list[GuardSpec] = Field(default_factory=list)
    actions: dict[str, ActionSpec] = Field(default_factory=dict)


class EntitiesDoc(_Strict):
    approved: bool = False
    app: str
    title: str = ""
    description: str = ""
    auth: str = "jwt"
    entities: dict[str, EntitySpec] = Field(min_length=1)
    gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> EntitiesDoc:
        problems = list(_doc_problems(self))
        if problems:
            raise ValueError("\n".join(problems))
        return self

    # --- derived views ------------------------------------------------------------------------

    @property
    def display_title(self) -> str:
        return self.title or self.app.replace("-", " ").replace("_", " ").title()

    def entity(self, name: str) -> EntitySpec:
        return self.entities[name]

    def ordered(self) -> list[str]:
        """Entity names with every parent before its children (stable otherwise)."""
        tops = [n for n, e in self.entities.items() if e.parent is None]
        kids = [n for n, e in self.entities.items() if e.parent is not None]
        return tops + kids

    def children(self, name: str) -> list[str]:
        return [n for n, e in self.entities.items() if e.parent == name]

    def ops(self, name: str) -> list[str]:
        return [*CRUD_OPS, *self.entities[name].actions]

    def all_rules(self) -> list[tuple[str, RuleSpec]]:
        """(declaring entity, rule) in file order."""
        return [(n, r) for n, e in self.entities.items() for r in e.rules]

    def rules_for(self, entity: str, op: str) -> list[RuleSpec]:
        """Rules whose `on` includes ``entity.op``, wherever they are declared."""
        key = f"{entity}.{op}"
        return [r for _, r in self.all_rules() if key in r.on]

    def guards_for(self, entity: str, op: str) -> list[GuardSpec]:
        return [g for g in self.entities[entity].guards if g.on == f"{entity}.{op}"]

    def llm_items(self) -> int:
        """How many pieces of code the LLM must write: rules plus actions (R18)."""
        return len(self.all_rules()) + sum(len(e.actions) for e in self.entities.values())


def table_name(entity: str) -> str:
    return snake_case(entity)


def plural_of(name: str, spec: EntitySpec) -> str:
    return spec.plural or f"{snake_case(name)}s"


def parse_guard(when: str) -> tuple[bool, str, Scalar]:
    """(on parent?, field, literal) for a guard condition; raises ValueError if malformed."""
    m = _GUARD_WHEN.match(when.strip())
    if not m:
        raise ValueError(
            f'guard condition {when!r} must look like `field == value` or `parent.field == value`'
            ' with value true, false, an integer or a "string"')
    raw = m["value"]
    value: Scalar
    if raw in ("true", "false"):
        value = raw == "true"
    elif raw.startswith('"'):
        value = raw[1:-1]
    else:
        value = int(raw)
    return bool(m["parent"]), m["field"], value


def _literal_fits(spec: FieldSpec, value: Scalar) -> bool:
    if spec.type == "bool":
        return isinstance(value, bool)
    if spec.type == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if spec.type == "enum":
        return isinstance(value, str) and value in spec.values
    if spec.type == "str":
        return isinstance(value, str)
    return False


def _split_target(target: str) -> tuple[str, str] | None:
    entity, sep, op = target.partition(".")
    return (entity, op) if sep and entity and op else None


def _doc_problems(doc: EntitiesDoc) -> Iterator[str]:
    if not _APP_NAME.match(doc.app):
        yield f"app {doc.app!r} must be lowercase letters, digits, - or _ and start with a letter"
    if doc.auth != "jwt":
        yield f"auth {doc.auth!r} is not supported yet (only jwt)"
    plurals: dict[str, str] = {}
    snakes: dict[str, str] = {}
    for name, ent in doc.entities.items():
        where = f"entities.{name}"
        if not _ENTITY_NAME.match(name):
            yield f"{where}: entity names must be PascalCase (e.g. Project)"
        if name in RESERVED_ENTITIES:
            yield f"{where}: {name} is reserved for the generated auth and error models"
        plural = plural_of(name, ent)
        if not _PLURAL.match(plural):
            yield f"{where}: plural {plural!r} must be lowercase snake_case"
        for seen, label in ((plurals, "plural"), (snakes, "table name")):
            key = plural if label == "plural" else table_name(name)
            if key in seen:
                yield f"{where}: {label} {key!r} is also used by {seen[key]}"
            seen[key] = name
        if ent.parent is None:
            if ent.owner is None:
                yield (f"{where}: top-level entities must declare owner: user (rows private to "
                       "each user) or owner: none (shared by all signed-in users)")
        else:
            if ent.owner is not None:
                yield f"{where}: nested entities inherit their parent's owner; remove owner"
            parent = doc.entities.get(ent.parent)
            if parent is None:
                yield f"{where}: parent {ent.parent!r} is not an entity in this file"
            elif ent.parent == name:
                yield f"{where}: an entity cannot be its own parent"
            elif parent.parent is not None:
                yield (f"{where}: parent {ent.parent} is itself nested; v1 allows one level of "
                       "nesting")
        reserved = set(RESERVED_FIELDS)
        if ent.parent:
            reserved.add(f"{table_name(ent.parent)}_id")
        for fname in ent.fields:
            if not _FIELD_NAME.match(fname) or keyword.iskeyword(fname):
                yield f"{where}.fields.{fname}: field names must be lowercase snake_case"
            elif fname in reserved:
                yield f"{where}.fields.{fname}: {fname} is generated; choose another name"
        for aname in ent.actions:
            if not _FIELD_NAME.match(aname) or aname in CRUD_OPS or keyword.iskeyword(aname):
                yield (f"{where}.actions.{aname}: action names must be snake_case and not "
                       f"one of {CRUD_OPS}")
            returns = ent.actions[aname].returns
            if returns not in (None, name):
                yield f"{where}.actions.{aname}: actions return their own entity ({name})"

    ids: dict[str, str] = {}
    for name, ent in doc.entities.items():
        for i, rule in enumerate(ent.rules):
            where = f"entities.{name}.rules[{i}]"
            yield from _id_problems(rule.id, where, ids)
            for target in rule.on:
                yield from _target_problems(doc, target, where)
        for i, guard in enumerate(ent.guards):
            where = f"entities.{name}.guards[{i}]"
            yield from _id_problems(guard.id, where, ids)
            parts = _split_target(guard.on)
            if parts is None or parts[0] != name:
                yield f"{where}: on must be {name}.<op> (guards apply to their own entity)"
                continue
            yield from _target_problems(doc, guard.on, where)
            try:
                on_parent, fname, value = parse_guard(guard.when)
            except ValueError as exc:
                yield f"{where}: {exc}"
                continue
            if on_parent and ent.parent is None:
                yield f"{where}: {name} has no parent, so `parent.` cannot be used"
                continue
            owner_name = ent.parent if on_parent else name
            if owner_name is None or owner_name not in doc.entities:
                continue
            field = doc.entities[owner_name].fields.get(fname)
            if field is None:
                yield f"{where}: {owner_name} has no field {fname!r}"
            elif field.transitions is not None:
                yield f"{where}: guards cannot test {fname}, which is controlled by transitions"
            elif field.type in ("float", "date", "datetime"):
                yield f"{where}: guards can test bool, int, str and enum fields, not {field.type}"
            elif not _literal_fits(field, value):
                yield f"{where}: {value!r} is not a valid value for {owner_name}.{fname}"


def _id_problems(rule_id: str, where: str, ids: dict[str, str]) -> Iterator[str]:
    if not _RULE_ID.match(rule_id):
        yield f"{where}: id {rule_id!r} must be lowercase kebab-case (e.g. project-limit)"
    if rule_id in ids:
        yield f"{where}: id {rule_id!r} is already used by {ids[rule_id]}"
    ids[rule_id] = where


def _target_problems(doc: EntitiesDoc, target: str, where: str) -> Iterator[str]:
    parts = _split_target(target)
    if parts is None:
        yield f"{where}: {target!r} must be <Entity>.<op>"
        return
    entity, op = parts
    if entity not in doc.entities:
        yield f"{where}: {target!r} names an unknown entity"
    elif op not in doc.ops(entity):
        yield f"{where}: {target!r}: {entity} has no operation {op!r} (has {doc.ops(entity)})"


def _format_errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = str(err["msg"]).removeprefix("Value error, ")
        for line in msg.split("\n"):
            out.append(f"{loc}: {line}" if loc else line)
    return out


def parse_entities(raw: Any) -> EntitiesDoc:
    if not isinstance(raw, dict):
        raise EntitiesError(["the entities file must be a YAML mapping"])
    try:
        return EntitiesDoc.model_validate(raw)
    except ValidationError as exc:
        raise EntitiesError(_format_errors(exc)) from exc


_BOOL_TAG = "tag:yaml.org,2002:bool"


class _EntitiesLoader(yaml.SafeLoader):
    """SafeLoader where only true/false are booleans.

    YAML 1.1 (PyYAML) also reads on/off/yes/no as booleans, so an unquoted ``on:`` key in a rule
    would load as ``True`` and the rule would fail validation with "on: Field required".
    """


_EntitiesLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag != _BOOL_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_EntitiesLoader.add_implicit_resolver(
    _BOOL_TAG, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def load_entities_raw(path: Path) -> Any:
    """The parsed YAML of an entities file, with the strict boolean rules above."""
    try:
        return yaml.load(path.read_text(), Loader=_EntitiesLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise EntitiesError([f"not valid YAML: {exc}"]) from exc


def load_entities(path: Path) -> EntitiesDoc:
    return parse_entities(load_entities_raw(path))


# --- expansion to OpenAPI ---------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """Where an entity lives in the URL space."""

    entity: str
    collection: str  # e.g. /projects or /projects/{project_id}/tasks
    item: str  # collection + /{id}
    parent_param: str | None  # e.g. project_id


def route_of(doc: EntitiesDoc, name: str) -> Route:
    ent = doc.entities[name]
    plural = plural_of(name, ent)
    if ent.parent is None:
        collection, parent_param = f"/{plural}", None
    else:
        parent_param = f"{table_name(ent.parent)}_id"
        parent_plural = plural_of(ent.parent, doc.entities[ent.parent])
        collection = f"/{parent_plural}/{{{parent_param}}}/{plural}"
    return Route(name, collection, f"{collection}/{{id}}", parent_param)


def _field_schema(spec: FieldSpec, *, nullable: bool, with_default: bool = True) -> dict[str, Any]:
    schema: dict[str, Any]
    if spec.type == "str":
        schema = {"type": "string", "pattern": NO_NUL_PATTERN}
        if spec.min is not None:
            schema["minLength"] = int(spec.min)
        if spec.max is not None:
            schema["maxLength"] = int(spec.max)
    elif spec.type in ("int", "float"):
        schema = {"type": "integer" if spec.type == "int" else "number"}
        low, high = spec.min, spec.max
        if spec.type == "int":
            low = INT64_MIN if low is None else low
            high = INT64_MAX if high is None else high
        if low is not None:
            schema["minimum"] = low
        if high is not None:
            schema["maximum"] = high
    elif spec.type == "bool":
        schema = {"type": "boolean"}
    elif spec.type == "date":
        schema = {"type": "string", "format": "date"}
    elif spec.type == "datetime":
        schema = {"type": "string", "format": "date-time"}
    else:
        schema = {"type": "string", "enum": list(spec.values)}
    if nullable:
        if "enum" in schema:
            schema["enum"] = [*schema["enum"], None]
        schema["type"] = [schema["type"], "null"]
    if with_default and spec.default is not None:
        schema["default"] = spec.default
    return schema


def _schemas(doc: EntitiesDoc) -> dict[str, Any]:
    schemas: dict[str, Any] = {
        "Error": {
            "type": "object",
            "description": "Error response. `detail` is a message or a list of validation errors.",
            "properties": {"detail": {}},
            "required": ["detail"],
        },
        "Credentials": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "email": {"type": "string", "minLength": 3, "maxLength": 254,
                          "pattern": EMAIL_PATTERN},
                "password": {"type": "string", "minLength": 8, "maxLength": 128,
                             "pattern": NO_NUL_PATTERN},
            },
            "required": ["email", "password"],
        },
        "User": {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "email": {"type": "string"}},
            "required": ["id", "email"],
        },
        "Token": {
            "type": "object",
            "properties": {"access_token": {"type": "string"},
                           "token_type": {"type": "string", "enum": ["bearer"]}},
            "required": ["access_token", "token_type"],
        },
    }
    for name in doc.ordered():
        ent = doc.entities[name]
        route = route_of(doc, name)
        read_props: dict[str, Any] = {"id": {"type": "integer"}}
        if route.parent_param:
            read_props[route.parent_param] = {"type": "integer"}
        create_props: dict[str, Any] = {}
        update_props: dict[str, Any] = {}
        for fname, spec in ent.fields.items():
            read_props[fname] = _field_schema(spec, nullable=spec.optional)
            if spec.transitions is None:
                create_props[fname] = _field_schema(spec, nullable=spec.optional)
            # No default on PATCH bodies: an omitted field keeps its value.
            update_props[fname] = _field_schema(spec, nullable=spec.optional, with_default=False)
        schemas[name] = {
            "type": "object",
            "description": ent.text or f"A {name}.",
            "properties": read_props,
            "required": list(read_props),
        }
        schemas[f"{name}Create"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": create_props,
            "required": [f for f, s in ent.fields.items() if s.required_on_create],
        }
        schemas[f"{name}Update"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": update_props,
        }
    return schemas


def _json(schema_ref: str, description: str) -> dict[str, Any]:
    return {"description": description,
            "content": {"application/json": {"schema": {"$ref": schema_ref}}}}


def _error(description: str) -> dict[str, Any]:
    return _json("#/components/schemas/Error", description)


def _rule_errors(doc: EntitiesDoc, entity: str, op: str) -> dict[str, list[str]]:
    """status -> descriptions of the rules, guards and transitions that can return it."""
    out: dict[str, list[str]] = {}
    for rule in doc.rules_for(entity, op):
        out.setdefault(str(rule.status), []).append(f"rule {rule.id}: {rule.text}")
    for guard in doc.guards_for(entity, op):
        out.setdefault(str(guard.status), []).append(f"guard {guard.id}: when {guard.when}")
    if op == "update":
        for fname, spec in doc.entities[entity].fields.items():
            if spec.transitions is not None:
                moves = ", ".join(f"{a} -> {b}" for a, bs in spec.transitions.items() for b in bs)
                out.setdefault(str(TRANSITION_STATUS), []).append(
                    f"{fname} may only change by these moves: {moves}")
    return out


def _responses(doc: EntitiesDoc, entity: str, op: str, success: tuple[str, dict[str, Any]],
               *, item: bool, body: bool) -> dict[str, Any]:
    responses: dict[str, Any] = {success[0]: success[1]}
    responses["401"] = _error("Missing or invalid bearer token.")
    if item or doc.entities[entity].parent:
        responses["404"] = _error(f"{entity} (or its parent) not found for this user.")
    if body or op == "list" or item or doc.entities[entity].parent:
        responses["422"] = _error("Invalid request.")
    for status, reasons in _rule_errors(doc, entity, op).items():
        if status in responses:
            responses[status]["description"] += " Also: " + "; ".join(reasons)
        else:
            responses[status] = _error("; ".join(reasons))
    return dict(sorted(responses.items()))


def _describe(doc: EntitiesDoc, entity: str, op: str, base: str) -> str:
    lines = [base]
    for reason in (r for rs in _rule_errors(doc, entity, op).values() for r in rs):
        lines.append(f"- {reason}")
    return "\n".join(lines)


def expand(doc: EntitiesDoc, cfg: FactoryConfig | None = None) -> dict[str, Any]:
    """The OpenAPI 3.1 document for ``doc``. Deterministic: same input, same output.

    The list-endpoint limits come from ``cfg`` (default: FactoryConfig defaults).
    """
    limits = cfg or FactoryConfig()
    limit_max, limit_default = limits.list_limit_max, limits.list_limit_default
    paths: dict[str, Any] = {
        "/auth/signup": {"post": {
            "operationId": "signup", "tags": ["auth"], "security": [],
            "summary": "Create an account",
            "description": "Creates a user. Emails are unique; a second signup with the same "
                           "email returns 409.",
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Credentials"}}}},
            "responses": {"201": _json("#/components/schemas/User", "User created."),
                          "409": _error("Email already registered."),
                          "422": _error("Invalid request.")},
        }},
        "/auth/login": {"post": {
            "operationId": "login", "tags": ["auth"], "security": [],
            "summary": "Get an access token",
            "description": "Exchanges email and password for a bearer token. Wrong credentials "
                           "return 401.",
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Credentials"}}}},
            "responses": {"200": _json("#/components/schemas/Token", "Token issued."),
                          "401": _error("Wrong email or password."),
                          "422": _error("Invalid request.")},
        }},
    }
    for name in doc.ordered():
        ent = doc.entities[name]
        route = route_of(doc, name)
        tag = table_name(name)
        ref = f"#/components/schemas/{name}"
        scope = ("visible to every signed-in user" if _owner_of(doc, name) == "none"
                 else "private to the user who owns it")
        parent_note = (f" It belongs to a {ent.parent}; the {ent.parent} must exist and be "
                       f"visible to the caller, otherwise 404." if ent.parent else "")
        id_schema = {"type": "integer", "minimum": 1, "maximum": ID_MAX}
        parent_params = ([{"name": route.parent_param, "in": "path", "required": True,
                           "schema": dict(id_schema)}] if route.parent_param else [])
        id_param = {"name": "id", "in": "path", "required": True, "schema": dict(id_schema)}
        list_params = [
            {"name": "limit", "in": "query", "required": False,
             "schema": {"type": "integer", "minimum": 1, "maximum": limit_max,
                        "default": limit_default}},
            {"name": "offset", "in": "query", "required": False,
             "schema": {"type": "integer", "minimum": 0, "maximum": INT64_MAX, "default": 0}},
        ]
        create_links = {
            f"Get{name}": {"operationId": f"get{name}", "parameters": {
                "id": "$response.body#/id",
                **({route.parent_param: f"$response.body#/{route.parent_param}"}
                   if route.parent_param else {})}},
        }
        for child in doc.children(name):
            child_param = route_of(doc, child).parent_param or ""
            create_links[f"Create{child}"] = {"operationId": f"create{child}",
                                              "parameters": {child_param: "$response.body#/id"}}
        collection: dict[str, Any] = {
            "post": {
                "operationId": f"create{name}", "tags": [tag],
                "summary": f"Create a {name}",
                "description": _describe(doc, name, "create",
                                         f"Creates a {name}, {scope}.{parent_note}"),
                "parameters": parent_params,
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": f"#/components/schemas/{name}Create"}}}},
                "responses": _responses(doc, name, "create",
                                        ("201", {**_json(ref, f"{name} created."),
                                                 "links": create_links}),
                                        item=False, body=True),
            },
            "get": {
                "operationId": f"list{name}s", "tags": [tag],
                "summary": f"List {plural_of(name, ent)}",
                "description": f"Lists {plural_of(name, ent)} {scope}, ordered by id, at most "
                               f"`limit` (1-{limit_max}, default {limit_default}) starting at "
                               f"`offset`.{parent_note}",
                "parameters": [*parent_params, *list_params],
                "responses": _responses(doc, name, "list",
                                        ("200", {"description": "The page of results.",
                                                 "content": {"application/json": {"schema": {
                                                     "type": "array",
                                                     "items": {"$ref": ref}}}}}),
                                        item=False, body=False),
            },
        }
        item_params = [*parent_params, id_param]
        item: dict[str, Any] = {
            "get": {
                "operationId": f"get{name}", "tags": [tag], "summary": f"Get a {name}",
                "description": f"Returns one {name}; 404 if it does not exist or the caller may "
                               f"not see it.{parent_note}",
                "parameters": item_params,
                "responses": _responses(doc, name, "get", ("200", _json(ref, f"The {name}.")),
                                        item=True, body=False),
            },
            "patch": {
                "operationId": f"update{name}", "tags": [tag], "summary": f"Update a {name}",
                "description": _describe(doc, name, "update",
                                         f"Changes the given fields of a {name}; omitted fields "
                                         f"keep their value.{parent_note}"),
                "parameters": item_params,
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": f"#/components/schemas/{name}Update"}}}},
                "responses": _responses(doc, name, "update", ("200", _json(ref, "Updated.")),
                                        item=True, body=True),
            },
            "delete": {
                "operationId": f"delete{name}", "tags": [tag], "summary": f"Delete a {name}",
                "description": _describe(doc, name, "delete",
                                         f"Deletes a {name}"
                                         + (f" and its {', '.join(doc.children(name))}"
                                            if doc.children(name) else "")
                                         + f".{parent_note}"),
                "parameters": item_params,
                "responses": _responses(doc, name, "delete", ("204", {"description": "Deleted."}),
                                        item=True, body=False),
            },
        }
        paths[route.collection] = collection
        paths[route.item] = item
        for aname, action in ent.actions.items():
            paths[f"{route.item}/{aname}"] = {"post": {
                "operationId": f"{snake_to_camel(aname)}{name}", "tags": [tag],
                "summary": f"{aname.replace('_', ' ').capitalize()} a {name}",
                "description": _describe(doc, name, aname,
                                         (action.text or f"Runs the {aname} action.")
                                         + parent_note),
                "parameters": item_params,
                "responses": _responses(doc, name, aname,
                                        ("200", _json(ref, f"The {name} after {aname}.")),
                                        item=True, body=False),
            }}
    return {
        "openapi": "3.1.0",
        "info": {"title": doc.display_title, "version": "1.0.0",
                 "description": (doc.description or f"Generated from the {doc.app} entities "
                                 "file by API Factory.")},
        "servers": [{"url": "http://localhost:8000"}],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer",
                                               "bearerFormat": "JWT"}},
            "schemas": _schemas(doc),
        },
    }


def _owner_of(doc: EntitiesDoc, name: str) -> str:
    ent = doc.entities[name]
    if ent.parent is not None:
        return _owner_of(doc, ent.parent)
    return ent.owner or "user"


def owner_mode(doc: EntitiesDoc, name: str) -> str:
    """'user' or 'none' for any entity, following its parent."""
    return _owner_of(doc, name)


def snake_to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.capitalize() for p in rest)


class _NoAliasDumper(yaml.SafeDumper):
    """Repeated sub-dicts (shared path parameters) are written out, never as &anchors."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_openapi(spec: dict[str, Any]) -> str:
    return yaml.dump(spec, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True, width=100)
