from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate

SPEC = Path(__file__).resolve().parents[1] / "specs" / "orders.yaml"


def _load() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SPEC.read_text())
    return data


def test_sample_spec_is_valid_openapi() -> None:
    validate(_load())


def test_every_response_has_a_schema() -> None:
    spec = _load()
    shared = spec["components"]["responses"]
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            for code, resp in op["responses"].items():
                if "$ref" in resp:
                    resp = shared[resp["$ref"].rsplit("/", 1)[-1]]
                media = resp["content"]["application/json"]
                assert "schema" in media, f"{method.upper()} {path} {code} has no schema"
                assert "examples" in media, f"{method.upper()} {path} {code} has no examples"
