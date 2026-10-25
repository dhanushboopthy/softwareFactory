"""Business logic for the `orders` tag."""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select

from app import models
from app.db import OrderItemRow, OrderRow, session_scope


def _to_model(row: OrderRow) -> models.Order:
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return models.Order(
        id=row.id,
        status=models.OrderStatus(row.status),
        items=[
            models.OrderItem(product_id=i.product_id, qty=i.qty, unit_price=i.unit_price)
            for i in row.items
        ],
        total=row.total,
        created_at=created,
    )


def list_orders(status: models.OrderStatus | None = None) -> list[models.Order]:
    with session_scope() as session:
        stmt = select(OrderRow).order_by(OrderRow.id)
        if status is not None:
            stmt = stmt.where(OrderRow.status == status.value)
        return [_to_model(row) for row in session.scalars(stmt)]


def create_order(body: models.OrderCreate) -> models.Order:
    with session_scope() as session:
        row = OrderRow(
            status=models.OrderStatus.pending.value,
            total=sum(item.qty * item.unit_price for item in body.items),
            created_at=datetime.now(UTC),
            items=[
                OrderItemRow(
                    position=n, product_id=i.product_id, qty=i.qty, unit_price=i.unit_price
                )
                for n, i in enumerate(body.items)
            ],
        )
        session.add(row)
        session.flush()
        return _to_model(row)


def get_order(id: int) -> models.Order:
    with session_scope() as session:
        row = session.get(OrderRow, id)
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found")
        return _to_model(row)
