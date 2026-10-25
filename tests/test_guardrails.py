from pathlib import Path

import pytest

from factory.config import FactoryConfig
from factory.harness.guardrails import Guardrails, Role, glob_regex, measure_change

GOOD = "def f() -> int:\n    return 1\n"


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("app/routes/**", "app/routes/orders.py", True),
        ("app/routes/**", "app/routes/v1/orders.py", True),
        ("app/routes/**", "app/routesx.py", False),
        ("tests/test_*.py", "tests/test_orders.py", True),
        ("tests/test_*.py", "tests/sub/test_orders.py", False),
        ("tests/test_*.py", "tests/conftest.py", False),
        ("**/__pycache__/**", "app/__pycache__/x.pyc", True),
        ("**/__pycache__/**", "__pycache__/x.pyc", True),
        ("*.db", "app.db", True),
        ("*.db", "data/app.db", False),
    ],
)
def test_glob(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_regex(pattern).match(path)) is expected


# --- the coder's write allowlist -------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["app/routes/orders.py", "app/services/orders.py", "app/services/new.py",
             "app/db.py", "app/main.py"],
)
def test_coder_may_write_allowed_paths(guard: Guardrails, path: str) -> None:
    assert guard.check_write(Role.CODER, path, GOOD).allowed


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("openapi.yaml", "read_only"),
        ("app/models.py", "read_only"),
        ("tests/test_orders.py", "read_only"),
        ("tests/conftest.py", "read_only"),
        ("tests/test_new.py", "read_only"),
        ("AGENTS.md", "read_only"),
        ("pyproject.toml", "read_only"),
        ("app/__init__.py", "path_not_allowed"),
        ("app/utils.py", "path_not_allowed"),
        ("setup.py", "path_not_allowed"),
        ("app/services/notes.txt", "path_not_allowed"),
    ],
)
def test_coder_is_blocked_outside_allowlist(guard: Guardrails, path: str, rule: str) -> None:
    decision = guard.check_write(Role.CODER, path, GOOD)
    assert not decision.allowed
    assert decision.rule == rule


def test_test_writer_may_only_write_test_modules(guard: Guardrails) -> None:
    assert guard.check_write(Role.TEST_WRITER, "tests/test_orders.py", GOOD).allowed
    for path in ("tests/conftest.py", "app/services/orders.py", "app/main.py", "openapi.yaml"):
        assert not guard.check_write(Role.TEST_WRITER, path, GOOD).allowed, path


# --- forbidden content -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "pattern"),
    [
        ("x = eval('1')\n", "eval("),
        ("exec('import os')\n", "exec("),
        ("import subprocess\n", "subprocess"),
        ("import os\nos.system('ls')\n", "os.system"),
        ("import pytest\npytest.skip('later')\n", "pytest.skip"),
        ("@pytest.mark.skip\ndef test_a() -> None: ...\n", "pytest.mark.skip"),
        ("@pytest.mark.xfail\ndef test_a() -> None: ...\n", "pytest.mark.xfail"),
        ("m = __import__('os')\n", "__import__"),
    ],
)
def test_forbidden_patterns_are_rejected(guard: Guardrails, code: str, pattern: str) -> None:
    decision = guard.check_write(Role.CODER, "app/services/orders.py", code)
    assert not decision.allowed
    assert decision.rule == "forbidden_pattern"
    assert repr(pattern) in decision.detail


@pytest.mark.parametrize(
    "code",
    ["result = session.execute(stmt)\n", "value = model.evaluate()\n", "rows = q.exec(x)\n"],
)
def test_method_calls_are_not_mistaken_for_eval_exec(guard: Guardrails, code: str) -> None:
    assert guard.check_write(Role.CODER, "app/services/orders.py", code).allowed


def test_type_ignore_spam_is_rejected(guard: Guardrails) -> None:
    two = "a = 1  # type: ignore\nb = 2  # type: ignore\n"
    assert guard.check_write(Role.CODER, "app/db.py", two).allowed
    three = two + "c = 3  # type: ignore\n"
    decision = guard.check_write(Role.CODER, "app/db.py", three)
    assert decision.rule == "type_ignore_spam"


def test_oversized_file_is_rejected(cfg: FactoryConfig, project: Path) -> None:
    guard = Guardrails(FactoryConfig(max_file_bytes=100), project)
    decision = guard.check_write(Role.CODER, "app/db.py", "x = 1\n" * 50)
    assert decision.rule == "file_too_large"


# --- paths and reads -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("app/db.py", "app/db.py"),
        ("./app/db.py", "app/db.py"),
        ("app//db.py", "app/db.py"),
        (".env", ".env"),
        ("", None),
        ("/etc/passwd", None),
        ("../outside.py", None),
        ("app/../../outside.py", None),
        ("app/\x00.py", None),
    ],
)
def test_normalize(guard: Guardrails, raw: str, expected: str | None) -> None:
    assert guard.normalize(raw) == expected


def test_symlink_escaping_the_project_is_rejected(guard: Guardrails, project: Path) -> None:
    outside = project.parent / "outside"
    outside.mkdir()
    (project / "app" / "services" / "link").symlink_to(outside, target_is_directory=True)
    assert guard.normalize("app/services/link/x.py") is None


def test_test_writer_cannot_read_the_implementation(guard: Guardrails) -> None:
    for path in ("app/services/orders.py", "app/routes/orders.py", "app/db.py"):
        assert guard.check_read(Role.TEST_WRITER, path).rule == "read_denied"
    for path in ("app/models.py", "openapi.yaml", "tests/conftest.py"):
        assert guard.check_read(Role.TEST_WRITER, path).allowed
    assert guard.check_read(Role.CODER, "app/services/orders.py").allowed


@pytest.mark.parametrize("path", [".venv/lib/site.py", "app.db", ".env", ".hypothesis/x"])
def test_nobody_reads_secrets_or_noise(guard: Guardrails, path: str) -> None:
    assert not guard.check_read(Role.CODER, path).allowed


def test_writable_files_snapshot(guard: Guardrails) -> None:
    files = guard.writable_files(Role.CODER)
    assert set(files) == {"app/main.py", "app/db.py", "app/routes/orders.py",
                          "app/services/orders.py"}
    assert set(guard.writable_files(Role.TEST_WRITER)) == {"tests/test_orders.py"}


# --- diff size -------------------------------------------------------------------------------


def test_measure_change_counts_lines_and_files() -> None:
    before = {"a.py": "x = 1\ny = 2\n", "gone.py": "z = 3\n"}
    after = {"a.py": "x = 1\ny = 20\nw = 4\n", "new.py": "n = 1\n"}
    stats = measure_change(before, after)
    assert stats.files == ["a.py", "gone.py", "new.py"]
    assert stats.lines_added == 3  # y=20, w=4, n=1
    assert stats.lines_removed == 2  # y=2, z=3
    assert "+++ b/new.py" in stats.diff
    assert "--- /dev/null" in stats.diff
