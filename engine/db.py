"""Connections, server information, cancel support and database management (Step 4)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import errors, sql
from psycopg.rows import dict_row

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


# ---------------------------------------------------------------- database management (Step 4)

MAX_NAME_BYTES = 63
TEMPLATES = ("template0", "template1")
RETRIES = 3
RETRY_DELAY_S = 0.5


class DbActionError(EngineError):
    """A database action was refused. `code`: invalid | not_found | exists | protected | permission | in_use."""

    def __init__(self, message: str, code: str = "invalid"):
        self.code = code
        super().__init__(message)


def connect_maintenance(server: dict) -> psycopg.Connection:
    """Autocommit connection to the configured maintenance DB, else `postgres`, else `template1`.

    Moves to the next name only when the database does not exist (3D000).
    """
    names = list(dict.fromkeys(n for n in (server.get("maintenance_db"), "postgres", "template1") if n))
    for i, name in enumerate(names):
        try:
            return connect(server, name, autocommit=True)
        except EngineError as e:
            if not _missing_database(e) or i == len(names) - 1:
                raise
    raise AssertionError("unreachable")


_MISSING_DB = re.compile(r'database ".*" does not exist')


def _missing_database(e: EngineError) -> bool:
    """Did `connect` fail because the database does not exist (3D000)?

    When libpq tries several addresses (localhost = ::1 and 127.0.0.1) psycopg re-raises a combined
    error without the SQLSTATE, so the server's message is checked as well.
    """
    cause = e.__cause__
    return getattr(cause, "sqlstate", None) == "3D000" or bool(cause is not None and _MISSING_DB.search(str(cause)))


def check_name(name, what: str = "database name") -> str:
    """Refuse names PostgreSQL would reject or silently truncate (more than 63 bytes)."""
    if not isinstance(name, str) or not name:
        raise DbActionError(f"Enter a {what}.")
    if "\x00" in name:
        raise DbActionError(f"The {what} cannot contain a NUL character.")
    size = len(name.encode("utf-8"))
    if size > MAX_NAME_BYTES:
        raise DbActionError(f"The {what} is too long: at most {MAX_NAME_BYTES} bytes ({size} given).")
    return name


def protected_names(conn: psycopg.Connection) -> set[str]:
    """Databases that may not be dropped or renamed: the templates and the maintenance DB in use."""
    return {*TEMPLATES, conn.info.dbname}


_DATABASES_SQL = """
SELECT d.datname AS name, pg_catalog.pg_get_userbyid(d.datdba) AS owner,
       pg_catalog.pg_encoding_to_char(d.encoding) AS encoding, d.datcollate AS collation,
       CASE WHEN pg_catalog.has_database_privilege(d.oid, 'CONNECT')
            THEN pg_catalog.pg_database_size(d.oid) END AS size,
       (SELECT count(*) FROM pg_catalog.pg_stat_activity a
        WHERE a.datid = d.oid AND a.pid <> pg_catalog.pg_backend_pid())::int AS connections,
       d.datallowconn AS allow_connections
FROM pg_catalog.pg_database d
WHERE NOT d.datistemplate {extra}
ORDER BY d.datname
"""


def _database_rows(conn: psycopg.Connection, name: str | None = None) -> list[dict]:
    extra, params = ("AND d.datname = %s", (name,)) if name is not None else ("", ())
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(_DATABASES_SQL.format(extra=extra), params).fetchall()
    protected = protected_names(conn)
    for r in rows:
        r["protected"] = r["name"] in protected
    return rows


def list_databases(server: dict) -> list[dict]:
    """Non-template databases with owner, encoding, size (None without CONNECT) and other sessions."""
    with _maintenance(server, "list the databases") as conn:
        return _database_rows(conn)


def list_roles(server: dict) -> list[dict]:
    """Roles to choose an owner from; predefined `pg_*` roles are left out."""
    with _maintenance(server, "list the roles") as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT rolname AS name, rolsuper AS superuser, rolcanlogin AS can_login,"
                           " rolcreatedb AS createdb FROM pg_catalog.pg_roles WHERE rolname !~ '^pg_'"
                           " ORDER BY rolname").fetchall()


def list_templates(server: dict) -> list[str]:
    """Template databases, template0 first."""
    with _maintenance(server, "list the templates") as conn:
        rows = conn.execute("SELECT datname FROM pg_catalog.pg_database WHERE datistemplate"
                            " ORDER BY datname <> 'template0', datname").fetchall()
    return [r[0] for r in rows]


def database_sessions(server: dict, name: str) -> int:
    """Sessions on `name` other than the app's own (what kill / drop / clone would disconnect)."""
    with _maintenance(server, f"count the sessions on {name}") as conn:
        return _other_sessions(conn, name)


_CONTENTS_SQL = """
WITH ext AS (SELECT classid, objid FROM pg_catalog.pg_depend WHERE deptype = 'e'),
     ns AS (SELECT oid, nspname FROM pg_catalog.pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema') AND nspname !~ '^pg_(toast|temp_)')
SELECT
  (SELECT count(*) FROM ns WHERE ns.nspname <> 'public'
     AND NOT EXISTS (SELECT 1 FROM ext WHERE ext.classid = 'pg_namespace'::regclass AND ext.objid = ns.oid))::int,
  (SELECT count(*) FROM pg_catalog.pg_class c WHERE c.relnamespace IN (SELECT oid FROM ns)
     AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
     AND NOT EXISTS (SELECT 1 FROM ext WHERE ext.classid = 'pg_class'::regclass AND ext.objid = c.oid))::int,
  (SELECT count(*) FROM pg_catalog.pg_proc p WHERE p.pronamespace IN (SELECT oid FROM ns)
     AND NOT EXISTS (SELECT 1 FROM ext WHERE ext.classid = 'pg_proc'::regclass AND ext.objid = p.oid))::int,
  (SELECT count(*) FROM pg_catalog.pg_type t WHERE t.typnamespace IN (SELECT oid FROM ns)
     AND (t.typtype IN ('d', 'e', 'r')
          OR (t.typtype = 'c' AND (SELECT c.relkind FROM pg_catalog.pg_class c WHERE c.oid = t.typrelid) = 'c'))
     AND NOT EXISTS (SELECT 1 FROM ext WHERE ext.classid = 'pg_type'::regclass AND ext.objid = t.oid))::int
"""


def database_contents(server: dict, name: str) -> dict:
    """User objects in `name`. `empty` ignores schemas and extensions (a backup creates them IF NOT EXISTS)."""
    try:
        conn = connect(server, name, autocommit=True)
    except EngineError as e:
        if _missing_database(e):
            raise DbActionError(f"Database {name} does not exist.", "not_found") from e
        raise
    try:
        schemas, tables, functions, types = conn.execute(_CONTENTS_SQL).fetchone()
    except psycopg.Error as e:
        raise EngineError(f"Could not inspect {name}: {_primary(e)}") from e
    finally:
        conn.close()
    return {"empty": tables + functions + types == 0, "schemas": schemas, "tables": tables,
            "functions": functions, "types": types}


def create_database(server: dict, name: str, *, owner: str | None = None, template: str | None = None,
                    encoding: str | None = None) -> dict:
    """CREATE DATABASE with optional owner, template and encoding; returns the new list row."""
    check_name(name)
    with _maintenance(server, f"create {name}") as conn:
        query = _create_sql(conn, name, owner=owner, template=template, encoding=encoding)
        _run(conn, query, "create", name, owner=owner, new_name=name, busy_db=template or "template1")
        return _database_rows(conn, name)[0]


def _create_sql(conn: psycopg.Connection, name: str, *, owner=None, template=None, encoding=None) -> sql.Composed:
    parts = [sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))]
    if owner:
        parts.append(sql.SQL("OWNER {}").format(sql.Identifier(owner)))
    if template:
        parts.append(sql.SQL("TEMPLATE {}").format(sql.Identifier(template)))
    if encoding:
        if not isinstance(encoding, str) or not conn.execute(
                "SELECT pg_catalog.pg_char_to_encoding(%s) >= 0", (encoding,)).fetchone()[0]:
            raise DbActionError(f"Unknown encoding {encoding!r}.")
        parts.append(sql.SQL("ENCODING {}").format(sql.Literal(encoding)))
    return sql.SQL(" ").join(parts)


def drop_database(server: dict, name: str, *, confirm: str | None) -> dict:
    """Drop `name` once the caller typed its exact name; other sessions are terminated first."""
    if confirm != name:
        raise DbActionError(f"Type the database name ({name}) exactly to confirm the drop.")
    with _maintenance(server, f"drop {name}") as conn:
        _require_unprotected(conn, name, "dropped")
        _require_exists(conn, name)
        killed = _terminate(conn, name)
        force = sql.SQL(" WITH (FORCE)") if conn.info.server_version >= 130000 else sql.SQL("")
        _run(conn, sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)) + force, "drop", name, busy_db=name)
        return {"name": name, "terminated": killed["terminated"]}


def rename_database(server: dict, name: str, new_name: str) -> dict:
    """ALTER DATABASE ... RENAME TO after terminating other sessions; returns the renamed list row."""
    check_name(new_name, "new database name")
    with _maintenance(server, f"rename {name}") as conn:
        _require_unprotected(conn, name, "renamed")
        _require_exists(conn, name)
        query = sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(name), sql.Identifier(new_name))
        _with_terminate(conn, name, query, "rename", new_name=new_name)
        return _database_rows(conn, new_name)[0]


def clone_database(server: dict, source: str, new_name: str, *, owner: str | None = None) -> dict:
    """CREATE DATABASE new TEMPLATE source; sessions on the source are terminated (it must be idle)."""
    check_name(new_name, "new database name")
    with _maintenance(server, f"clone {source}") as conn:
        _require_exists(conn, source, templates=True)
        query = _create_sql(conn, new_name, owner=owner, template=source)
        _with_terminate(conn, source, query, "clone", owner=owner, new_name=new_name)
        return _database_rows(conn, new_name)[0]


def kill_connections(server: dict, name: str) -> dict:
    """Terminate every other session on `name`; sessions the login may not end are counted in `failed`."""
    with _maintenance(server, f"end the sessions on {name}") as conn:
        _require_exists(conn, name, templates=True)
        return {"name": name, **_terminate(conn, name)}


# ---------------------------------------------------------------- helpers

@contextmanager
def _maintenance(server: dict, action: str) -> Iterator[psycopg.Connection]:
    conn = connect_maintenance(server)
    try:
        require_supported(conn)
        yield conn
    except EngineError:
        raise
    except psycopg.Error as e:
        raise EngineError(f"Could not {action}: {_primary(e)}") from e
    finally:
        conn.close()


def _primary(e: psycopg.Error) -> str:
    diag = getattr(e, "diag", None)
    return (diag.message_primary if diag else None) or " ".join(str(e).split()) or type(e).__name__


def _other_sessions(conn: psycopg.Connection, name: str) -> int:
    return conn.execute("SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname = %s"
                        " AND pid <> pg_catalog.pg_backend_pid()", (name,)).fetchone()[0]


def _require_exists(conn: psycopg.Connection, name: str, templates: bool = False) -> None:
    row = conn.execute("SELECT datistemplate FROM pg_catalog.pg_database WHERE datname = %s", (name,)).fetchone()
    if row is None or (row[0] and not templates):
        raise DbActionError(f"Database {name} does not exist.", "not_found")


def _require_unprotected(conn: psycopg.Connection, name: str, verb: str) -> None:
    if name in protected_names(conn):
        why = "the app connects through it" if name == conn.info.dbname else "new databases are created from it"
        raise DbActionError(f"Database {name} cannot be {verb}: {why}.", "protected")


def _terminate(conn: psycopg.Connection, name: str) -> dict:
    """pg_terminate_backend for each other session; a refusal (another role's session) counts as failed."""
    pids = [r[0] for r in conn.execute("SELECT pid FROM pg_catalog.pg_stat_activity WHERE datname = %s"
                                       " AND pid <> pg_catalog.pg_backend_pid()", (name,)).fetchall()]
    done = failed = 0
    for pid in pids:
        try:
            ok = conn.execute("SELECT pg_catalog.pg_terminate_backend(%s)", (pid,)).fetchone()[0]
        except errors.InsufficientPrivilege:
            ok = False
        if ok:
            done += 1
        else:
            failed += 1
    return {"terminated": done, "failed": failed}


def _with_terminate(conn: psycopg.Connection, name: str, query: sql.Composable, action: str, **kw) -> None:
    """Terminate other sessions on `name` and run `query`; retry when a session reconnects in between."""
    for attempt in range(RETRIES):
        _terminate(conn, name)
        try:
            _run(conn, query, action, name, busy_db=name, **kw)
            return
        except DbActionError as e:
            if e.code != "in_use" or attempt == RETRIES - 1:
                raise
        time.sleep(RETRY_DELAY_S)


_ACTION_TEXT = {"create": "create", "drop": "drop", "rename": "rename", "clone": "clone"}


def _run(conn: psycopg.Connection, query: sql.Composable, action: str, name: str, *, owner: str | None = None,
         new_name: str | None = None, busy_db: str | None = None) -> None:
    """Execute one DDL statement; server errors become DbActionError with a clear message.

    `name` is the database acted on, `new_name` the one created / renamed to, `busy_db` the one that
    must have no other sessions.
    """
    verb = _ACTION_TEXT[action]
    try:
        conn.execute(query)
    except errors.DuplicateDatabase as e:
        raise DbActionError(f"A database named {new_name or name} already exists.", "exists") from e
    except errors.InvalidCatalogName as e:
        raise DbActionError(f"{_primary(e)}.", "not_found") from e
    except errors.InsufficientPrivilege as e:
        raise DbActionError(f"{conn.info.user} may not {verb} {name}: {_primary(e)}.", "permission") from e
    except errors.UndefinedObject as e:
        raise DbActionError(f"Role {owner} does not exist." if owner else f"{_primary(e)}.") from e
    except errors.ObjectInUse as e:
        db = busy_db or name
        n = _other_sessions(conn, db)
        raise DbActionError(f"Could not {verb} {name}: {db} is still used by {n} other session(s) (they keep "
                            f"reconnecting, or this login may not end them). Stop the application that uses "
                            f"it and try again.", "in_use") from e
    except (errors.InvalidParameterValue, errors.WrongObjectType, errors.FeatureNotSupported) as e:
        raise DbActionError(f"{_primary(e)}. To use another encoding or locale, choose template0 as the "
                            f"template.") from e
    except psycopg.Error as e:
        raise EngineError(f"Could not {verb} {name}: {_primary(e)}") from e
