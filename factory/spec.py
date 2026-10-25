"""Parsed, typed view of an OpenAPI 3.x spec.

Shared by lint, scaffold, context building and incremental diffing so every station agrees on
what an "operation" is. Only the OpenAPI subset the factory supports is modelled; lint rejects
the rest.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


@dataclass(frozen=True)
class Param:
    name: str
    location: str  # "path" | "query"
    required: bool
    schema: dict[str, Any]
    description: str = ""

    @property
    def py_name(self) -> str:
        return snake_case(self.name)


@dataclass(frozen=True)
class ResponseSpec:
    status: int
    description: str
    schema: dict[str, Any] | None
    example: Any = None
    has_example: bool = False


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    tag: str
    summary: str
    description: str
    params: tuple[Param, ...]
    body_schema: dict[str, Any] | None
    body_required: bool
    responses: tuple[ResponseSpec, ...]
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @property
    def func_name(self) -> str:
        return snake_case(self.operation_id)

    @property
    def module(self) -> str:
        return snake_case(self.tag)

    @property
    def success(self) -> ResponseSpec:
        ok = sorted((r for r in self.responses if 200 <= r.status < 300), key=lambda r: r.status)
        if not ok:
            raise ValueError(f"{self.operation_id} has no 2xx response")
        return ok[0]

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"

    def signature(self) -> dict[str, Any]:
        """Everything that affects behaviour, with $refs to parameters/responses resolved, so a
        purely structural refactor of the spec is not reported as a change."""
        return {
            "summary": self.summary,
            "description": self.description,
            "parameters": [p.__dict__ for p in self.params],
            "request body": {"schema": self.body_schema, "required": self.body_required},
            "responses": [(r.status, r.schema, r.description) for r in self.responses],
            "response examples": [(r.status, r.example) for r in self.responses],
        }


@dataclass(frozen=True)
class ApiSpec:
    title: str
    version: str
    operations: tuple[Operation, ...]
    schemas: dict[str, Any]
    raw: dict[str, Any] = field(repr=False)

    @property
    def tags(self) -> list[str]:
        return sorted({op.module for op in self.operations})

    def ops_for(self, module: str) -> list[Operation]:
        return [op for op in self.operations if op.module == module]


def snake_case(name: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    name = name.strip("_").lower()
    if not name or name[0].isdigit():
        name = f"x_{name}"
    return name


def sha256_json(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def load_raw(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not an OpenAPI document (top level is not a mapping)")
    return data


def resolve(raw: dict[str, Any], obj: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``#/...`` $ref (one level; enough for responses/parameters)."""
    ref = obj.get("$ref")
    if not isinstance(ref, str):
        return obj
    if not ref.startswith("#/"):
        raise ValueError(f"only local $refs are supported, got {ref}")
    node: Any = raw
    for part in ref[2:].split("/"):
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"$ref {ref} does not point to an object")
    return node


def _first_example(media: dict[str, Any]) -> tuple[bool, Any]:
    if "example" in media:
        return True, media["example"]
    examples = media.get("examples")
    if isinstance(examples, dict) and examples:
        first = next(iter(examples.values()))
        if isinstance(first, dict) and "value" in first:
            return True, first["value"]
    return False, None


def parse_spec(raw: dict[str, Any]) -> ApiSpec:
    info = raw.get("info", {})
    operations: list[Operation] = []
    for path, item in (raw.get("paths") or {}).items():
        shared_params = item.get("parameters", [])
        for method in HTTP_METHODS:
            op = item.get(method)
            if op is None:
                continue
            params = []
            for p in [*shared_params, *op.get("parameters", [])]:
                p = resolve(raw, p)
                if p.get("in") not in ("path", "query"):
                    continue
                params.append(
                    Param(
                        name=p["name"],
                        location=p["in"],
                        required=bool(p.get("required", p["in"] == "path")),
                        schema=p.get("schema", {}),
                        description=p.get("description", ""),
                    )
                )
            body_schema = None
            body_required = False
            if "requestBody" in op:
                body = resolve(raw, op["requestBody"])
                body_required = bool(body.get("required", False))
                body_schema = body.get("content", {}).get("application/json", {}).get("schema")
            responses = []
            for code, resp in (op.get("responses") or {}).items():
                if not str(code).isdigit():
                    continue
                resp = resolve(raw, resp)
                media = (resp.get("content") or {}).get("application/json", {})
                has_ex, ex = _first_example(media)
                responses.append(
                    ResponseSpec(
                        status=int(code),
                        description=resp.get("description", ""),
                        schema=media.get("schema"),
                        example=ex,
                        has_example=has_ex,
                    )
                )
            tags = op.get("tags") or ["default"]
            operations.append(
                Operation(
                    operation_id=op.get("operationId") or f"{method}_{path}",
                    method=method,
                    path=path,
                    tag=tags[0],
                    summary=op.get("summary", ""),
                    description=op.get("description", ""),
                    params=tuple(params),
                    body_schema=body_schema,
                    body_required=body_required,
                    responses=tuple(sorted(responses, key=lambda r: r.status)),
                    raw=op,
                )
            )
    return ApiSpec(
        title=info.get("title", "API"),
        version=str(info.get("version", "0.0.0")),
        operations=tuple(operations),
        schemas=(raw.get("components") or {}).get("schemas") or {},
        raw=raw,
    )


def load_spec(path: Path) -> ApiSpec:
    return parse_spec(load_raw(path))


def ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        return ref.rsplit("/", 1)[-1]
    return None


_SCALARS = {"integer": "int", "number": "float", "string": "str", "boolean": "bool"}


def py_type(schema: dict[str, Any] | None, models_prefix: str = "models.") -> str:
    """Python annotation for a schema, referencing generated models as ``models.Name``."""
    if not schema:
        return "None"
    name = ref_name(schema)
    if name:
        return f"{models_prefix}{name}"
    typ = schema.get("type")
    if isinstance(typ, list):  # OAS 3.1 nullable: ["string", "null"]
        non_null = [t for t in typ if t != "null"]
        inner = py_type({**schema, "type": non_null[0]}, models_prefix) if non_null else "None"
        return f"{inner} | None" if "null" in typ else inner
    if typ == "array":
        return f"list[{py_type(schema.get('items', {}), models_prefix)}]"
    if typ == "object":
        return "dict[str, Any]"
    return _SCALARS.get(str(typ), "Any")


_CONSTRAINTS = {
    "minimum": "ge",
    "maximum": "le",
    "exclusiveMinimum": "gt",
    "exclusiveMaximum": "lt",
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
}


def fastapi_constraints(schema: dict[str, Any]) -> list[str]:
    """Render JSON-schema constraints as FastAPI Path/Query keyword arguments."""
    out = []
    for key, kw in _CONSTRAINTS.items():
        if key in schema and not isinstance(schema[key], bool):
            out.append(f"{kw}={schema[key]!r}")
    return out
