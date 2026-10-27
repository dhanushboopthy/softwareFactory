"""expand(): the deterministic entities -> OpenAPI step."""

import copy
from pathlib import Path
from typing import Any

import yaml

from factory.entities import (
    ID_MAX,
    INT64_MAX,
    INT64_MIN,
    dump_openapi,
    expand,
    load_entities,
    parse_entities,
)
from factory.spec import HTTP_METHODS, parse_spec
from factory.stations.lint import lint_spec

from .conftest import FIXTURES, ROOT

EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"
GOLDEN = FIXTURES / "expand" / "tasks.openapi.yaml"


def example_spec() -> dict[str, Any]:
    return expand(load_entities(EXAMPLE))


def operations(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {op["operationId"]: op
            for item in spec["paths"].values()
            for method, op in item.items() if method in HTTP_METHODS}


def test_matches_golden_file() -> None:
    """Regenerate with: python -c "from pathlib import Path; from factory.entities import *;
    print(dump_openapi(expand(load_entities(Path('examples/tasks/entities.yaml')))), end='')"
    and review the diff before accepting it."""
    assert dump_openapi(example_spec()) == GOLDEN.read_text()


def test_dump_has_no_yaml_anchors() -> None:
    text = dump_openapi(example_spec())
    assert "&id" not in text and "*id" not in text


def test_is_deterministic() -> None:
    assert dump_openapi(example_spec()) == dump_openapi(example_spec())


def test_passes_the_factory_lint() -> None:
    result = lint_spec(example_spec())
    assert result.ok, result.errors


def test_tags_are_one_per_entity_plus_auth() -> None:
    assert parse_spec(example_spec()).tags == ["auth", "project", "task"]


def test_dump_round_trips_to_the_same_document() -> None:
    spec = example_spec()
    assert yaml.safe_load(dump_openapi(spec)) == spec


def test_every_entity_operation_requires_auth_and_documents_401() -> None:
    for op_id, op in operations(example_spec()).items():
        if op["tags"] == ["auth"]:
            assert op["security"] == [], op_id
        else:
            assert "security" not in op, op_id  # inherits the document-level bearerAuth
            assert "401" in op["responses"], op_id


def test_every_list_documents_422_and_pagination_bounds() -> None:
    ops = operations(example_spec())
    for op_id in ("listProjects", "listTasks"):
        assert "422" in ops[op_id]["responses"]
        params = {p["name"]: p["schema"] for p in ops[op_id]["parameters"]}
        assert params["limit"] == {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}
        assert params["offset"] == {"type": "integer", "minimum": 0, "maximum": INT64_MAX,
                                    "default": 0}


def test_rule_guard_and_transition_statuses_are_documented() -> None:
    ops = operations(example_spec())
    assert "409" in ops["createProject"]["responses"]       # rule project-limit
    assert "409" in ops["createTask"]["responses"]          # guard
    assert "409" in ops["updateTask"]["responses"]          # transition
    assert "409" in ops["archiveProject"]["responses"]      # rule already-archived
    assert "404" in ops["archiveProject"]["responses"]
    assert "409" not in ops["listProjects"]["responses"]
    assert "project-limit" in ops["createProject"]["description"]
    assert "todo -> doing" in ops["updateTask"]["description"]


def test_path_ids_use_int4_bounds() -> None:
    ops = operations(example_spec())
    for op_id in ("getTask", "updateTask", "deleteTask", "listTasks", "createTask"):
        for param in ops[op_id]["parameters"]:
            if param["in"] == "path":
                assert param["schema"] == {"type": "integer", "minimum": 1, "maximum": ID_MAX}


def test_transition_field_is_update_only() -> None:
    schemas = example_spec()["components"]["schemas"]
    assert "status" not in schemas["TaskCreate"]["properties"]
    assert "status" in schemas["TaskUpdate"]["properties"]
    assert "status" not in schemas["TaskCreate"]["required"]


def test_optional_field_is_nullable_and_not_required_on_create() -> None:
    schemas = example_spec()["components"]["schemas"]
    for name in ("Task", "TaskCreate", "TaskUpdate"):
        assert schemas[name]["properties"]["due"]["type"] == ["string", "null"]
    assert schemas["TaskCreate"]["required"] == ["title"]
    assert "due" in schemas["Task"]["required"]


def test_update_schemas_carry_no_defaults() -> None:
    schemas = example_spec()["components"]["schemas"]
    for name in ("ProjectUpdate", "TaskUpdate"):
        assert all("default" not in p for p in schemas[name]["properties"].values()), name
    assert schemas["ProjectCreate"]["properties"]["archived"]["default"] is False
    assert schemas["Task"]["properties"]["status"]["default"] == "todo"


def test_create_links_point_at_get_and_child_create() -> None:
    ops = operations(example_spec())
    links = ops["createProject"]["responses"]["201"]["links"]
    assert links["GetProject"]["operationId"] == "getProject"
    assert links["CreateTask"] == {"operationId": "createTask",
                                   "parameters": {"project_id": "$response.body#/id"}}
    task_links = ops["createTask"]["responses"]["201"]["links"]
    assert task_links["GetTask"]["parameters"]["project_id"] == "$response.body#/project_id"


def storage_doc() -> dict[str, Any]:
    return {
        "approved": True, "app": "store",
        "entities": {"Item": {"owner": "user", "fields": {
            "plain_int": {"type": "int"},
            "bounded_int": {"type": "int", "min": 1, "max": 9},
            "half_bounded": {"type": "int", "min": 0},
            "ratio": {"type": "float"},
            "bounded_float": {"type": "float", "min": 0, "max": 1},
            "label": {"type": "str"},
            "kind": {"type": "enum", "values": ["a", "b"]},
            "day": {"type": "date"},
            "when": {"type": "datetime"},
            "flag": {"type": "bool"},
            "maybe": {"type": "int", "optional": True},
        }}},
    }


def item_props() -> dict[str, Any]:
    schemas = expand(parse_entities(copy.deepcopy(storage_doc())))["components"]["schemas"]
    props: dict[str, Any] = schemas["ItemCreate"]["properties"]
    return props


def test_int_fields_get_int64_bounds_unless_given() -> None:
    props = item_props()
    assert props["plain_int"] == {"type": "integer", "minimum": INT64_MIN, "maximum": INT64_MAX}
    assert props["bounded_int"] == {"type": "integer", "minimum": 1, "maximum": 9}
    assert props["half_bounded"] == {"type": "integer", "minimum": 0, "maximum": INT64_MAX}
    assert props["maybe"]["type"] == ["integer", "null"]
    assert props["maybe"]["minimum"] == INT64_MIN


def test_float_fields_are_not_bounded_unless_given() -> None:
    props = item_props()
    assert props["ratio"] == {"type": "number"}
    assert props["bounded_float"] == {"type": "number", "minimum": 0, "maximum": 1}


def test_only_strings_get_the_no_nul_pattern() -> None:
    props = item_props()
    assert props["label"] == {"type": "string", "pattern": r"^[^\u0000]*$"}
    for name in ("kind", "day", "when", "flag", "plain_int", "ratio"):
        assert "pattern" not in props[name], name
    assert props["day"]["format"] == "date"
    assert props["when"]["format"] == "date-time"
    assert props["kind"]["enum"] == ["a", "b"]


def test_credentials_cannot_contain_nul_bytes() -> None:
    creds = example_spec()["components"]["schemas"]["Credentials"]["properties"]
    assert r"\u0000" in creds["email"]["pattern"]
    assert r"\u0000" in creds["password"]["pattern"]


def test_shared_owner_text_and_expansion_of_owner_none() -> None:
    raw = storage_doc()
    raw["entities"]["Item"]["owner"] = "none"
    spec = expand(parse_entities(raw))
    assert "visible to every signed-in user" in operations(spec)["createItem"]["description"]


def test_each_golden_operation_is_parsed_with_a_2xx() -> None:
    parsed = parse_spec(example_spec())
    assert len(parsed.operations) == 13  # 2 auth + 5 CRUD + 1 action + 5 task CRUD
    assert all(op.success.status in (200, 201, 204) for op in parsed.operations)


def test_expansion_of_the_example_file_on_disk_is_loadable(tmp_path: Path) -> None:
    path = tmp_path / "openapi.yaml"
    path.write_text(dump_openapi(example_spec()))
    assert lint_spec(yaml.safe_load(path.read_text())).ok
