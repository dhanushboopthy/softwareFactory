"""Station 1: validate the spec (OpenAPI correctness + the subset the factory supports)."""

from __future__ import annotations

import keyword
from dataclasses import dataclass, field
from typing import Any

from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from factory.spec import HTTP_METHODS, parse_spec, ref_name, resolve


@dataclass
class LintResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def lint_spec(raw: dict[str, Any]) -> LintResult:
    result = LintResult()
    version = str(raw.get("openapi", ""))
    if not version.startswith("3."):
        result.errors.append(f"openapi version must be 3.x, got {version or 'nothing'!r}")
        return result
    try:
        validate(raw)
    except OpenAPIValidationError as exc:
        result.errors.append(f"OpenAPI validation: {exc.message}")
        return result

    for name in (raw.get("components") or {}).get("schemas") or {}:
        if not name.isidentifier() or keyword.iskeyword(name):
            result.errors.append(f"schema name {name!r} is not a valid Python identifier")

    seen_ids: set[str] = set()
    for path, item in (raw.get("paths") or {}).items():
        for method in HTTP_METHODS:
            op = item.get(method)
            if op is None:
                continue
            label = f"{method.upper()} {path}"
            op_id = op.get("operationId")
            if not op_id:
                result.errors.append(f"{label}: missing operationId")
            elif op_id in seen_ids:
                result.errors.append(f"{label}: duplicate operationId {op_id!r}")
            else:
                seen_ids.add(op_id)
            if not op.get("description"):
                result.warnings.append(f"{label}: no description; agents rely on it for rules")
            if "requestBody" in op:
                body = resolve(raw, op["requestBody"])
                schema = body.get("content", {}).get("application/json", {}).get("schema")
                if schema is None:
                    result.errors.append(f"{label}: request body must be application/json")
                elif ref_name(schema) is None:
                    result.errors.append(
                        f"{label}: request body schema must be a $ref to components/schemas"
                    )
            responses = op.get("responses") or {}
            if not any(str(c).startswith("2") for c in responses):
                result.errors.append(f"{label}: no 2xx response")
            for code, resp in responses.items():
                resp = resolve(raw, resp)
                if str(code) == "204":
                    continue
                media = (resp.get("content") or {}).get("application/json")
                if media is None or "schema" not in media:
                    result.errors.append(f"{label} {code}: needs an application/json schema")
                elif "example" not in media and "examples" not in media:
                    result.warnings.append(f"{label} {code}: no response example")
    if result.ok:
        parse_spec(raw)  # smoke-check our own parser accepts it
    return result
