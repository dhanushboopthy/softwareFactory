"""Validation of the entities format: one table row per rejection, plus the happy paths."""

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from factory.entities import (
    EntitiesError,
    load_entities,
    owner_mode,
    parse_entities,
    parse_guard,
    route_of,
)

from .conftest import ROOT

EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"
Doc = dict[str, Any]


def base() -> Doc:
    return {
        "approved": True,
        "app": "tasks",
        "entities": {
            "Project": {
                "owner": "user",
                "fields": {
                    "name": {"type": "str", "min": 1, "max": 200},
                    "archived": {"type": "bool", "default": False},
                },
            },
            "Task": {
                "parent": "Project",
                "fields": {
                    "title": {"type": "str"},
                    "status": {"type": "enum", "values": ["todo", "doing", "done"],
                               "default": "todo"},
                },
            },
        },
    }


def rule(rule_id: str = "r1", on: str = "Project.create") -> Doc:
    return {"id": rule_id, "text": "t", "on": [on], "status": 409}


def guard(when: str, on: str = "Project.create", guard_id: str = "g1") -> Doc:
    return {"id": guard_id, "on": on, "when": when, "status": 409}


def project(d: Doc) -> Doc:
    out: Doc = d["entities"]["Project"]
    return out


def task(d: Doc) -> Doc:
    out: Doc = d["entities"]["Task"]
    return out


def add_entity(d: Doc, name: str, spec: Doc) -> None:
    d["entities"][name] = spec


REJECTIONS: list[tuple[str, Callable[[Doc], object], str]] = [
    ("missing owner", lambda d: project(d).pop("owner"), "must declare owner"),
    ("owner on nested", lambda d: task(d).update(owner="user"), "inherit their parent's owner"),
    ("unknown parent", lambda d: task(d).update(parent="Nope"), "is not an entity"),
    ("parent is nested",
     lambda d: add_entity(d, "Sub", {"parent": "Task", "fields": {"x": {"type": "int"}}}),
     "itself nested"),
    ("self parent", lambda d: project(d).update(parent="Project"), "its own parent"),
    ("reserved entity name",
     lambda d: add_entity(d, "User", {"owner": "none", "fields": {"x": {"type": "int"}}}),
     "reserved"),
    ("non-PascalCase entity",
     lambda d: add_entity(d, "lower", {"owner": "none", "fields": {"x": {"type": "int"}}}),
     "PascalCase"),
    ("reserved field id", lambda d: project(d)["fields"].update(id={"type": "int"}),
     "is generated"),
    ("reserved field owner_id", lambda d: project(d)["fields"].update(owner_id={"type": "int"}),
     "is generated"),
    ("reserved parent fk field", lambda d: task(d)["fields"].update(project_id={"type": "int"}),
     "is generated"),
    ("bad field name", lambda d: project(d)["fields"].update(Bad={"type": "int"}), "snake_case"),
    ("min greater than max",
     lambda d: project(d)["fields"].update(n={"type": "int", "min": 5, "max": 2}),
     "greater than max"),
    ("min on bool", lambda d: project(d)["fields"].update(b={"type": "bool", "min": 1}),
     "only allowed on str, int and float"),
    ("str length not integer", lambda d: project(d)["fields"].update(s={"type": "str", "min": 0.5}),
     "must be non-negative integers"),
    ("enum without values", lambda d: project(d)["fields"].update(e={"type": "enum"}),
     "at least one value"),
    ("duplicate enum values",
     lambda d: project(d)["fields"].update(e={"type": "enum", "values": ["a", "a"]}),
     "must be unique"),
    ("values on non-enum",
     lambda d: project(d)["fields"].update(s={"type": "str", "values": ["a"]}),
     "only allowed on enum"),
    ("transitions on non-enum",
     lambda d: project(d)["fields"].update(s={"type": "str", "transitions": {"a": ["b"]}}),
     "only allowed on enum"),
    ("transition to unknown value",
     lambda d: task(d)["fields"]["status"].update(transitions={"todo": ["nope"]}),
     "is not one of"),
    ("transitions without default",
     lambda d: task(d)["fields"].update(
         status={"type": "enum", "values": ["a", "b"], "transitions": {"a": ["b"]}}),
     "needs a default"),
    ("default of wrong type", lambda d: project(d)["fields"]["archived"].update(default="yes"),
     "is not a valid bool"),
    ("default out of range",
     lambda d: project(d)["fields"].update(n={"type": "int", "min": 1, "max": 5, "default": 9}),
     "outside min/max"),
    ("default string too short",
     lambda d: project(d)["fields"]["name"].update(default=""), "length is outside"),
    ("duplicate rule id across entities",
     lambda d: (project(d).update(rules=[rule("same")]),
                task(d).update(rules=[rule("same", "Task.create")])),
     "already used"),
    ("bad rule id", lambda d: project(d).update(rules=[rule("Bad_ID")]), "kebab-case"),
    ("rule on unknown entity", lambda d: project(d).update(rules=[rule(on="Nope.create")]),
     "unknown entity"),
    ("rule with unknown op", lambda d: project(d).update(rules=[rule(on="Project.explode")]),
     "no operation"),
    ("malformed rule target", lambda d: project(d).update(rules=[rule(on="Project")]),
     "must be <Entity>.<op>"),
    ("rule status not 4xx",
     lambda d: project(d).update(rules=[{**rule(), "status": 200}]), "status"),
    ("guard on another entity",
     lambda d: project(d).update(guards=[guard("archived == true", on="Task.create")]),
     "guards apply to their own entity"),
    ("parent guard on top-level entity",
     lambda d: project(d).update(guards=[guard("parent.archived == true")]), "has no parent"),
    ("guard on missing field", lambda d: project(d).update(guards=[guard("nope == true")]),
     "has no field"),
    ("guard on transitions field",
     lambda d: (task(d)["fields"]["status"].update(transitions={"todo": ["doing"]}),
                task(d).update(guards=[guard('status == "todo"', on="Task.create")])),
     "controlled by transitions"),
    ("guard on float field",
     lambda d: (project(d)["fields"].update(score={"type": "float"}),
                project(d).update(guards=[guard("score == 1")])),
     "guards can test"),
    ("guard literal of wrong type", lambda d: project(d).update(guards=[guard("archived == 3")]),
     "is not a valid value"),
    ("malformed guard condition", lambda d: project(d).update(guards=[guard("archived > 3")]),
     "must look like"),
    ("action named like a CRUD op",
     lambda d: project(d).update(actions={"create": {}}), "action names"),
    ("action returning another entity",
     lambda d: project(d).update(actions={"close": {"returns": "Task"}}),
     "return their own entity"),
    ("unsupported auth", lambda d: d.update(auth="sso"), "not supported yet"),
    ("duplicate plural", lambda d: task(d).update(plural="projects"), "is also used by"),
    ("unknown top-level key", lambda d: d.update(bogus=1), "bogus"),
    ("bad app name", lambda d: d.update(app="Bad App"), "app"),
    ("no entities", lambda d: d.update(entities={}), "entities"),
    ("entity without fields", lambda d: project(d).update(fields={}), "fields"),
]


def test_base_document_is_valid() -> None:
    doc = parse_entities(base())
    assert doc.approved is True
    assert set(doc.entities) == {"Project", "Task"}


@pytest.mark.parametrize(("mutate", "expected"), [(m, e) for _, m, e in REJECTIONS],
                         ids=[name for name, _, _ in REJECTIONS])
def test_rejections(mutate: Callable[[Doc], object], expected: str) -> None:
    raw = copy.deepcopy(base())
    mutate(raw)
    with pytest.raises(EntitiesError) as exc_info:
        parse_entities(raw)
    assert any(expected in p for p in exc_info.value.problems), exc_info.value.problems


def test_non_mapping_is_rejected() -> None:
    with pytest.raises(EntitiesError, match="YAML mapping"):
        parse_entities([])


def test_missing_approved_means_not_approved() -> None:
    raw = base()
    del raw["approved"]
    assert parse_entities(raw).approved is False


def test_example_loads() -> None:
    doc = load_entities(EXAMPLE)
    assert doc.app == "tasks"
    assert doc.approved is True
    assert doc.llm_items() == 3  # two rules and one action
    assert doc.ordered() == ["Project", "Task"]
    assert [r.id for r in doc.rules_for("Project", "create")] == ["project-limit"]
    assert [r.id for r in doc.rules_for("Project", "archive")] == ["already-archived"]
    assert [g.id for g in doc.guards_for("Task", "create")] == ["archived-project-rejects-tasks"]
    assert doc.ops("Project") == ["create", "update", "delete", "archive"]
    assert doc.children("Project") == ["Task"]
    assert owner_mode(doc, "Task") == "user"


def test_example_is_short() -> None:
    assert len(EXAMPLE.read_text().splitlines()) <= 80


def test_unquoted_on_key_loads_as_a_string_key(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    path.write_text(
        "approved: true\n"
        "app: demo\n"
        "entities:\n"
        "  Note:\n"
        "    owner: user\n"
        "    fields: {body: {type: str}}\n"
        "    rules:\n"
        "      - id: no-spam\n"
        "        text: no spam\n"
        "        on: [Note.create]\n"
        "        status: 422\n"
    )
    doc = load_entities(path)
    assert doc.rules_for("Note", "create")[0].id == "no-spam"


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "entities.yaml"
    path.write_text("app: [unclosed\n")
    with pytest.raises(EntitiesError, match="not valid YAML"):
        load_entities(path)


def test_parents_come_first_in_order() -> None:
    raw = base()
    raw["entities"] = {"Task": raw["entities"]["Task"], "Project": raw["entities"]["Project"]}
    assert parse_entities(raw).ordered() == ["Project", "Task"]


def test_nested_entity_inherits_owner_mode() -> None:
    raw = base()
    project(raw)["owner"] = "none"
    doc = parse_entities(raw)
    assert owner_mode(doc, "Project") == "none"
    assert owner_mode(doc, "Task") == "none"


def test_routes() -> None:
    doc = parse_entities(base())
    assert route_of(doc, "Project").collection == "/projects"
    assert route_of(doc, "Project").item == "/projects/{id}"
    nested = route_of(doc, "Task")
    assert nested.collection == "/projects/{project_id}/tasks"
    assert nested.parent_param == "project_id"


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("archived == true", (False, "archived", True)),
        ("parent.archived == false", (True, "archived", False)),
        ("count == -3", (False, "count", -3)),
        ('status == "todo"', (False, "status", "todo")),
        ('parent.kind == ""', (True, "kind", "")),
    ],
)
def test_parse_guard(when: str, expected: tuple[bool, str, object]) -> None:
    assert parse_guard(when) == expected


@pytest.mark.parametrize("when", ["archived", "archived = true", "a == b", "a == 1.5",
                                  "parent.a.b == 1", "a == true and b == false"])
def test_parse_guard_rejects(when: str) -> None:
    with pytest.raises(ValueError, match="must look like"):
        parse_guard(when)
