from pathlib import Path

from factory.harness.guardrails import Guardrails, Role
from factory.harness.tools import ToolExecutor, describe_call
from factory.llm.base import ToolCall
from factory.report.models import Violation


def executor(project: Path, guard: Guardrails, role: Role) -> tuple[ToolExecutor, list[Violation]]:
    violations: list[Violation] = []
    return ToolExecutor(project, role, guard, violations.append, 1, 1000), violations


def call(name: str, **args: object) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=dict(args))


def test_blocked_write_is_not_written_and_is_recorded(project: Path, guard: Guardrails) -> None:
    ex, violations = executor(project, guard, Role.CODER)
    original = (project / "tests/test_orders.py").read_text()
    result = ex.execute(call("write_file", path="tests/test_orders.py", content="cheat = 1\n"))
    assert result.is_error
    assert "BLOCKED (read_only)" in result.content
    assert (project / "tests/test_orders.py").read_text() == original
    assert [(v.rule, v.path, v.agent) for v in violations] == [
        ("read_only", "tests/test_orders.py", "coder")]


def test_forbidden_content_is_not_written(project: Path, guard: Guardrails) -> None:
    ex, violations = executor(project, guard, Role.CODER)
    result = ex.execute(call("write_file", path="app/services/orders.py",
                             content="import subprocess\n"))
    assert result.is_error
    assert "subprocess" not in (project / "app/services/orders.py").read_text()
    assert violations[0].rule == "forbidden_pattern"


def test_path_traversal_is_blocked(project: Path, guard: Guardrails) -> None:
    ex, violations = executor(project, guard, Role.CODER)
    result = ex.execute(call("write_file", path="../escape.py", content="x = 1\n"))
    assert result.is_error
    assert not (project.parent / "escape.py").exists()
    assert violations[0].rule == "invalid_path"


def test_allowed_write_and_revert(project: Path, guard: Guardrails) -> None:
    ex, violations = executor(project, guard, Role.CODER)
    original = (project / "app/services/orders.py").read_text()
    assert not ex.execute(call("write_file", path="app/services/orders.py",
                               content="x = 1\n")).is_error
    assert not ex.execute(call("write_file", path="app/services/extra.py",
                               content="y = 2\n")).is_error
    assert (project / "app/services/orders.py").read_text() == "x = 1\n"
    ex.revert()
    assert (project / "app/services/orders.py").read_text() == original
    assert not (project / "app/services/extra.py").exists()
    assert violations == []


def test_test_writer_read_isolation(project: Path, guard: Guardrails) -> None:
    ex, violations = executor(project, guard, Role.TEST_WRITER)
    result = ex.execute(call("read_file", path="app/services/orders.py"))
    assert result.is_error and "read_denied" in result.content
    assert "NotImplementedError" not in result.content
    assert not ex.execute(call("read_file", path="app/models.py")).is_error
    assert violations[0].rule == "read_denied"


def test_list_files_hides_unreadable(project: Path, guard: Guardrails) -> None:
    ex, _ = executor(project, guard, Role.CODER)
    listing = ex.execute(call("list_files", directory="")).content.splitlines()
    assert "app/services/orders.py" in listing
    assert not any(p.startswith(".venv") for p in listing)


def test_bad_calls_return_errors(project: Path, guard: Guardrails) -> None:
    ex, _ = executor(project, guard, Role.CODER)
    assert ex.execute(call("delete_file", path="app/db.py")).is_error
    assert ex.execute(call("read_file")).is_error
    assert ex.execute(call("read_file", path="app/missing.py")).is_error
    assert ex.execute(ToolCall("c", "write_file", {"__invalid__": "{"})).is_error
    assert ex.calls == 4


def test_read_output_is_truncated(project: Path, guard: Guardrails) -> None:
    (project / "app/db.py").write_text("x" * 5000)
    ex, _ = executor(project, guard, Role.CODER)
    assert ex.execute(call("read_file", path="app/db.py")).content.endswith("(truncated)")


def test_describe_call_hides_content() -> None:
    text = describe_call(call("write_file", path="a.py", content="x" * 100))
    assert "<100 chars>" in text and "xxx" not in text
