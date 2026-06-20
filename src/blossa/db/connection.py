# Copyright (c) 2026 Bogdan Voinea
# SPDX-License-Identifier: AGPL-3.0-only

"""Read-only Oracle connection wrapper.

Uses python-oracledb in **thin mode** (no Oracle Instant Client required). The first thing
we do on every connection is open a read-only transaction (`SET TRANSACTION READ ONLY`), so
the database itself rejects any accidental DML/DDL Blossa might issue — a hard guarantee that
backs up the "read-only, always" rule, not just a convention in our code.
"""

from __future__ import annotations

from typing import Any, Protocol

import oracledb

from ..config import OracleConfig


class QueryExecutor(Protocol):
    """Minimal interface the introspector / profiler need. Lets us swap in fakes for tests."""

    def query(self, sql: str, binds: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...


class Database:
    """A read-only Oracle connection that returns rows as dicts keyed by upper-case column name."""

    def __init__(self, config: OracleConfig):
        self._config = config
        self._conn: oracledb.Connection | None = None

    def connect(self) -> Database:
        # Thin mode is the default in python-oracledb 2.x; we never init the thick client.
        self._conn = oracledb.connect(
            user=self._config.user,
            password=self._config.password,
            dsn=self._config.dsn,
        )
        # Belt-and-braces: make the session itself read-only for this transaction.
        with self._conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
        return self

    @property
    def effective_schema(self) -> str:
        """The owner whose objects we introspect — explicit config, else the connecting user."""
        return (self._config.schema_name or self._config.user).upper()

    def query(self, sql: str, binds: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be called before query().")
        with self._conn.cursor() as cur:
            cur.execute(sql, binds or {})
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Database:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()
