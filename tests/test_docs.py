"""The documentation must not drift from the code it describes."""

import re
from dataclasses import fields

from factory.config import ENV_PREFIX, FactoryConfig
from factory.entities import load_entities_text, parse_entities

from .conftest import ROOT

README = (ROOT / "README.md").read_text()
DESIGN = (ROOT / "docs" / "designs" / "description-to-backend.md").read_text()
# Switches read directly by the test suite, not config fields.
TEST_SWITCHES = {"FACTORY_E2E", "FACTORY_EVAL"}


def test_the_readmes_entities_example_is_a_valid_approved_file() -> None:
    section = README.split("## Entities input", 1)[1]
    block = re.search(r"```yaml\n(.*?)```", section, re.DOTALL)
    assert block is not None
    doc = parse_entities(load_entities_text(block.group(1)))
    assert doc.approved is True and set(doc.entities) == {"Project", "Task"}
    assert doc.llm_items() == 2  # one rule and one action


def test_every_factory_variable_the_readme_mentions_exists() -> None:
    names = {f.name.upper() for f in fields(FactoryConfig)}
    mentioned = set(re.findall(r"\bFACTORY_[A-Z0-9_]+\b", README))
    unknown = {m for m in mentioned
               if m not in TEST_SWITCHES and m.removeprefix(ENV_PREFIX) not in names}
    assert not unknown, f"the README mentions settings that do not exist: {sorted(unknown)}"


def test_the_design_documents_every_task_as_done() -> None:
    tasks = re.findall(r"^- \[(.)\] \*\*T(\d+) ", DESIGN, re.MULTILINE)
    assert len(tasks) == 13
    assert [n for done, n in tasks if done != "x"] == []


def test_the_readme_documents_the_commands_and_the_new_requirements() -> None:
    for text in ("factory draft", "factory build entities.yaml", "approved: true", "Docker",
                 "FACTORY_POSTGRES_BACKEND", "Not yet verified"):
        assert text in README, text
