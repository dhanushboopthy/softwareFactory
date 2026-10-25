"""Database setup (SQLAlchemy 2.0 + SQLite) and ORM models."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import ForeignKey, String, create_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./app.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(20))
    total: Mapped[float]
    created_at: Mapped[datetime]
    items: Mapped[list["OrderItemRow"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderItemRow.position",
        lazy="selectin",
    )


class OrderItemRow(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    position: Mapped[int]
    product_id: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int]
    unit_price: Mapped[float]
    order: Mapped[OrderRow] = relationship(back_populates="items")


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
