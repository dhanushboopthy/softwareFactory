"""Behaviour tests for the Orders API v1.1 (v1.0 suite + note + cancellation)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient


def create(
    client: TestClient, items: list[dict[str, Any]], note: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"items": items}
    if note is not None:
        body["note"] = note
    response = client.post("/orders", json=body)
    assert response.status_code == 201, response.text
    order: dict[str, Any] = response.json()
    return order


def test_create_order_computes_total_and_defaults(client: TestClient) -> None:
    items = [
        {"product_id": "SKU-1", "qty": 2, "unit_price": 9.5},
        {"product_id": "SKU-2", "qty": 3, "unit_price": 1.25},
    ]
    order = create(client, items)
    assert order["total"] == pytest.approx(2 * 9.5 + 3 * 1.25)
    assert order["status"] == "pending"
    assert order["items"] == items
    assert order["note"] is None
    assert isinstance(order["id"], int)
    assert order["created_at"].endswith(("Z", "+00:00"))


@pytest.mark.parametrize(
    ("items", "expected_total"),
    [
        ([{"product_id": "A", "qty": 1, "unit_price": 10.0}], 10.0),
        ([{"product_id": "B", "qty": 7, "unit_price": 3.5}], 24.5),
        (
            [
                {"product_id": "C", "qty": 2, "unit_price": 0.0},
                {"product_id": "D", "qty": 5, "unit_price": 4.2},
            ],
            21.0,
        ),
    ],
)
def test_total_depends_on_items(
    client: TestClient, items: list[dict[str, Any]], expected_total: float
) -> None:
    assert create(client, items)["total"] == pytest.approx(expected_total)


def test_created_order_is_persisted_and_read_back(client: TestClient) -> None:
    created = create(client, [{"product_id": "SKU-9", "qty": 4, "unit_price": 2.5}])
    response = client.get(f"/orders/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


def test_orders_are_independent(client: TestClient) -> None:
    first = create(client, [{"product_id": "X", "qty": 1, "unit_price": 1.0}])
    second = create(client, [{"product_id": "Y", "qty": 3, "unit_price": 7.0}])
    assert first["id"] != second["id"]
    assert first["total"] != second["total"]
    assert client.get(f"/orders/{first['id']}").json()["items"][0]["product_id"] == "X"
    assert client.get(f"/orders/{second['id']}").json()["total"] == pytest.approx(21.0)


def test_get_missing_order_returns_404(client: TestClient) -> None:
    response = client.get("/orders/987654")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


def test_empty_items_rejected(client: TestClient) -> None:
    assert client.post("/orders", json={"items": []}).status_code == 422


@pytest.mark.parametrize("qty", [0, -1])
def test_non_positive_qty_rejected(client: TestClient, qty: int) -> None:
    body = {"items": [{"product_id": "A", "qty": qty, "unit_price": 1.0}]}
    assert client.post("/orders", json=body).status_code == 422


def test_negative_unit_price_rejected(client: TestClient) -> None:
    body = {"items": [{"product_id": "A", "qty": 1, "unit_price": -0.01}]}
    assert client.post("/orders", json=body).status_code == 422


def test_rejected_order_is_not_persisted(client: TestClient) -> None:
    client.post("/orders", json={"items": []})
    assert client.get("/orders").json() == []


def test_list_orders_returns_all_in_id_order(client: TestClient) -> None:
    assert client.get("/orders").json() == []
    ids = [create(client, [{"product_id": f"P{n}", "qty": n, "unit_price": 1.0}])["id"]
           for n in (1, 2, 3)]
    listed = client.get("/orders").json()
    assert [o["id"] for o in listed] == sorted(ids)
    assert [o["total"] for o in listed] == pytest.approx([1.0, 2.0, 3.0])


def test_list_orders_filters_by_status(client: TestClient) -> None:
    create(client, [{"product_id": "A", "qty": 1, "unit_price": 1.0}])
    create(client, [{"product_id": "B", "qty": 1, "unit_price": 2.0}])
    assert len(client.get("/orders", params={"status": "pending"}).json()) == 2
    assert client.get("/orders", params={"status": "paid"}).json() == []


def test_list_orders_rejects_unknown_status(client: TestClient) -> None:
    assert client.get("/orders", params={"status": "lost"}).status_code == 422


# --- v1.1: note ------------------------------------------------------------------------------


@pytest.mark.parametrize("note", ["Leave at the door", "Ring twice — gate code 1234"])
def test_note_is_stored_and_returned(client: TestClient, note: str) -> None:
    created = create(client, [{"product_id": "N", "qty": 1, "unit_price": 1.0}], note=note)
    assert created["note"] == note
    assert client.get(f"/orders/{created['id']}").json()["note"] == note


def test_note_longer_than_200_chars_rejected(client: TestClient) -> None:
    body = {"items": [{"product_id": "N", "qty": 1, "unit_price": 1.0}], "note": "x" * 201}
    assert client.post("/orders", json=body).status_code == 422


# --- v1.1: cancellation ----------------------------------------------------------------------


def test_cancel_sets_status_and_persists(client: TestClient) -> None:
    created = create(client, [{"product_id": "C", "qty": 2, "unit_price": 3.0}], note="n")
    response = client.post(f"/orders/{created['id']}/cancel")
    assert response.status_code == 200
    cancelled = response.json()
    assert cancelled["status"] == "cancelled"
    assert {k: v for k, v in cancelled.items() if k != "status"} == {
        k: v for k, v in created.items() if k != "status"
    }
    assert client.get(f"/orders/{created['id']}").json()["status"] == "cancelled"


def test_cancel_only_affects_that_order(client: TestClient) -> None:
    keep = create(client, [{"product_id": "K", "qty": 1, "unit_price": 1.0}])
    drop = create(client, [{"product_id": "D", "qty": 1, "unit_price": 1.0}])
    client.post(f"/orders/{drop['id']}/cancel")
    assert client.get(f"/orders/{keep['id']}").json()["status"] == "pending"
    cancelled = client.get("/orders", params={"status": "cancelled"}).json()
    assert [o["id"] for o in cancelled] == [drop["id"]]


def test_cancel_twice_returns_409(client: TestClient) -> None:
    created = create(client, [{"product_id": "C", "qty": 1, "unit_price": 1.0}])
    assert client.post(f"/orders/{created['id']}/cancel").status_code == 200
    response = client.post(f"/orders/{created['id']}/cancel")
    assert response.status_code == 409
    assert isinstance(response.json()["detail"], str)


def test_cancel_missing_order_returns_404(client: TestClient) -> None:
    assert client.post("/orders/424242/cancel").status_code == 404
