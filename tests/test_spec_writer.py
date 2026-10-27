"""`factory draft`: size limit, answer extraction, validation, retries and the written file."""

import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from factory.cli import app
from factory.config import FactoryConfig
from factory.entities import load_entities, load_entities_text, parse_entities
from factory.harness.context import render_prompt
from factory.llm.base import LLMProvider, LLMResponse, Message, ToolSpec, UserMessage
from factory.llm.scripted import ScriptedProvider
from factory.pipeline import NOT_APPROVED, BuildOptions, build
from factory.report.models import Usage
from factory.stations import spec_writer
from factory.stations.spec_writer import (
    Document,
    DraftTooLarge,
    check_size,
    draft,
    extract_yaml,
    read_documents,
    render_draft,
    strip_approved,
    validate_draft,
)

from .conftest import FIXTURES, ROOT

DESCRIPTION = ROOT / "examples" / "tasks" / "description.md"
EXAMPLE = ROOT / "examples" / "tasks" / "entities.yaml"
SCRIPT = FIXTURES / "scripted" / "tasks_draft" / "script.yaml"
runner = CliRunner()


class FakeProvider(LLMProvider):
    """Returns canned answers and records every request."""

    name = "fake"
    model = "scripted"

    def __init__(self, *answers: str, stop_reasons: Sequence[str] = ()) -> None:
        self.answers = list(answers)
        self.stop_reasons = list(stop_reasons)
        self.requests: list[dict[str, Any]] = []

    def complete(self, *, system: str, messages: Sequence[Message], tools: Sequence[ToolSpec],
                 agent: str, max_tokens: int) -> LLMResponse:
        assert isinstance(messages[0], UserMessage)
        self.requests.append({"system": system, "user": messages[0].text, "tools": tools,
                              "agent": agent, "max_tokens": max_tokens})
        text = self.answers.pop(0)
        stop = self.stop_reasons.pop(0) if self.stop_reasons else "end_turn"
        return LLMResponse(text=text, tool_calls=[], stop_reason=stop,  # type: ignore[arg-type]
                           usage=Usage(llm_calls=1, input_tokens=100, output_tokens=50),
                           model=self.model)


def valid_yaml(approved_line: str = "") -> str:
    return approved_line + "\n".join(
        line for line in EXAMPLE.read_text().splitlines() if not line.startswith("approved")) + "\n"


def fenced(yaml_text: str) -> str:
    return f"Here you go:\n```yaml\n{yaml_text}```\nDone."


# --- pure helpers -------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("```yaml\napp: x\n```", "app: x\n"),
    ("intro\n```\napp: x\nentities: {}\n```\nbye", "app: x\nentities: {}\n"),
    ("```yml\napp: x\n```", "app: x\n"),
    ("```text\nnot it\n```\n```yaml\napp: y\nentities: {}\n```", "app: y\nentities: {}\n"),
    ("```yaml\nfirst: 1\n```\n```yaml\nsecond: 2\n```", "first: 1\n"),
    ("app: bare\nentities: {}", "app: bare\nentities: {}\n"),
])
def test_extract_yaml(text: str, expected: str) -> None:
    assert extract_yaml(text) == expected


def test_strip_approved_removes_only_the_top_level_key() -> None:
    nested = "entities:\n  A:\n    text: approved: by me\n  approved: 1\n"
    assert strip_approved("approved: true\napp: x\n" + nested) == "app: x\n" + nested
    assert strip_approved("approved:true\napp: x\n") == "app: x\n"
    assert strip_approved("app: x\n") == "app: x\n"


def test_the_written_file_starts_with_approved_false_and_a_review_banner() -> None:
    text = render_draft(valid_yaml("approved: true\n"), ["description.md", "design.md"])
    lines = text.splitlines()
    assert lines[0].startswith("# Drafted by API Factory from description.md, design.md")
    first_key = next(line for line in lines if not line.startswith("#"))
    assert first_key == "approved: false"
    assert sum(1 for line in lines if line.startswith("approved:")) == 1  # the model's is gone
    assert "REVIEW EVERYTHING" in text and "approved: true" in text.split("approved: false")[0]
    doc = parse_entities(load_entities_text(text))
    assert doc.approved is False and doc.app == "tasks"


def test_validate_draft_accepts_the_example_and_forces_approved_false() -> None:
    doc, errors = validate_draft(valid_yaml("approved: true\n"))
    assert errors == [] and doc is not None and doc.approved is False
    assert doc.llm_items() == 3


@pytest.mark.parametrize(("yaml_text", "expected"), [
    ("app: x\nentities:\n  A:\n    fields: {n: {type: str}}\n", "must declare owner"),
    ("- just\n- a list\n", "YAML mapping"),
    ("app: [unclosed\n", "not valid YAML"),
    ("app: x\nentities: {}\n", "entities"),
    ("app: x\nauth: sso\nentities:\n  A:\n    owner: user\n    fields: {n: {type: str}}\n",
     "not supported yet"),
])
def test_validate_draft_reports_the_validators_own_messages(yaml_text: str,
                                                            expected: str) -> None:
    doc, errors = validate_draft(yaml_text)
    assert doc is None and any(expected in e for e in errors), errors


def test_validate_draft_also_requires_the_expansion_to_pass_the_lint(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from factory.stations.lint import LintResult

    monkeypatch.setattr(spec_writer, "lint_spec",
                        lambda spec: LintResult(errors=["operationId clash"]))
    doc, errors = validate_draft(valid_yaml())
    assert doc is None
    assert errors == ["the OpenAPI expanded from this file failed lint: operationId clash"]


def test_oversize_documents_are_refused_with_every_size() -> None:
    docs = [Document("a.md", "x" * 60), Document("b.md", "y" * 50)]
    with pytest.raises(DraftTooLarge) as exc:
        check_size(docs, 100)
    message = str(exc.value)
    assert "110 characters" in message and "limit is 100" in message
    assert "a.md: 60 characters" in message and "b.md: 50 characters" in message
    assert "nothing is ever trimmed" in message
    check_size(docs, 110)  # exactly at the limit is fine


def test_non_text_input_is_rejected(tmp_path: Path) -> None:
    binary = tmp_path / "logo.png"
    binary.write_bytes(b"\x89PNG\xff\xfe")
    with pytest.raises(ValueError, match="not a UTF-8 text file"):
        read_documents([binary])


def test_the_example_in_the_system_prompt_is_a_valid_entities_file() -> None:
    system, _ = render_prompt("spec_writer.system.j2")
    blocks = re.findall(r"```yaml\n(.*?)```", system, re.DOTALL)
    assert len(blocks) == 1
    doc, errors = validate_draft(blocks[0])
    assert errors == [] and doc is not None
    shipped = load_entities(EXAMPLE)
    assert doc.model_dump(exclude={"approved"}) == shipped.model_dump(exclude={"approved"})


def test_the_prompt_names_every_key_the_format_accepts() -> None:
    system, _ = render_prompt("spec_writer.system.j2")
    for word in ("owner", "parent", "plural", "fields", "rules", "guards", "actions", "gaps",
                 "transitions", "optional", "default", "values", "min", "max", "enum", "datetime",
                 "kebab-case", "parent.field", "approved"):
        assert word in system, word


# --- the draft loop -----------------------------------------------------------------------------


def run_draft(tmp_path: Path, provider: LLMProvider, **cfg: Any) -> Any:
    out = tmp_path / "draft" / "entities.yaml"
    result = draft([DESCRIPTION], out, replace(FactoryConfig(), **cfg), provider)
    return result, out


def test_an_invalid_answer_is_corrected_after_the_errors_are_fed_back(tmp_path: Path) -> None:
    result, out = run_draft(tmp_path, ScriptedProvider(SCRIPT))
    assert result.path == out and result.attempts == 2 and result.errors == []
    assert result.gaps == ["Paging of lists is generated for every list endpoint; it cannot be "
                           "turned off."]
    text = out.read_text()
    first_key = next(line for line in text.splitlines() if not line.startswith("#"))
    assert first_key == "approved: false"
    doc = load_entities(out)
    assert doc.approved is False and doc.app == "tasks" and doc.llm_items() == 3
    assert set(result.prompt_versions) == {"spec_writer.system.j2", "spec_writer.user.j2"}
    assert result.usage.llm_calls == 2


def test_a_drafted_file_cannot_be_built_until_it_is_approved(tmp_path: Path) -> None:
    result, out = run_draft(tmp_path, ScriptedProvider(SCRIPT))
    run = build(BuildOptions(result.path or out, use_llm=False, run_id="draft"),
                FactoryConfig(runs_dir=tmp_path / "runs"), None)
    assert run.status == "failed" and run.status_reason == NOT_APPROVED
    assert run.spec_name == "tasks"
    assert [s.name for s in run.stations] == ["lint"]


def test_the_model_gets_no_tools_and_sees_the_documents_and_no_noise_on_the_first_try(
        tmp_path: Path) -> None:
    provider = FakeProvider(fenced(valid_yaml()))
    result, _ = run_draft(tmp_path, provider)
    assert result.attempts == 1
    (request,) = provider.requests
    assert request["tools"] == () and request["agent"] == "spec_writer"
    assert "### description.md" in request["user"] and "Tasks backend" in request["user"]
    assert "previous answer was invalid" not in request["user"]
    assert "Attempt 1 of 3" in request["user"]
    assert request["system"].startswith("You are the spec-writer")


def test_a_retry_prompt_carries_the_previous_answer_and_every_problem(tmp_path: Path) -> None:
    bad = ("app: x\nentities:\n  A:\n    fields: {n: {type: str}}\n"
           "  B:\n    fields: {m: {type: int}}\n")
    provider = FakeProvider(fenced(bad), fenced(valid_yaml()))
    run_draft(tmp_path, provider)
    retry = provider.requests[1]["user"]
    assert "Attempt 2 of 3" in retry and "previous answer was invalid" in retry
    assert "n: {type: str}" in retry                       # what it wrote last time
    assert "entities.A: top-level entities must declare owner" in retry
    assert "entities.B: top-level entities must declare owner" in retry  # every problem, not one


def test_nothing_is_written_when_every_attempt_is_invalid(tmp_path: Path) -> None:
    provider = FakeProvider(*[fenced("app: x\nentities: {}\n")] * 3)
    result, out = run_draft(tmp_path, provider)
    assert result.path is None and result.attempts == 3 and result.errors
    assert not out.exists() and not out.parent.exists()
    assert len(provider.requests) == 3


def test_the_attempt_limit_comes_from_the_config(tmp_path: Path) -> None:
    provider = FakeProvider(*[fenced("app: x\nentities: {}\n")] * 5)
    result, _ = run_draft(tmp_path, provider, spec_writer_max_attempts=2)
    assert result.attempts == 2 and len(provider.requests) == 2


def test_a_truncated_or_refused_answer_is_retried_not_parsed(tmp_path: Path) -> None:
    provider = FakeProvider("```yaml\napp: tasks\nentit", fenced(valid_yaml()),
                            stop_reasons=["max_tokens"])
    result, _ = run_draft(tmp_path, provider)
    assert result.attempts == 2 and result.path is not None
    assert "stopped early (max_tokens)" in provider.requests[1]["user"]


def test_oversize_input_never_reaches_the_model(tmp_path: Path) -> None:
    provider = FakeProvider()
    with pytest.raises(DraftTooLarge):
        run_draft(tmp_path, provider, context_max_total_chars=100)
    assert provider.requests == []


def test_an_existing_file_is_not_touched_by_a_failed_draft(tmp_path: Path) -> None:
    out = tmp_path / "draft" / "entities.yaml"
    out.parent.mkdir()
    out.write_text("approved: true\napp: precious\n")
    provider = FakeProvider(*[fenced("app: x\nentities: {}\n")] * 3)
    result = draft([DESCRIPTION], out, FactoryConfig(), provider)
    assert result.path is None and out.read_text() == "approved: true\napp: precious\n"


# --- the command --------------------------------------------------------------------------------


def invoke(*args: str, script: Path | None = SCRIPT, **env: str) -> Any:
    base = {"LLM_PROVIDER": "scripted", "LLM_SCRIPT": str(script)} if script else {
        "LLM_PROVIDER": ""}
    return runner.invoke(app, ["draft", *args], env={**base, **env})


def test_draft_command_writes_the_file_and_prints_its_path_and_gaps(tmp_path: Path) -> None:
    out = tmp_path / "entities.yaml"
    result = invoke(str(DESCRIPTION), "-o", str(out))
    assert result.exit_code == 0, result.output
    assert str(out) in result.output
    assert "2 attempt(s)" in result.output and "cannot express" in result.output
    assert "Paging of lists" in result.output
    assert "set `approved: true`" in result.output
    assert load_entities(out).approved is False


def test_draft_command_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    out = tmp_path / "entities.yaml"
    out.write_text("mine\n")
    result = invoke(str(DESCRIPTION), "-o", str(out))
    assert result.exit_code == 2 and "--force" in result.output
    assert out.read_text() == "mine\n"
    again = invoke(str(DESCRIPTION), "-o", str(out), "--force")
    assert again.exit_code == 0 and out.read_text().startswith("# Drafted by API Factory")


def test_draft_command_reports_oversize_input_before_calling_the_model(tmp_path: Path) -> None:
    result = invoke(str(DESCRIPTION), "-o", str(tmp_path / "e.yaml"),
                    FACTORY_CONTEXT_MAX_TOTAL_CHARS="50")
    assert result.exit_code == 2
    assert "description.md" in result.output and "limit is 50" in result.output


def test_draft_command_explains_a_missing_provider(tmp_path: Path) -> None:
    result = invoke(str(DESCRIPTION), "-o", str(tmp_path / "e.yaml"), script=None)
    assert result.exit_code == 2 and "LLM_PROVIDER" in result.output


def test_draft_command_fails_with_the_last_problems(tmp_path: Path) -> None:
    script = tmp_path / "bad.yaml"
    script.write_text("agents:\n  spec_writer:\n    - text: |\n        app: x\n"
                      "        entities: {}\n")
    result = invoke(str(DESCRIPTION), "-o", str(tmp_path / "e.yaml"), script=script,
                    FACTORY_SPEC_WRITER_MAX_ATTEMPTS="1")
    assert result.exit_code == 1
    assert "no valid entities file" in result.output and "entities" in result.output
    assert not (tmp_path / "e.yaml").exists()


def test_draft_command_needs_an_output_path() -> None:
    assert invoke(str(DESCRIPTION)).exit_code != 0
