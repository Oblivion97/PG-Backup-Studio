"""SQL console sessions: one open connection per query tab (ARCHITECTURE 14, decision 009).

Statements run as typed (full access). Each `execute` splits the text with the restore
splitter and runs the statements one by one, stopping at the first error. Values are returned
as PostgreSQL's own text output (what psql shows), so they can be edited and sent back with a
cast to the column type.

Large results: a SELECT / WITH / VALUES / TABLE statement is read through a server-side cursor
(`DECLARE ... ; FETCH max_rows + 1`), so a query over a huge table never loads more than the
row cap into this process. When DECLARE refuses the statement (SELECT INTO, data-modifying
WITH, ...) it is run normally instead; DECLARE fails before anything runs, so nothing happens
twice.

Transactions: in auto-commit mode every statement commits on its own. In manual mode the
session stays in one transaction until Commit or Rollback, and each statement runs inside a
savepoint, so an error undoes only the failing statement and the transaction stays usable.
"""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field

import psycopg
from psycopg import errors, sql
from psycopg.pq import TransactionStatus

from .db import connect
from .restore import Statement, split_sql
from .util import EngineError, ident, qname

log = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 1000
MAX_ROWS_LIMIT = 10000
MAX_TIMEOUT_S = 24 * 3600
IDLE_TIMEOUT_S = 30 * 60
MAX_SESSIONS = 16
MAX_NOTICES = 200
MAX_EDITS = 5000
MODES = ("auto", "manual")
CURSOR = "pgbs_console_cursor"
SAVEPOINT = "pgbs_console_sp"

_FIRST_WORD = re.compile(r"\s*\(*\s*([A-Za-z]+)")
_LITERALS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/|\$([A-Za-z_]\w*)?\$.*?\$\1\$", re.S)
_ROW_QUERIES = {"SELECT", "WITH", "VALUES", "TABLE"}
_TX_CONTROL = {"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE", "PREPARE"}
_TX_STATUS = {TransactionStatus.IDLE: "idle", TransactionStatus.INTRANS: "in_transaction",
              TransactionStatus.INERROR: "failed", TransactionStatus.ACTIVE: "active"}


class ConsoleError(EngineError):
    """A console request was refused. `code`: invalid | not_found | busy | limit | session_lost."""

    def __init__(self, message: str, code: str = "invalid"):
        self.code = code
        super().__init__(message)


def first_word(sql_text: str) -> str:
    m = _FIRST_WORD.match(sql_text)
    return m.group(1).upper() if m else ""


def _strip_literals(text: str) -> str:
    """`text` without its quoted strings, quoted names and comments (to look for a stray ';')."""
    return _LITERALS.sub(" ", text)


@dataclass
class Session:
    id: str
    server_id: str
    database: str
    conn: psycopg.Connection
    mode: str = "auto"
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.monotonic)
    running: bool = False
    timeout_applied: float | None = None
    types: dict = field(default_factory=dict)
    notices: list = field(default_factory=list)

    def tx_status(self) -> str:
        if self.conn.closed:
            return "closed"
        return _TX_STATUS.get(self.conn.info.transaction_status, "unknown")

    def state(self) -> dict:
        return {"session_id": self.id, "mode": self.mode, "tx_status": self.tx_status(),
                "server_id": self.server_id, "database": self.database}


class ConsoleManager:
    """Open console sessions, keyed by a random id. Thread-safe."""

    def __init__(self, idle_timeout_s: float = IDLE_TIMEOUT_S, max_sessions: int = MAX_SESSIONS, connect_fn=None):
        self.idle_timeout_s = idle_timeout_s
        self.max_sessions = max_sessions
        self._connect = connect_fn or connect
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # -------------------------------------------------- lifecycle

    def open(self, server: dict, database: str) -> dict:
        self.reap()
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise ConsoleError(f"At most {self.max_sessions} query tabs can be open at once; close one first.",
                                   "limit")
        conn = self._connect(server, database, autocommit=True)
        s = Session(secrets.token_hex(12), server.get("id") or "", database, conn)
        conn.add_notice_handler(lambda d, s=s: self._notice(s, d))
        with self._lock:
            self._sessions[s.id] = s
        return s.state()

    @staticmethod
    def _notice(s: Session, diag) -> None:
        if len(s.notices) < MAX_NOTICES:
            s.notices.append({"severity": diag.severity or "NOTICE", "message": diag.message_primary or ""})

    def get(self, session_id: str) -> Session:
        with self._lock:
            s = self._sessions.get(session_id)
        if s is None:
            raise ConsoleError("This query tab's connection was closed (idle for too long or the app restarted); "
                               "it reconnects on the next run.", "session_lost")
        return s

    def close(self, session_id: str) -> bool:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s is None:
            return False
        self._close_conn(s)
        return True

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            self._close_conn(s)

    def close_for(self, server_id: str, database: str | None = None) -> int:
        """Close the sessions of a server (or one of its databases), e.g. before dropping that database."""
        with self._lock:
            hit = [s for s in self._sessions.values()
                   if s.server_id == server_id and (database is None or s.database == database)]
            for s in hit:
                self._sessions.pop(s.id, None)
        for s in hit:
            self._close_conn(s)
        return len(hit)

    @staticmethod
    def _close_conn(s: Session) -> None:
        try:
            if s.running:
                s.conn.cancel_safe(timeout=5)
            s.conn.close()   # an open transaction is rolled back by the server
        except Exception:  # noqa: BLE001 - closing must not fail
            log.exception("closing console session %s failed", s.id)

    def reap(self) -> int:
        """Close sessions idle for longer than the idle timeout; returns how many."""
        now = time.monotonic()
        with self._lock:
            idle = [s for s in self._sessions.values()
                    if not s.running and now - s.last_used > self.idle_timeout_s]
            for s in idle:
                self._sessions.pop(s.id, None)
        for s in idle:
            self._close_conn(s)
        return len(idle)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def open_transactions(self) -> int:
        """Sessions with a transaction that would be rolled back by closing them (asked about on app exit)."""
        with self._lock:
            sessions = list(self._sessions.values())
        return sum(1 for s in sessions if s.tx_status() in ("in_transaction", "failed"))

    # -------------------------------------------------- transactions

    def _locked(self, session_id: str) -> Session:
        s = self.get(session_id)
        if not s.lock.acquire(blocking=False):
            raise ConsoleError("A statement is still running in this tab; wait for it or cancel it.", "busy")
        if s.conn.closed:
            s.lock.release()
            self.close(session_id)
            raise ConsoleError("The connection of this query tab was lost; it reconnects on the next run.",
                               "session_lost")
        return s

    def set_mode(self, session_id: str, mode: str) -> dict:
        if mode not in MODES:
            raise ConsoleError("The mode must be auto or manual.")
        s = self._locked(session_id)
        try:
            if mode == "auto" and s.conn.info.transaction_status != TransactionStatus.IDLE:
                raise ConsoleError("A transaction is open; commit or roll it back before switching to auto-commit.")
            s.mode = mode
            s.last_used = time.monotonic()
            return s.state()
        finally:
            s.lock.release()

    def commit(self, session_id: str) -> dict:
        return self._end(session_id, "COMMIT")

    def rollback(self, session_id: str) -> dict:
        return self._end(session_id, "ROLLBACK")

    def _end(self, session_id: str, word: str) -> dict:
        s = self._locked(session_id)
        try:
            if s.conn.info.transaction_status != TransactionStatus.IDLE:
                s.conn.execute(word)
                s.timeout_applied = None   # a rollback can undo a SET made inside the transaction
            s.last_used = time.monotonic()
            return s.state()
        except psycopg.OperationalError as e:
            raise self._lost(s, e) from e
        finally:
            s.lock.release()

    def cancel(self, session_id: str) -> bool:
        s = self.get(session_id)
        if not s.running:
            return False
        try:
            s.conn.cancel_safe(timeout=5)
        except Exception:  # noqa: BLE001
            log.exception("cancel of console session %s failed", s.id)
            return False
        return True

    def _lost(self, s: Session, e: Exception) -> ConsoleError:
        self.close(s.id)
        return ConsoleError(f"The connection was lost: {' '.join(str(e).split())}. It reconnects on the next run.",
                            "session_lost")

    # -------------------------------------------------- execute

    def execute(self, session_id: str, sql_text: str, max_rows: int = DEFAULT_MAX_ROWS,
                timeout_s: float = 0) -> dict:
        """Run every statement of `sql_text`; returns {results: [...], **state}. Stops at the first error."""
        if not isinstance(sql_text, str) or not sql_text.strip():
            raise ConsoleError("Type a statement to run.")
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or not 1 <= max_rows <= MAX_ROWS_LIMIT:
            raise ConsoleError(f"The row limit must be between 1 and {MAX_ROWS_LIMIT}.")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not 0 <= timeout_s <= MAX_TIMEOUT_S:
            raise ConsoleError("The timeout must be 0 (none) or a number of seconds.")
        try:
            statements = list(split_sql(io.BytesIO(sql_text.encode("utf-8"))))
        except EngineError as e:
            raise ConsoleError(str(e)) from e
        if not statements:
            raise ConsoleError("There is no statement to run (only comments or blanks).")
        return self._batch(session_id, statements, max_rows, timeout_s)

    def table_data(self, session_id: str, schema: str, table: str, where: str = "", order_by=(),
                   limit: int = 100, offset: int = 0, count: bool = False) -> dict:
        """One page of a table or view for the table viewer: {result, count, sql, has_more, **state}.

        Names are quoted here; `where` is used as typed (full access, decision 009 §1). One extra
        row is read to know whether a next page exists. `count` also runs count(*) with the filter.
        """
        for key, value in (("schema", schema), ("table", table)):
            if not isinstance(value, str) or not value:
                raise ConsoleError(f"'{key}' is required.")
        if not isinstance(where, str) or ";" in _strip_literals(where):
            raise ConsoleError("The filter must be one condition without ';'.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_ROWS_LIMIT:
            raise ConsoleError(f"The page size must be between 1 and {MAX_ROWS_LIMIT}.")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ConsoleError("The offset must be 0 or more.")
        if not isinstance(order_by, (list, tuple)) or len(order_by) > 16 or not all(
                isinstance(o, dict) and isinstance(o.get("column"), str) and o["column"] for o in order_by):
            raise ConsoleError("'order_by' must be a list of {column, desc}.")
        source = qname(schema, table)
        cond = f"\nWHERE (\n{where.strip()}\n)" if where.strip() else ""
        order = ", ".join(f"{ident(o['column'])}{' DESC' if o.get('desc') else ''}" for o in order_by)
        page = f"SELECT * FROM {source}{cond}{f' ORDER BY {order}' if order else ''}\nLIMIT {limit + 1} OFFSET {offset}"
        statements = [Statement(page, line=1)]
        if count:
            statements.append(Statement(f"SELECT count(*) FROM {source}{cond}", line=1))
        out = self._batch(session_id, statements, limit, None)
        results = out.pop("results")
        first = results[0]
        counted = results[1] if len(results) > 1 and not results[1]["error"] else None
        return {"result": first, "has_more": first["truncated"], "sql": page,
                "count": int(counted["rows"][0][0]) if counted else None, **out}

    # -------------------------------------------------- EXPLAIN (8d)

    def explain(self, session_id: str, sql_text: str, analyze: bool = False, timeout_s: float = 0) -> dict:
        """EXPLAIN (FORMAT JSON) one statement: {plan, statement, analyze, rolled_back, duration_ms, error, **state}.

        With `analyze` the statement really runs (EXPLAIN ANALYZE, BUFFERS), always inside a transaction
        (or a savepoint when one is open) that is rolled back afterwards, so nothing it changes is kept.
        """
        if not isinstance(sql_text, str) or not sql_text.strip():
            raise ConsoleError("Put the cursor on a statement to explain.")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not 0 <= timeout_s <= MAX_TIMEOUT_S:
            raise ConsoleError("The timeout must be 0 (none) or a number of seconds.")
        try:
            statements = list(split_sql(io.BytesIO(sql_text.encode("utf-8"))))
        except EngineError as e:
            raise ConsoleError(str(e)) from e
        if len(statements) != 1 or statements[0].kind == "copy":
            raise ConsoleError("Explain works on one statement at a time; put the cursor on it or select it.")
        st = statements[0]
        if first_word(st.sql) == "EXPLAIN":
            raise ConsoleError("The statement already starts with EXPLAIN; remove it and use the Explain button.")
        s = self._locked(session_id)
        s.running = True
        conn = s.conn
        s.notices.clear()
        t0 = time.perf_counter()
        rolled_back = False
        try:
            if conn.info.transaction_status == TransactionStatus.INERROR:
                raise ConsoleError("The transaction failed; roll it back before explaining.")
            self._apply_timeout(s, timeout_s)
            options = "ANALYZE, BUFFERS, FORMAT JSON" if analyze else "FORMAT JSON"
            own = conn.info.transaction_status == TransactionStatus.IDLE
            if analyze:
                conn.execute("BEGIN" if own else f"SAVEPOINT {SAVEPOINT}_explain")
            try:
                with conn.cursor() as cur:
                    prefix = f"EXPLAIN ({options}) "
                    cur.execute(prefix + st.sql.rstrip().rstrip(';'))
                    plan = cur.fetchone()[0]
            except psycopg.Error as e:
                if conn.closed or conn.broken:
                    raise
                if analyze:
                    conn.execute("ROLLBACK" if own else f"ROLLBACK TO SAVEPOINT {SAVEPOINT}_explain")
                    if not own:
                        conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}_explain")
                err = _error_result(st, None, e, notices=list(s.notices), duration=time.perf_counter() - t0,
                                    offset=len(prefix))
                return {"plan": None, "statement": st.sql, "analyze": analyze, "rolled_back": analyze,
                        "duration_ms": err["duration_ms"], "notices": err["notices"], "error": err["error"], **s.state()}
            if analyze:
                conn.execute("ROLLBACK" if own else f"ROLLBACK TO SAVEPOINT {SAVEPOINT}_explain")
                if not own:
                    conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}_explain")
                rolled_back = True
        except psycopg.OperationalError as e:
            if conn.closed or conn.broken:
                raise self._lost(s, e) from e
            raise
        finally:
            s.running = False
            s.last_used = time.monotonic()
            s.lock.release()
        if isinstance(plan, str):
            plan = json.loads(plan)
        return {"plan": plan[0] if isinstance(plan, list) and plan else plan, "statement": st.sql, "analyze": analyze,
                "rolled_back": rolled_back, "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                "notices": list(s.notices), "error": None, **s.state()}

    # -------------------------------------------------- editable grid (8c)

    def apply_edits(self, session_id: str, schema: str, table: str, changes: list, preview: bool = False) -> dict:
        """Turn grid changes into UPDATE / INSERT / DELETE statements and run them (or only return them).

        changes: [{op: update, key: {col: text}, set: {col: value}}, {op: insert, values: {col: value}},
        {op: delete, key: {col: text}}]; a value is text (cast to the column type), None (NULL) or
        {"default": true}. `key` must name exactly the primary key columns. Names, types and the key come
        from the catalog, never from the browser; values are quoted literals, so the preview is exactly
        what runs. Auto-commit: all statements in one transaction. Manual: inside the open transaction
        (started if needed), undone to a savepoint on failure. An UPDATE or DELETE that does not touch
        exactly one row (changed or deleted meanwhile) undoes the whole batch.

        Returns {statements, applied, error: None | {index, message, sqlstate, detail, hint, conflict}, **state}.
        """
        for key, value in (("schema", schema), ("table", table)):
            if not isinstance(value, str) or not value:
                raise ConsoleError(f"'{key}' is required.")
        if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_EDITS:
            raise ConsoleError(f"Send between 1 and {MAX_EDITS} changes.")
        s = self._locked(session_id)
        s.running = True
        try:
            if s.conn.info.transaction_status == TransactionStatus.INERROR:
                raise ConsoleError("The transaction failed; roll it back before saving changes.")
            meta = self._edit_meta(s, schema, table)
            built = [self._edit_sql(s.conn, schema, table, meta, ch, i) for i, ch in enumerate(changes)]
            texts = [text for text, _ in built]
            if preview:
                return {"statements": texts, "applied": 0, "error": None, **s.state()}
            return {"statements": texts, **self._run_edits(s, built), **s.state()}
        except psycopg.OperationalError as e:
            if s.conn.closed or s.conn.broken:
                raise self._lost(s, e) from e
            raise
        finally:
            s.running = False
            s.last_used = time.monotonic()
            s.lock.release()

    @staticmethod
    def _edit_meta(s: Session, schema: str, table: str) -> dict:
        """{column: {type, generated, pk}} of a table, plus "_pk": [key columns in order]."""
        with s.conn.cursor() as cur:
            cur.execute("SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attgenerated <> '',"
                        " array_position(k.conkey, a.attnum)"
                        " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
                        " JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " LEFT JOIN pg_constraint k ON k.conrelid = c.oid AND k.contype = 'p'"
                        " WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')"
                        " AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum", (schema, table))
            rows = cur.fetchall()
        if not rows:
            raise ConsoleError(f"Table {schema}.{table} does not exist (any more).", "not_found")
        pk = [r[0] for r in sorted((r for r in rows if r[3]), key=lambda r: r[3])]
        if not pk:
            raise ConsoleError(f"{schema}.{table} has no primary key, so its rows cannot be edited here.")
        meta = {r[0]: {"type": r[1], "generated": r[2]} for r in rows}
        meta["_pk"] = pk
        return meta

    @staticmethod
    def _edit_sql(conn: psycopg.Connection, schema: str, table: str, meta: dict, ch, i: int) -> tuple[str, str]:
        where_ = f"Change {i + 1}"
        if not isinstance(ch, dict) or ch.get("op") not in ("update", "insert", "delete"):
            raise ConsoleError(f"{where_}: 'op' must be update, insert or delete.")
        op = ch["op"]

        def column(name) -> dict:
            if not isinstance(name, str) or name == "_pk" or name not in meta:
                raise ConsoleError(f"{where_}: {schema}.{table} has no column {name!r}.")
            return meta[name]

        def value(name, v, allow_default: bool) -> str:
            col = column(name)
            if isinstance(v, dict) and v.get("default") is True and allow_default:
                return "DEFAULT"
            if col["generated"]:
                raise ConsoleError(f"{where_}: {name} is a generated column; it cannot be set.")
            if v is None:
                return "NULL"
            if not isinstance(v, str):
                raise ConsoleError(f"{where_}: the value for {name} must be text, null or default.")
            if "\x00" in v:
                raise ConsoleError(f"{where_}: the value for {name} contains a NUL character.")
            return f"{sql.Literal(v).as_string(conn)}::{col['type']}"

        def key_clause() -> str:
            key = ch.get("key")
            if not isinstance(key, dict) or set(key) != set(meta["_pk"]):
                raise ConsoleError(f"{where_}: 'key' must give the primary key column(s) {', '.join(meta['_pk'])}.")
            parts = []
            for name in meta["_pk"]:
                if not isinstance(key[name], str):
                    raise ConsoleError(f"{where_}: key values must be text.")
                parts.append(f"{ident(name)} = {value(name, key[name], False)}")
            return " AND ".join(parts)

        target = qname(schema, table)
        if op == "delete":
            return f"DELETE FROM {target} WHERE {key_clause()}", op
        if op == "update":
            changes = ch.get("set")
            if not isinstance(changes, dict) or not changes:
                raise ConsoleError(f"{where_}: 'set' must name at least one column.")
            sets = ", ".join(f"{ident(n)} = {value(n, v, True)}" for n, v in changes.items())
            return f"UPDATE {target} SET {sets} WHERE {key_clause()}", op
        values = ch.get("values") or {}
        if not isinstance(values, dict):
            raise ConsoleError(f"{where_}: 'values' must be an object.")
        given = {n: v for n, v in values.items() if not (isinstance(v, dict) and v.get("default") is True and column(n))}
        if not given:
            return f"INSERT INTO {target} DEFAULT VALUES", op
        cols = ", ".join(ident(n) for n in given)
        return f"INSERT INTO {target} ({cols}) VALUES ({', '.join(value(n, v, True) for n, v in given.items())})", op

    def _run_edits(self, s: Session, built: list[tuple[str, str]]) -> dict:
        conn = s.conn
        s.notices.clear()
        if s.mode == "manual" and conn.info.transaction_status == TransactionStatus.IDLE:
            conn.execute("BEGIN")
        own = conn.info.transaction_status == TransactionStatus.IDLE
        conn.execute("BEGIN" if own else f"SAVEPOINT {SAVEPOINT}_edit")
        undo = "ROLLBACK" if own else f"ROLLBACK TO SAVEPOINT {SAVEPOINT}_edit"
        i = 0
        try:
            for i, (text, op) in enumerate(built):
                with conn.cursor() as cur:
                    cur.execute(text)
                    touched = cur.rowcount
                if op != "insert" and touched != 1:
                    conn.execute(undo)
                    if not own:
                        conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}_edit")
                    what = "no row" if touched == 0 else f"{touched} rows"
                    return {"applied": 0, "error": {
                        "index": i, "conflict": True, "sqlstate": None, "detail": None, "hint": None,
                        "message": f"Change {i + 1} ({op}) matched {what}: the row was changed or deleted since it was "
                                   f"read. Nothing was saved; reload and try again."}}
            conn.execute("COMMIT" if own else f"RELEASE SAVEPOINT {SAVEPOINT}_edit")
        except psycopg.Error as e:
            if conn.closed or conn.broken:
                raise
            conn.execute(undo)
            if not own:
                conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}_edit")
            diag = e.diag
            return {"applied": 0, "error": {
                "index": i, "conflict": False, "sqlstate": e.sqlstate, "detail": diag.message_detail,
                "hint": diag.message_hint,
                "message": f"Change {i + 1}: {diag.message_primary or ' '.join(str(e).split())}. Nothing was saved."}}
        return {"applied": len(built), "error": None}

    def _batch(self, session_id: str, statements: list, max_rows: int, timeout_s: float | None) -> dict:
        """Run statements in order under the session lock, stopping at the first error.
        `timeout_s` None keeps the session's statement timeout as it is."""
        s = self._locked(session_id)
        s.running = True
        results = []
        try:
            if timeout_s is not None:
                self._apply_timeout(s, timeout_s)
            for st in statements:
                if st.kind == "copy":
                    results.append(_error_result(st, "COPY ... FROM STDIN needs a file; use the Restore tab or "
                                                     "COPY ... FROM '/server/path'.", None))
                    break
                res = self._run_one(s, st, max_rows)
                results.append(res)
                if res["error"]:
                    break
        except psycopg.OperationalError as e:
            if s.conn.closed or s.conn.broken:
                raise self._lost(s, e) from e
            raise
        finally:
            s.running = False
            s.last_used = time.monotonic()
            s.lock.release()
        return {"results": results, **s.state()}

    def _apply_timeout(self, s: Session, timeout_s: float) -> None:
        if s.timeout_applied == timeout_s or s.conn.info.transaction_status == TransactionStatus.INERROR:
            return
        ms = 0 if not timeout_s else max(1, round(timeout_s * 1000))
        s.conn.execute(f"SET statement_timeout = {int(ms)}")
        s.timeout_applied = timeout_s

    def _run_one(self, s: Session, st, max_rows: int) -> dict:
        conn = s.conn
        word = first_word(st.sql)
        s.notices.clear()
        t0 = time.perf_counter()
        in_tx = conn.info.transaction_status == TransactionStatus.INTRANS
        use_savepoint = in_tx and word not in _TX_CONTROL
        if s.mode == "manual" and conn.info.transaction_status == TransactionStatus.IDLE and word not in _TX_CONTROL:
            conn.execute("BEGIN")
            use_savepoint = True
        if use_savepoint:
            conn.execute(f"SAVEPOINT {SAVEPOINT}")
        try:
            res = None
            if word in _ROW_QUERIES:
                res = self._through_cursor(s, st, max_rows, own_tx=not use_savepoint)
            if res is None:
                res = self._plain(s, st, max_rows)
        except psycopg.Error as e:
            if conn.closed or conn.broken:
                raise
            if use_savepoint:
                conn.execute(f"ROLLBACK TO SAVEPOINT {SAVEPOINT}")
                conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}")
            return _error_result(st, None, e, notices=list(s.notices), duration=time.perf_counter() - t0)
        if use_savepoint:
            conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}")
        res.update(statement=st.sql, line=st.line, notices=list(s.notices),
                   duration_ms=round((time.perf_counter() - t0) * 1000, 1), error=None)
        return res

    def _through_cursor(self, s: Session, st, max_rows: int, own_tx: bool) -> dict | None:
        """Read a row query through DECLARE/FETCH; None when DECLARE refuses it (run it normally then)."""
        conn = s.conn
        if own_tx:
            conn.execute("BEGIN")
        else:
            conn.execute(f"SAVEPOINT {SAVEPOINT}_declare")
        try:
            conn.execute(f"DECLARE {CURSOR} NO SCROLL CURSOR FOR {st.sql.rstrip().rstrip(';')}")
        except psycopg.Error:
            if conn.closed or conn.broken:
                raise
            conn.execute("ROLLBACK" if own_tx else f"ROLLBACK TO SAVEPOINT {SAVEPOINT}_declare")
            if not own_tx:
                conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}_declare")
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(f"FETCH FORWARD {max_rows + 1} FROM {CURSOR}")
                res = self._read(s, cur.pgresult, max_rows)
            conn.execute(f"CLOSE {CURSOR}")
            if own_tx:
                conn.execute("COMMIT")
            else:
                conn.execute(f"RELEASE SAVEPOINT {SAVEPOINT}_declare")
        except psycopg.Error:
            if not (conn.closed or conn.broken) and own_tx:
                conn.execute("ROLLBACK")
            raise
        n = res["row_count"]
        res["status"] = f"SELECT {n}{'+' if res['truncated'] else ''}"
        return res

    def _plain(self, s: Session, st, max_rows: int) -> dict:
        with s.conn.cursor() as cur:
            cur.execute(st.sql)
            pgres = cur.pgresult
            status = cur.statusmessage or ""
            if cur.description is None or pgres is None:
                return {"status": status, "columns": [], "rows": [], "row_count": max(cur.rowcount, 0),
                        "truncated": False, "has_rows": False, "editable": None, "readonly": None}
            res = self._read(s, pgres, max_rows)
        res["status"] = status
        return res

    def _read(self, s: Session, pgres, max_rows: int) -> dict:
        enc = s.conn.info.encoding or "utf-8"
        nf, nt = pgres.nfields, pgres.ntuples
        keep = min(nt, max_rows)
        cols = []
        for i in range(nf):
            cols.append({"name": (pgres.fname(i) or b"").decode(enc, "replace"), "type_oid": pgres.ftype(i),
                         "type_mod": pgres.fmod(i), "table_oid": pgres.ftable(i) or None,
                         "table_col": pgres.ftablecol(i) or None})
        rows = []
        for r in range(keep):
            row = []
            for c in range(nf):
                v = pgres.get_value(r, c)
                row.append(None if v is None else bytes(v).decode(enc, "replace"))
            rows.append(row)
        self._type_names(s, cols)
        editable, readonly = self._editable(s, cols)
        return {"columns": cols, "rows": rows, "row_count": keep, "truncated": nt > max_rows, "has_rows": True,
                "editable": editable, "readonly": readonly}

    @staticmethod
    def _editable(s: Session, cols: list[dict]) -> tuple[dict | None, str | None]:
        """Whether a result can be edited in the grid: every column comes straight from one table that has a
        primary key, and the key columns are in the result. Returns (editable, None) or (None, reason).

        editable = {schema, table, columns: [table column name per result column], key: [result column indexes]}
        """
        oids = {c["table_oid"] for c in cols}
        if not cols or None in oids or len(oids) != 1:
            return None, "Only results whose columns all come straight from one table can be edited."
        if s.conn.info.transaction_status == TransactionStatus.INERROR:
            return None, "The transaction failed; roll it back to edit."
        oid = oids.pop()
        try:
            with s.conn.cursor() as cur:
                cur.execute("SELECT n.nspname, c.relname, c.relkind,"
                            " (SELECT k.conkey::int[] FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'p')"
                            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = %s", (oid,))
                rel = cur.fetchone()
                cur.execute("SELECT attnum, attname FROM pg_attribute WHERE attrelid = %s AND attnum > 0", (oid,))
                names = dict(cur.fetchall())
        except psycopg.Error:
            log.exception("edit check failed")
            return None, "The table could not be checked for editing."
        if rel is None or rel[2] not in ("r", "p"):
            return None, "Only tables can be edited here (this result comes from a view or another kind of relation)."
        if not rel[3]:
            return None, f"{rel[0]}.{rel[1]} has no primary key, so its rows cannot be told apart for editing."
        attnums = [c["table_col"] for c in cols]
        if len(set(attnums)) != len(attnums):
            return None, "A table column appears twice in the result."
        missing = [names.get(a, str(a)) for a in rel[3] if a not in attnums]
        if missing:
            return None, f"Add the primary key column(s) {', '.join(missing)} to the query to edit its rows."
        return {"schema": rel[0], "table": rel[1], "columns": [names.get(a) for a in attnums],
                "key": [attnums.index(a) for a in rel[3]]}, None

    @staticmethod
    def _type_names(s: Session, cols: list[dict]) -> None:
        missing = sorted({(c["type_oid"], c["type_mod"]) for c in cols} - set(s.types))
        if missing and s.conn.info.transaction_status != TransactionStatus.INERROR:
            try:
                with s.conn.cursor() as cur:
                    cur.execute("SELECT t.o, t.m, format_type(t.o::oid, NULLIF(t.m, -1)::int)"
                                " FROM unnest(%s::bigint[], %s::bigint[]) AS t(o, m)",
                                ([m[0] for m in missing], [m[1] for m in missing]))
                    for o, m, name in cur.fetchall():
                        s.types[(o, m)] = name
            except psycopg.Error:
                log.exception("type names could not be read")
        for c in cols:
            c["type"] = s.types.get((c["type_oid"], c["type_mod"]), str(c["type_oid"]))


def _error_result(st, message: str | None, e: psycopg.Error | None, notices=None, duration: float = 0,
                  offset: int = 0) -> dict:
    """`offset`: characters run in front of the statement (an EXPLAIN prefix), taken off the error position."""
    diag = getattr(e, "diag", None)
    msg = message or (diag.message_primary if diag and diag.message_primary else " ".join(str(e).split()))
    position = None
    line = st.line
    if diag is not None and diag.statement_position:
        try:
            position = max(1, int(diag.statement_position) - offset)
            line = st.line + st.sql[:position - 1].count("\n")
        except ValueError:
            position = None
    err = {"message": msg, "sqlstate": getattr(e, "sqlstate", None), "line": line, "position": position,
           "detail": diag.message_detail if diag else None, "hint": diag.message_hint if diag else None}
    if isinstance(e, errors.QueryCanceled) and "user request" in msg:
        err["message"] = "Cancelled."
    return {"statement": st.sql, "line": st.line, "status": "ERROR", "columns": [], "rows": [], "row_count": 0,
            "truncated": False, "has_rows": False, "notices": notices or [],
            "duration_ms": round(duration * 1000, 1), "error": err}
