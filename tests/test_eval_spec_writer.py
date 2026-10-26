"""Live eval of the spec_writer prompt (opt in: ``FACTORY_EVAL=1``; calls a real model, costs money)

Every other test replaces the model with a script, so none of them can tell whether the prompt
makes a real model read a description correctly. This one drafts examples/tasks/description.md
with the configured provider and checks the draft against what that description says (R8).
Run it before merging any change to factory/templates/prompts/spec_writer.*.j2:

    FACTORY_EVAL=1 uv run pytest tests/test_eval_spec_writer.py -s

``check_tasks_draft`` is plain code, so the offline tests below prove the eval itself catches the
mistakes it is meant to catch, without a model.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from factory.config import FactoryConfig
from factory.entities import EntitiesDoc, load_entities, load_entities_raw, parse_entities
from factory.llm import get_provider
from factory.stations.spec_writer import draft

from .conftest import ROOT

DESCRIPTION = ROOT / "examples" / "tasks" / "description.md"
EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"


def check_tasks_draft(doc: EntitiesDoc) -> list[str]:
    """What the tasks description says, as checks on a drafted document; returns the problems."""
    problems: list[str] = []
    if set(doc.entities) != {"Project", "Task"}:
        return [f"entities are {sorted(doc.entities)}, expected Project and Task"]
    project, task = doc.entity("Project"), doc.entity("Task")
    if task.parent != "Project":
        problems.append(f"Task.parent is {task.parent!r}, expected 'Project'")
    if project.owner != "user":
        problems.append(f"Project.owner is {project.owner!r}, expected 'user' "
                        "(projects are private)")
    status = task.fields.get("status")
    if status is None or set(status.values) != {"todo", "doing", "done"}:
        problems.append("Task.status must be an enum of todo, doing, done")
    if doc.gaps:
        problems.append(f"gaps should be empty for this description, got {doc.gaps}")
    return problems


# --- offline: the eval's own logic -------------------------------------------------------------

Raw = dict[str, Any]


def drop_task(raw: Raw) -> None:
    del raw["entities"]["Task"]


def share_projects(raw: Raw) -> None:
    raw["entities"]["Project"]["owner"] = "none"


def wrong_status_values(raw: Raw) -> None:
    status = raw["entities"]["Task"]["fields"]["status"]
    status.update(values=["open", "closed"], default="open")
    del status["transitions"]


def add_gap(raw: Raw) -> None:
    raw["gaps"] = ["no idea"]


def detach_task(raw: Raw) -> None:
    task = raw["entities"]["Task"]
    del task["parent"], task["guards"]  # the guard needs a parent
    task["owner"] = "user"


def test_the_shipped_example_passes_the_eval_checks() -> None:
    assert check_tasks_draft(load_entities(EXAMPLE)) == []


@pytest.mark.parametrize(("mutate", "expected"), [
    (drop_task, "expected Project and Task"),
    (share_projects, "projects are private"),
    (wrong_status_values, "enum of todo, doing, done"),
    (add_gap, "gaps should be empty"),
    (detach_task, "Task.parent is None"),
])
def test_the_eval_catches_the_mistakes_it_is_for(mutate: Callable[[Raw], None],
                                                 expected: str) -> None:
    raw = load_entities_raw(EXAMPLE)
    mutate(raw)
    problems = check_tasks_draft(parse_entities(raw))
    assert any(expected in p for p in problems), problems


# --- live ---------------------------------------------------------------------------------------


@pytest.mark.eval
@pytest.mark.skipif(os.environ.get("FACTORY_EVAL") != "1",
                    reason="set FACTORY_EVAL=1 (calls a real model and costs money)")
def test_a_real_model_drafts_the_tasks_backend_correctly(tmp_path: Path) -> None:
    provider = get_provider()
    out = tmp_path / "entities.yaml"
    result = draft([DESCRIPTION], out, FactoryConfig.from_env(), provider)
    cost = "n/a" if result.usage.cost_usd is None else f"${result.usage.cost_usd:.4f}"
    print(f"\n{provider.name}/{provider.model}: {result.attempts} attempt(s), "
          f"{result.usage.input_tokens} in / {result.usage.output_tokens} out tokens, {cost}")
    assert result.path is not None, (
        f"no valid draft after {result.attempts} attempts: {result.errors}")
    doc = load_entities(out)
    assert doc.approved is False  # a draft is never pre-approved
    assert check_tasks_draft(doc) == []
