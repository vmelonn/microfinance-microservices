"""
A very small database dialect shim, so auth-service and ledger-service can
each run on Postgres in a cluster and SQLite on a laptop.

WHY NOT AN ORM: the SQL in this platform is a dozen statements, most of them
inserts and one aggregate. SQLAlchemy would be more code to configure than
to replace, and it would hide the exact statement being issued -- which
matters here, because ledger-service's correctness rests on a specific
PRIMARY KEY conflict being raised and caught, not on an abstraction over it.

WHY NOT JUST POSTGRES: unit tests should not need a container. The whole
test suite runs in about ten seconds against SQLite; requiring Postgres to
assert that a double-entry posting balances would make the fast tests slow
and the slow tests skipped.

WHAT THIS DELIBERATELY DOES NOT DO: hide the differences that actually
matter. Placeholder style and a few DDL type names are mechanical and safe
to translate. Transaction isolation, row locking, and conflict behaviour are
NOT, and each service handles those explicitly. A shim that pretended SQLite
and Postgres had the same concurrency semantics would be actively dangerous
in a ledger.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

POSTGRES = "postgres"
SQLITE = "sqlite"


def utc_now_param() -> str:
    """
    The current UTC time, as an ISO 8601 string, for binding as a query
    parameter.

    A string rather than a datetime, for two reasons:

    1. Python 3.12 deprecated sqlite3's implicit datetime adapter and it is
       scheduled for removal. Passing a datetime object works today and
       raises later.
    2. Postgres casts a valid ISO 8601 string to TIMESTAMPTZ correctly, so
       one representation serves both engines and there is no per-dialect
       branch to get wrong.

    Always offset-aware. A naive timestamp written to a TIMESTAMPTZ column is
    interpreted in the server's timezone, which is how the monolith's
    warehouse sync ended up re-processing the same rows forever -- the
    offset was silently dropped and comparisons stopped matching.
    """
    return datetime.now(timezone.utc).isoformat()

# Rewrites ? placeholders to %s, while leaving ? inside string literals
# alone. Naive .replace("?", "%s") corrupts any statement containing a
# literal question mark.
_PLACEHOLDER = re.compile(r"\?(?=(?:[^']*'[^']*')*[^']*$)")


class Database:
    """
    dsn is either a postgresql:// URL or a filesystem path for SQLite.

    Detection is on the scheme rather than a separate config flag, because
    one variable that cannot disagree with itself beats two that can.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.dialect = POSTGRES if dsn.startswith(("postgresql://", "postgres://")) else SQLITE

    @property
    def is_postgres(self) -> bool:
        return self.dialect == POSTGRES

    def sql(self, statement: str) -> str:
        """Translate ?-style placeholders for the active driver."""
        return _PLACEHOLDER.sub("%s", statement) if self.is_postgres else statement

    def connect(self):
        if self.is_postgres:
            import psycopg2

            return psycopg2.connect(self.dsn)

        conn = sqlite3.connect(self.dsn, timeout=15)
        # Foreign keys are OFF by default in SQLite, which would let a
        # ledger entry reference an account that does not exist -- the exact
        # class of corruption the schema's REFERENCES clauses exist to
        # prevent. Postgres enforces them unconditionally.
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets readers proceed during a write. Without it, concurrent
        # test threads hitting the same file serialise into "database is
        # locked" errors that look like application bugs.
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def transaction(self):
        """
        Commits on success, rolls back on any exception, always closes.

        Explicit rather than relying on sqlite3's connection context manager,
        because that one commits but does NOT close, and psycopg2's has
        different semantics again. Two drivers behaving differently under
        `with conn:` is precisely the kind of difference worth removing.
        """
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def cursor(self):
        """Read-only convenience: yields a cursor, never commits."""
        conn = self.connect()
        try:
            yield conn.cursor()
        finally:
            conn.close()

    # -- dialect-specific fragments ----------------------------------------

    @property
    def autoincrement_pk(self) -> str:
        return "BIGSERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    @property
    def timestamp_type(self) -> str:
        # TIMESTAMPTZ, not TIMESTAMP. A plain Postgres TIMESTAMP silently
        # discards the UTC offset, which is exactly the bug the monolith's
        # README documents finding in its warehouse sync: timestamps compared
        # across engines stopped matching and every sync re-processed the
        # same rows forever.
        return "TIMESTAMPTZ" if self.is_postgres else "TIMESTAMP"

    def is_unique_violation(self, exc: Exception, constraint_hint: str = "") -> bool:
        """
        True only for a duplicate-key violation, NOT for a foreign-key one.

        This distinction is load-bearing in ledger-service. SQLite raises the
        same IntegrityError for both, and treating a foreign-key violation
        as "already recorded" would silently swallow a posting against a
        nonexistent account -- reporting success while the money went
        nowhere. Postgres separates them by SQLSTATE (23505 vs 23503);
        SQLite has to be told apart by message text.
        """
        if self.is_postgres:
            return getattr(exc, "pgcode", None) == "23505"

        message = str(exc)
        if "UNIQUE constraint failed" not in message:
            return False
        return constraint_hint in message if constraint_hint else True
