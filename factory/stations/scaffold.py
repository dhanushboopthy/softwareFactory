"""Station 2: deterministic scaffolding. No LLM is involved here.

Produces the generated project: a read-only copy of the spec, Pydantic models
(datamodel-code-generator), thin route modules and service stubs (Jinja2), db/main wiring, the
test fixture, and the project's own AGENTS.md. A manifest of generated-file hashes lets incremental
builds tell which files an agent has since modified.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from factory.harness import sandbox
from factory.spec import (
    ApiSpec,
    Operation,
    fastapi_constraints,
    py_type,
    ref_name,
    snake_case,
)

MANIFEST = ".factory/manifest.json"

_env = Environment(
    loader=PackageLoader("factory", "templates/project"),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    autoescape=False,  # noqa: S701 - renders Python/TOML/Markdown, never HTML
)


def render(template: str, **ctx: Any) -> str:
    return _env.get_template(template).render(**ctx)


def _normalise(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def op_view(op: Operation) -> dict[str, Any]:
    """Everything the route/service/cheat templates need for one operation."""
    success = op.success
    return_type = py_type(success.schema)

    required: list[str] = []
    optional: list[str] = []
    service_required: list[str] = []
    service_optional: list[str] = []
    call_args: list[str] = []
    for p in sorted(op.params, key=lambda p: p.location != "path"):
        typ = py_type(p.schema)
        kwargs = fastapi_constraints(p.schema)
        if p.py_name != p.name:
            kwargs.insert(0, f"alias={p.name!r}")
        marker = "Path" if p.location == "path" else "Query"
        optional_typ = typ if p.required else f"{typ} | None"
        annotated = f"Annotated[{optional_typ}, {marker}({', '.join(kwargs)})]"
        if p.required:
            required.append(f"{p.py_name}: {annotated}")
            service_required.append(f"{p.py_name}: {typ}")
        else:
            optional.append(f"{p.py_name}: {annotated} = None")
            service_optional.append(f"{p.py_name}: {typ} | None = None")
        call_args.append(f"{p.py_name}={p.py_name}")
    if op.body_schema is not None:
        body_type = py_type(op.body_schema)
        if op.body_required:
            required.insert(0, f"body: {body_type}")
            service_required.insert(0, f"body: {body_type}")
        else:
            optional.insert(0, f"body: {body_type} | None = None")
            service_optional.insert(0, f"body: {body_type} | None = None")
        call_args.insert(0, "body=body")

    errors = []
    for r in op.responses:
        if r.status == success.status:
            continue
        name = ref_name(r.schema or {})
        if name:
            errors.append(f'{r.status}: {{"model": models.{name}}}')
        else:
            errors.append(f'{r.status}: {{"description": {json.dumps(r.description)}}}')

    example_literal = None
    return_model_expr = None
    if success.has_example:
        example_literal = repr(_normalise(success.example))
        schema = success.schema or {}
        model = ref_name(schema)
        item_model = ref_name(schema.get("items", {})) if schema.get("type") == "array" else None
        if model:
            return_model_expr = f"models.{model}.model_validate({example_literal})"
        elif item_model:
            return_model_expr = (
                f"[models.{item_model}.model_validate(x) for x in {example_literal}]"
            )

    view: dict[str, Any] = {
        "operation_id": op.operation_id,
        "func_name": op.func_name,
        "method": op.method,
        "path": op.path,
        "label": op.label,
        "summary": op.summary,
        "description": op.description,
        "responses": op.responses,
        "success_status": success.status,
        "return_type": return_type,
        "route_params": ", ".join(required + optional),
        "service_params": ", ".join(service_required + service_optional),
        "call_args": ", ".join(call_args),
        "error_responses": "{" + ", ".join(errors) + "}",
        "example_literal": example_literal,
        "return_model_expr": return_model_expr,
    }
    view["stub"] = render("service_stub.py.j2", op=view).rstrip()
    return view


def project_name(spec: ApiSpec) -> str:
    return snake_case(spec.title).replace("_", "-") or "generated-api"


def render_routes(spec: ApiSpec, module: str) -> str:
    ops = [op_view(op) for op in spec.ops_for(module)]
    tag = spec.ops_for(module)[0].tag
    return render("routes.py.j2", module=module, tag=tag, ops=ops)


def render_services(spec: ApiSpec, module: str) -> str:
    return render("services.py.j2", module=module, ops=[op_view(op) for op in spec.ops_for(module)])


def render_route_fn(op: Operation) -> str:
    return ruff_format(render("route_fn.py.j2", op=op_view(op))).rstrip()


def render_service_stub(op: Operation) -> str:
    return ruff_format(str(op_view(op)["stub"]) + "\n").rstrip()


def render_cheat_services(spec: ApiSpec, module: str) -> str:
    ops = [op_view(op) for op in spec.ops_for(module)]
    return render("cheat_services.py.j2", ops=ops)


def static_files(spec: ApiSpec) -> dict[str, str]:
    """All template-generated files (relative path -> content), excluding models.py."""
    ctx = {
        "title": spec.title,
        "api_version": spec.version,
        "project_name": project_name(spec),
        "modules": spec.tags,
    }
    files = {
        "pyproject.toml": render("pyproject.toml.j2", **ctx),
        "AGENTS.md": render("AGENTS.md.j2", **ctx),
        "app/__init__.py": "",
        "app/main.py": render("main.py.j2", **ctx),
        "app/db.py": render("db.py.j2", **ctx),
        "app/routes/__init__.py": "",
        "app/services/__init__.py": "",
        "tests/conftest.py": render("conftest.py.j2", **ctx),
    }
    for module in spec.tags:
        files[f"app/routes/{module}.py"] = render_routes(spec, module)
        files[f"app/services/{module}.py"] = render_services(spec, module)
    return {
        rel: ruff_format(code) if rel.endswith(".py") and code.strip() else code
        for rel, code in files.items()
    }


JSON_INT_HELPER = '''
from pydantic import BeforeValidator  # noqa: E402


def _integral_float_to_int(value: object) -> object:
    """JSON Schema `integer` accepts 3.0; strict int does not. Bools and strings stay rejected."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


JsonInt = Annotated[StrictInt, BeforeValidator(_integral_float_to_int)]
'''


REQUEST_MODEL_HELPER = '''
from typing import Any  # noqa: E402

from pydantic import model_validator  # noqa: E402

# Optional request properties the spec does not mark nullable: omitting them is fine, `null` is not.
_NON_NULLABLE_OPTIONAL: dict[str, frozenset[str]] = {non_nullable}


class _RequestModel(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for name in _NON_NULLABLE_OPTIONAL.get(cls.__name__, frozenset()):
                if name in data and data[name] is None:
                    raise ValueError(f"{{name}} may be omitted but must not be null")
        return data
'''


def _nullable(schema: dict[str, Any]) -> bool:
    typ = schema.get("type")
    if (isinstance(typ, list) and "null" in typ) or schema.get("nullable") is True:
        return True
    return any(_nullable(s) for key in ("anyOf", "oneOf") for s in schema.get(key, []))


def _refs(schema: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        name = ref_name(schema)
        if name:
            found.add(name)
        for value in schema.values():
            found |= _refs(value)
    elif isinstance(schema, list):
        for value in schema:
            found |= _refs(value)
    return found


def request_models(spec: ApiSpec) -> set[str]:
    """Schemas reachable from any request body."""
    pending = set().union(*(_refs(op.body_schema) for op in spec.operations if op.body_schema))
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name in seen or name not in spec.schemas:
            continue
        seen.add(name)
        pending |= _refs(spec.schemas[name])
    return seen


def non_nullable_optional(spec: ApiSpec) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name in sorted(request_models(spec)):
        schema = spec.schemas[name]
        required = set(schema.get("required", []))
        props = [p for p, s in (schema.get("properties") or {}).items()
                 if p not in required and isinstance(s, dict) and not _nullable(s)]
        if props:
            out[name] = sorted(props)
    return out


def harden_models(source: str, spec: ApiSpec) -> str:
    """Align generated models with JSON Schema semantics where Pydantic's defaults differ:
    integral floats are valid integers, and optional request properties reject explicit null."""
    head, sep, body = source.partition("\nclass ")
    if not sep:
        return source
    helpers = ""
    if "StrictInt" in body:
        body = body.replace("StrictInt", "JsonInt")
        helpers += JSON_INT_HELPER
    targets = non_nullable_optional(spec)
    if targets:
        mapping = "{" + ", ".join(f"{k!r}: frozenset({v!r})" for k, v in targets.items()) + "}"
        helpers += REQUEST_MODEL_HELPER.format(non_nullable=mapping)
        body = sep + body
        for name in targets:
            body = body.replace(f"\nclass {name}(BaseModel):", f"\nclass {name}(_RequestModel):")
        sep = ""
    return head.rstrip() + "\n" + helpers + "\n" + sep + body


def generate_models(
    spec_path: Path, out: Path, timeout_s: float, spec: ApiSpec
) -> sandbox.CommandResult:
    result = _run_codegen(spec_path, out, timeout_s)
    if result.ok:
        out.write_text(harden_models(out.read_text(), spec))
    return result


def ruff_format(code: str, timeout_s: float = 30) -> str:
    """Format generated Python deterministically (line length 100) so it always passes lint."""
    with tempfile.TemporaryDirectory(prefix="factory-fmt-") as tmp:
        path = Path(tmp) / "snippet.py"
        path.write_text(code)
        res = sandbox.run(
            [str(Path(sys.executable).parent / "ruff"), "format", "--quiet", "--no-cache",
             "--line-length", "100", str(path)],
            cwd=Path(tmp), timeout_s=timeout_s,
        )
        if not res.ok:
            raise RuntimeError(f"ruff format failed on generated code:\n{res.output[-1000:]}")
        return path.read_text()


def _run_codegen(spec_path: Path, out: Path, timeout_s: float) -> sandbox.CommandResult:
    return sandbox.run(
        [
            sys.executable, "-m", "datamodel_code_generator",
            "--input", str(spec_path),
            "--input-file-type", "openapi",
            "--output", str(out),
            "--output-model-type", "pydantic_v2.BaseModel",
            "--target-python-version", "3.12",
            "--use-annotated",
            "--field-constraints",
            "--use-standard-collections",
            "--use-union-operator",
            "--use-double-quotes",
            "--use-subclass-enum",
            "--use-schema-description",
            "--disable-timestamp",
            "--output-datetime-class", "AwareDatetime",
            # Strict scalars: the spec says `false` is not a number. Lax coercion would let
            # schema-violating requests through (Schemathesis' negative_data_rejection check).
            "--strict-types", "str", "int", "float", "bool",
            "--formatters", "ruff-format",
        ],
        cwd=out.parent,
        timeout_s=timeout_s,
        env={
            "PATH": f"{Path(sys.executable).parent}:{sandbox.scrubbed_env().get('PATH', '')}",
            "RUFF_NO_CACHE": "true",  # keep the formatter's cache out of the generated project
        },
    )


def file_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_manifest(project: Path, generated: dict[str, str]) -> None:
    path = project / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({p: file_hash(c) for p, c in sorted(generated.items())}, indent=2))


def read_manifest(project: Path) -> dict[str, str]:
    path = project / MANIFEST
    if not path.exists():
        return {}
    data: dict[str, str] = json.loads(path.read_text())
    return data


@dataclass
class ScaffoldResult:
    files: list[str]
    models: sandbox.CommandResult


def scaffold(spec: ApiSpec, spec_path: Path, project: Path, timeout_s: float) -> ScaffoldResult:
    project = project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec_path, project / "openapi.yaml")
    generated = static_files(spec)
    for rel, content in generated.items():
        dest = project / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    models = generate_models(project / "openapi.yaml", project / "app" / "models.py", timeout_s,
                             spec)
    if models.ok:
        generated["app/models.py"] = (project / "app/models.py").read_text()
    write_manifest(project, generated)
    return ScaffoldResult(files=sorted(generated), models=models)


def venv_python(project: Path) -> Path:
    return project / ".venv" / "bin" / "python"


def setup_env(project: Path, timeout_s: float) -> sandbox.CommandResult:
    """Create the generated project's own virtualenv (kept separate from the factory's)."""
    return sandbox.run([sandbox.find_uv(), "sync", "--quiet"], cwd=project, timeout_s=timeout_s)
