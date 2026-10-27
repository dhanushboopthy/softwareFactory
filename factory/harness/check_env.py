"""The environment that checks run in under the entities profile (R5, R9).

One Postgres server per run holds a template database that `alembic upgrade head` has already
migrated. Every check run clones its own database from it, so checks never share rows and the real
migration is exercised once per run. The JWT secret is random per run and only ever travels in the
environment of the processes that need it.
"""

from __future__ import annotations

import itertools
import secrets
from dataclasses import dataclass, field

from factory.harness.postgres import PostgresServer, safe_db_name

# Low PBKDF2 cost for checks: hashing at the production default would dominate the pytest timeout
# (OV#18). Production keeps the default in app/settings.py.
CHECK_HASH_ITERATIONS = 1000


@dataclass
class CheckEnv:
    server: PostgresServer
    template_db: str
    jwt_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    hash_iterations: int = CHECK_HASH_ITERATIONS
    _counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1), repr=False)

    def fresh_db(self, label: str) -> str:
        """A new database cloned from the migrated template; returns its name."""
        name = safe_db_name(f"{safe_db_name(label)}_{next(self._counter)}")
        self.server.create_database(name, template=self.template_db)
        return name

    def env_for(self, database: str) -> dict[str, str]:
        """The variables the generated app reads (settings.py) for ``database``."""
        return {
            "DATABASE_URL": self.server.url(database),
            "JWT_SECRET": self.jwt_secret,
            "PASSWORD_HASH_ITERATIONS": str(self.hash_iterations),
        }

    def fresh_env(self, label: str) -> dict[str, str]:
        """Shorthand: a new database and the environment pointing at it."""
        return self.env_for(self.fresh_db(label))
