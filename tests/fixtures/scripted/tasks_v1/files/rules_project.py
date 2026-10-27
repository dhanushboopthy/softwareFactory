"""Reference rules for the tasks sample (app/rules/project.py).

Used as the scripted coder's final answer and to check the generated project end to end.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models, orm
from app.errors import RuleViolation

MAX_ACTIVE_PROJECTS = 20


def before_create(session: Session, user: orm.User, data: models.ProjectCreate) -> None:
    """A user may have at most 20 non-archived projects."""
    if data.archived:
        return  # an archived project does not count towards the limit
    active = session.scalar(
        select(func.count())
        .select_from(orm.Project)
        .where(orm.Project.owner_id == user.id, orm.Project.archived.is_(False))
    )
    if (active or 0) >= MAX_ACTIVE_PROJECTS:
        raise RuleViolation(
            "project-limit", f"at most {MAX_ACTIVE_PROJECTS} active projects per user"
        )


def before_update(
    session: Session, user: orm.User, row: orm.Project, data: models.ProjectUpdate
) -> None:
    """No rule applies to updates."""
    return None


def before_delete(session: Session, user: orm.User, row: orm.Project) -> None:
    """No rule applies to deletes."""
    return None


def archive(session: Session, user: orm.User, row: orm.Project) -> orm.Project:
    """Marks the project archived; archiving twice is a conflict."""
    if row.archived:
        raise RuleViolation("already-archived", "the project is already archived")
    row.archived = True
    return row
