"""Deliberately weak suite: status codes only. The quality gates must reject it."""

from fastapi.testclient import TestClient

ITEMS = [{"product_id": "SKU-001", "qty": 2, "unit_price": 9.5}]


def test_create_order(client: TestClient) -> None:
    assert client.post("/orders", json={"items": ITEMS}).status_code == 201


def test_get_order(client: TestClient) -> None:
    client.post("/orders", json={"items": ITEMS})
    assert client.get("/orders/1").status_code == 200


def test_list_orders(client: TestClient) -> None:
    assert client.get("/orders").status_code == 200


def test_empty_items_rejected(client: TestClient) -> None:
    assert client.post("/orders", json={"items": []}).status_code == 422
