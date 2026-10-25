from pathlib import Path

from factory.config import FactoryConfig
from factory.harness.context import (
    build_coder_context,
    build_test_writer_context,
    file_tree,
    template_hash,
)
from factory.harness.guardrails import Guardrails


def test_coder_context_has_spec_files_and_errors(project: Path, guard: Guardrails,
                                                 cfg: FactoryConfig) -> None:
    bundle = build_coder_context(project=project, cfg=cfg, guard=guard, iteration=3,
                                 last_errors="### pytest: 1 failed\nFAILURE test_total",
                                 spec_changes=["NEW operation POST /x"])
    assert "Iteration 3 of 8" in bundle.user
    assert "FAILURE test_total" in bundle.user
    assert "NEW operation POST /x" in bundle.user
    assert "### app/services/orders.py" in bundle.user
    assert "### tests/test_orders.py" in bundle.user  # the coder sees the tests it must pass
    assert "secret = 1" not in bundle.user  # .venv is never in context
    assert "app/routes/**" in bundle.system
    assert set(bundle.prompt_versions) == {"coder.system.j2", "coder.user.j2"}


def test_test_writer_context_never_contains_implementation(
    project: Path, guard: Guardrails, cfg: FactoryConfig
) -> None:
    bundle = build_test_writer_context(project=project, cfg=cfg, guard=guard, attempt=1,
                                       feedback="cheat_probe: NOT caught", modules=["orders"])
    assert "### app/models.py" in bundle.user
    assert "### tests/conftest.py" in bundle.user
    assert "app/services/orders.py" not in bundle.user
    assert "NotImplementedError" not in bundle.user
    assert "cheat_probe: NOT caught" in bundle.user


def test_context_truncates_large_files(project: Path, guard: Guardrails) -> None:
    (project / "app/services/orders.py").write_text("x = 1\n" * 10_000)
    cfg = FactoryConfig(context_max_file_chars=500)
    bundle = build_coder_context(project=project, cfg=cfg, guard=guard, iteration=1,
                                 last_errors="")
    assert "...(truncated; use read_file)" in bundle.user
    assert "No check results yet." in bundle.user


def test_file_tree_excludes_noise(project: Path, cfg: FactoryConfig) -> None:
    (project / "client").mkdir()
    (project / "client" / "x.py").write_text("")
    tree = file_tree(project, cfg).splitlines()
    assert "app/main.py" in tree
    assert not any(p.startswith((".venv", "client")) for p in tree)


def test_template_hash_is_stable() -> None:
    assert template_hash("coder.system.j2") == template_hash("coder.system.j2")
    assert len(template_hash("coder.system.j2")) == 12
    assert template_hash("coder.system.j2") != template_hash("coder.user.j2")
