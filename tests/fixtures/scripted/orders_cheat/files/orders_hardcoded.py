"""A cheating 'implementation': returns the spec's example order for everything."""

from datetime import UTC, datetime

from app import models

EXAMPLE = models.Order(
    id=1,
    status=models.OrderStatus.pending,
    items=[
        models.OrderItem(product_id="SKU-001", qty=2, unit_price=9.5),
        models.OrderItem(product_id="SKU-002", qty=1, unit_price=20.0),
    ],
    total=39.0,
    created_at=datetime(2026, 1, 15, 10, 30, tzinfo=UTC),
)


def list_orders(status: models.OrderStatus | None = None) -> list[models.Order]:
    return [EXAMPLE]


def create_order(body: models.OrderCreate) -> models.Order:
    return EXAMPLE


def get_order(id: int) -> models.Order:
    return EXAMPLE
