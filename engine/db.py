"""Connections, server information and cancel support; database management follows in Step 4."""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from .util import EngineError

log = logging.getLogger(__name__)

MIN_SERVER_VERSION = 120000
APPLICATION_NAME = "PG Backup Studio"


class UnsupportedServerError(EngineError):
    """The server is older than PostgreSQL 12."""


def connect(server: dict, dbname: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a connection described by a saved-server dict.

    Keys: host, port, user, password, sslmode, maintenance_db. Without `dbname` the
    maintenance DB (default `postgres`) is used. Auto-prepare is off so repeated
    statements never turn into server-side prepared statements.
    """
    host = server.get("host") or "localhost"
    port = int(server.get("port") or 5432)
    db = dbname or server.get("maintenance_db") or "postgres"
    params = {
        "host": host,
        "port": port,
        "user": server.get("user") or None,
        "password": server.get("password") or None,
        "dbname": db,
        "sslmode": server.get("sslmode") or "prefer",
        "connect_timeout": 10,
        "application_name": APPLICATION_NAME,
    }
    try:
        return psycopg.connect(autocommit=autocommit, prepare_threshold=None,
                               **{k: v for k, v in params.items() if v is not None})
    except psycopg.OperationalError as e:
        raise EngineError(f"Could not connect to {host}:{port}, database {db!r}: {_reason(e)}") from e


def _reason(e: Exception) -> str:
    """Short reason from a libpq connection error (drops the per-address prefixes)."""
    text = " ".join(str(e).split())
    if "FATAL:" in text:
        return text.rsplit("FATAL:", 1)[1].strip()
    if " Multiple connection attempts failed" in text:
        text = text.split(" Multiple connection attempts failed", 1)[0]
    return text.rsplit("failed: ", 1)[-1].strip() or "unknown error"


def server_info(conn: psycopg.Connection) -> dict:
    """Version, current user, superuser flag, database, host, port and encoding."""
    row = conn.execute(
        "SELECT current_setting('server_version_num')::int, current_setting('server_version'),"
        " current_user, (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user),"
        " current_database(), current_setting('server_encoding')").fetchone()
    return {
        "version_num": row[0], "version": row[1], "user": row[2], "is_superuser": bool(row[3]),
        "database": row[4], "host": conn.info.host, "port": conn.info.port, "encoding": row[5],
    }


def require_supported(conn: psycopg.Connection) -> int:
    """Return `server_version_num`; raise `UnsupportedServerError` below PostgreSQL 12."""
    version = conn.info.server_version
    if version < MIN_SERVER_VERSION:
        major = version // 10000 if version >= 100000 else f"{version // 10000}.{version // 100 % 100}"
        raise UnsupportedServerError(
            f"PostgreSQL {major} is not supported; PG Backup Studio needs PostgreSQL 12 or later.")
    return version


@contextmanager
def cancel_on_event(conn: psycopg.Connection, ev: threading.Event | None, poll: float = 0.2) -> Iterator[None]:
    """While the block runs, cancel the connection's current statement as soon as `ev` is set.

    A daemon thread polls the event and calls `conn.cancel_safe()`, which ends a lock wait or a
    long statement with QueryCanceled; the caller turns that into `Cancelled`. The watcher fires
    once and is stopped when the block ends.
    """
    if ev is None:
        yield
        return
    stop = threading.Event()

    def watch() -> None:
        while not stop.wait(poll):
            if ev.is_set():
                try:
                    conn.cancel_safe(timeout=5)
                except Exception:  # noqa: BLE001 - the event is still checked by the worker
                    log.exception("cancel request failed")
                return

    watcher = threading.Thread(target=watch, name="pgbs-cancel-watcher", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=6)
