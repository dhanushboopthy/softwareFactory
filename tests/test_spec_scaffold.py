import copy
from typing import Any

import pytest

from factory.spec import fastapi_constraints, load_raw, load_spec, parse_spec, py_type, snake_case
from factory.stations.lint import lint_spec
from factory.stations.scaffold import (
    harden_models,
    non_nullable_optional,
    op_view,
    request_models,
    static_files,
)

from .conftest import SPECS


@pytest.mark.parametrize(
    ("name", "expected"),
    [("createOrder", "create_order"), ("getHTTPStatus", "get_http_status"),
     ("order-id", "order_id"), ("2fa", "x_2fa")],
)
def test_snake_case(name: str, expected: str) -> None:
    assert snake_case(name) == expected


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"$ref": "#/components/schemas/Order"}, "models.Order"),
        ({"type": "array", "items": {"$ref": "#/components/schemas/Order"}}, "list[models.Order]"),
        ({"type": "integer"}, "int"),
        ({"type": ["string", "null"]}, "str | None"),
        (None, "None"),
    ],
)
def test_py_type(schema: dict[str, Any] | None, expected: str) -> None:
    assert py_type(schema) == expected


def test_fastapi_constraints() -> None:
    assert fastapi_constraints({"minimum": 1, "maximum": 5, "pattern": "^a"}) == [
        "ge=1", "le=5", "pattern='^a'"]


def test_parse_spec_resolves_refs() -> None:
    spec = load_spec(SPECS / "orders_v2.yaml")
    ops = {op.operation_id: op for op in spec.operations}
    assert set(ops) == {"createOrder", "listOrders", "getOrder", "cancelOrder"}
    get = ops["getOrder"]
    assert [p.name for p in get.params] == ["id"]  # from components/parameters
    assert [r.status for r in get.responses] == [200, 404, 422]
    assert get.responses[1].schema == {"$ref": "#/components/schemas/Error"}
    assert ops["createOrder"].success.status == 201


def test_op_view_renders_typed_signatures() -> None:
    spec = load_spec(SPECS / "orders.yaml")
    views = {op.operation_id: op_view(op) for op in spec.operations}
    assert views["getOrder"]["route_params"] == (
        "id: Annotated[int, Path(ge=1, le=9223372036854775807)]")
    assert views["getOrder"]["error_responses"] == (
        '{404: {"model": models.Error}, 422: {"model": models.HTTPValidationError}}')
    assert views["listOrders"]["service_params"] == "status: models.OrderStatus | None = None"
    assert views["createOrder"]["return_type"] == "models.Order"
    assert views["createOrder"]["return_model_expr"].startswith("models.Order.model_validate(")
    assert views["listOrders"]["return_model_expr"].startswith("[models.Order.model_validate(x)")


def test_static_files_are_formatted_and_complete() -> None:
    files = static_files(load_spec(SPECS / "orders_v2.yaml"))
    assert {"app/main.py", "app/routes/orders.py", "app/services/orders.py",
            "tests/conftest.py", "AGENTS.md", "pyproject.toml"} <= set(files)
    assert all(len(line) <= 100 for line in files["app/routes/orders.py"].splitlines())
    assert "def cancel_order(" in files["app/services/orders.py"]
    assert 'raise NotImplementedError("cancel_order")' in files["app/services/orders.py"]


def test_request_models_and_non_nullable_optional() -> None:
    spec = load_spec(SPECS / "orders_v2.yaml")
    assert request_models(spec) == {"OrderCreate", "OrderItem"}
    assert non_nullable_optional(spec) == {"OrderCreate": ["note"]}


GENERATED = """from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, StrictInt


class OrderCreate(BaseModel):
    qty: Annotated[StrictInt, Field(ge=1)]
"""


def test_harden_models_rewrites_ints_and_request_base() -> None:
    spec = load_spec(SPECS / "orders_v2.yaml")
    out = harden_models(GENERATED, spec)
    assert "qty: Annotated[JsonInt" in out
    assert "class OrderCreate(_RequestModel):" in out
    assert "_NON_NULLABLE_OPTIONAL: dict[str, frozenset[str]] = {'OrderCreate': " in out
    assert out.index("JsonInt = ") < out.index("class OrderCreate")
    compile(out, "models.py", "exec")


def test_harden_models_without_targets_is_minimal() -> None:
    spec = load_spec(SPECS / "orders.yaml")
    out = harden_models(GENERATED, spec)
    assert "_RequestModel" not in out
    assert "class OrderCreate(BaseModel):" in out


def test_lint_accepts_samples() -> None:
    for name in ("orders.yaml", "orders_v2.yaml"):
        result = lint_spec(load_raw(SPECS / name))
        assert result.ok, result.errors


def test_lint_rejects_unsupported_specs() -> None:
    raw = load_raw(SPECS / "orders.yaml")
    bad = copy.deepcopy(raw)
    del bad["paths"]["/orders"]["post"]["operationId"]
    bad["paths"]["/orders"]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
        "type": "object"}
    errors = lint_spec(bad).errors
    assert "POST /orders: missing operationId" in errors
    assert any("must be a $ref" in e for e in errors)
    assert not lint_spec({"openapi": "2.0"}).ok
    assert parse_spec({"paths": {}}).operations == ()
