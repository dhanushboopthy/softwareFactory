"""A coder that skips the rules: every hook accepts and the action does nothing."""

from sqlalchemy.orm import Session

from app import models, orm


def before_create(session: Session, user: orm.User, data: models.ProjectCreate) -> None:
    return None


def before_update(
    session: Session, user: orm.User, row: orm.Project, data: models.ProjectUpdate
) -> None:
    return None


def before_delete(session: Session, user: orm.User, row: orm.Project) -> None:
    return None


def archive(session: Session, user: orm.User, row: orm.Project) -> orm.Project:
    return row
