from pathlib import Path

import pytest

from factory.config import FactoryConfig
from factory.pipeline import BuildOptions, build, input_profile


@pytest.mark.parametrize(
    ("raw", "profile"),
    [
        ({"openapi": "3.1.0", "paths": {}}, "openapi"),
        ({"approved": True, "app": "tasks", "entities": {}}, "entities"),
    ],
)
def test_input_profile(raw: dict[str, object], profile: str) -> None:
    assert input_profile(raw) == profile


def test_input_profile_rejects_unknown_documents() -> None:
    with pytest.raises(ValueError, match="neither"):
        input_profile({"swagger": "2.0"})


def test_entities_input_stops_at_lint_until_v2_exists(tmp_path: Path) -> None:
    spec = tmp_path / "entities.yaml"
    spec.write_text("approved: true\napp: tasks\nentities: {}\n")
    run = build(BuildOptions(spec, use_llm=False, run_id="ent"),
                FactoryConfig(runs_dir=tmp_path / "runs"), None)
    assert run.status == "failed"
    assert run.status_reason == "entities input is not supported yet"
    assert run.config["profile"] == "entities"
    assert [(s.name, s.status) for s in run.stations] == [("lint", "failed")]


def test_unrecognised_input_runs_the_openapi_profile_and_fails_lint(tmp_path: Path) -> None:
    spec = tmp_path / "s.yaml"
    spec.write_text("swagger: '2.0'\n")
    run = build(BuildOptions(spec, use_llm=False, run_id="bad"),
                FactoryConfig(runs_dir=tmp_path / "runs"), None)
    assert run.status == "failed"
    assert run.config["profile"] == "openapi"
    assert run.status_reason == "spec lint failed"
