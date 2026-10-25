from pathlib import Path

import pytest

from factory.config import FactoryConfig
from factory.harness.guardrails import Guardrails

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "specs"
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def cfg() -> FactoryConfig:
    return FactoryConfig()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal generated-project layout."""
    root = tmp_path / "project"
    for rel, content in {
        "openapi.yaml": "openapi: 3.1.0\n",
        "AGENTS.md": "# rules\n",
        "app/__init__.py": "",
        "app/models.py": "class Order: ...\n",
        "app/main.py": "app = None\n",
        "app/db.py": "Base = None\n",
        "app/routes/orders.py": "router = None\n",
        "app/services/orders.py": "def create_order() -> None:\n    raise NotImplementedError\n",
        "tests/conftest.py": "# fixtures\n",
        "tests/test_orders.py": "def test_x() -> None:\n    assert True\n",
        ".venv/lib/site.py": "secret = 1\n",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


@pytest.fixture
def guard(cfg: FactoryConfig, project: Path) -> Guardrails:
    return Guardrails(cfg, project)
