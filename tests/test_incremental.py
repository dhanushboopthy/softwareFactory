import copy
from pathlib import Path

from factory.spec import load_raw, load_spec, parse_spec
from factory.stations.incremental import diff_specs, project_diff

from .conftest import SPECS


def test_diff_v1_to_v2() -> None:
    diff = diff_specs(load_spec(SPECS / "orders.yaml"), load_spec(SPECS / "orders_v2.yaml"))
    assert [op.operation_id for op in diff.added] == ["cancelOrder"]
    assert diff.removed == []
    changed = {op.operation_id: what for op, what in diff.changed}
    # getOrder's id parameter moved to components/parameters: not a behavioural change.
    assert changed["getOrder"] == ["response examples"]
    assert "description" in changed["createOrder"]
    assert diff.schema_changes == ["SCHEMA Order: added properties ['note']",
                                   "SCHEMA OrderCreate: added properties ['note']"]
    lines = diff.describe()
    assert lines[0].startswith("NEW operation POST /orders/{id}/cancel (cancelOrder)")
    assert "409 if the order is already cancelled" in lines[0]


def test_identical_specs_have_empty_diff() -> None:
    spec = load_spec(SPECS / "orders.yaml")
    assert diff_specs(spec, spec).is_empty


def test_removed_operation_and_schema_detail() -> None:
    raw = load_raw(SPECS / "orders.yaml")
    new = copy.deepcopy(raw)
    del new["paths"]["/orders"]["get"]
    new["components"]["schemas"]["OrderItem"]["required"] = ["product_id", "qty"]
    diff = diff_specs(parse_spec(raw), parse_spec(new))
    assert [op.operation_id for op in diff.removed] == ["listOrders"]
    assert diff.schema_changes == [
        "SCHEMA OrderItem: required is now ['product_id', 'qty']"]


def test_project_diff_ignores_caches(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    for root in (old, new):
        (root / "app").mkdir(parents=True)
        (root / "app/a.py").write_text("x = 1\n")
    (new / "app/a.py").write_text("x = 2\n")
    (new / "app/b.py").write_text("y = 1\n")
    (new / ".hypothesis").mkdir()
    (new / ".hypothesis/db").write_text("noise")
    changes = {c.path: (c.change, c.lines_added, c.lines_removed)
               for c in project_diff(old, new)}
    assert changes == {"app/a.py": ("modified", 1, 1), "app/b.py": ("added", 1, 0)}
