"""Restore: a streaming SQL splitter and an executor that runs a backup file through psycopg.

The splitter understands quotes, dollar quoting, comments, SQL-standard function bodies
(BEGIN ATOMIC ... END), multi-action rules and `COPY ... FROM stdin` data blocks. The
executor applies statements one by one (or in one transaction), streams COPY data in
~1 MB batches, collects errors and reports progress by file position.
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from typing import BinaryIO

import psycopg

from .db import cancel_on_event, connect
from .util import Cancelled, RestoreError, check_cancel

log = logging.getLogger(__name__)

COPY_BATCH_BYTES = 1 << 20
MAX_LISTED = 1000
PROGRESS_INTERVAL = 0.25
SQL_PREVIEW = 160
_BOM = b"\xef\xbb\xbf"

_NORMAL, _SQ, _ESQ, _DQ, _DOLLAR, _BLOCK = range(6)

_TOKEN = re.compile(r"""
    (?P<esq>[Ee]')
  | (?P<sq>(?:[Uu]&)?')
  | (?P<dq>")
  | (?P<dollar>\$(?:[^\W\d]\w*)?\$)
  | (?P<param>\$\d+)
  | (?P<lc>--)
  | (?P<bc>/\*)
  | (?P<semi>;)
  | (?P<word>[^\W\d][\w$]*)
  | (?P<num>\d[\w.]*)
  | (?P<paren>[()])
""", re.VERBOSE)
_SPACE = re.compile(r"\s*")
_ESQ_STOP = re.compile(r"[\\']")
_BLOCK_STOP = re.compile(r"/\*|\*/")
_LABEL = re.compile(r"--\s*Type:\s*(?P<kind>[^;]*?)\s*;\s*Name:\s*(?P<name>.*?)\s*$")
_COPY_FROM_STDIN = re.compile(r"COPY\s.+\sFROM\s+stdin\s*;?\s*\Z", re.IGNORECASE | re.DOTALL)
_PRIVILEGE = re.compile(r"(?:GRANT|REVOKE)\s|ALTER\s.+\sOWNER\s+TO\s", re.IGNORECASE | re.DOTALL)
_GRANTEE = re.compile(r'(?:\sTO|\sFROM)\s+("(?:[^"]|"")+"|[^\s;]+)(?:\s+WITH\s+GRANT\s+OPTION)?\s*;?\s*\Z',
                      re.IGNORECASE)


@dataclass
class Statement:
    """One statement from a backup file; COPY statements carry their data as byte batches."""
    sql: str
    kind: str = "sql"
    label_kind: str = ""
    label_name: str = ""
    line: int = 0
    data: Iterator[bytes] | None = None


def _readline(reader: BinaryIO) -> bytes:
    """readline() that turns a damaged or cut-off gzip stream into a RestoreError."""
    try:
        return reader.readline()
    except (EOFError, OSError, zlib.error) as e:
        raise RestoreError(f"The backup file is truncated or damaged: {e}") from e


class _CopyData:
    """Iterates the data lines of one COPY block in ~1 MB batches up to the `\\.` line."""

    def __init__(self, reader: BinaryIO, lineno: int):
        self.reader = reader
        self.start = lineno
        self.lineno = lineno
        self.done = False

    def __iter__(self) -> Iterator[bytes]:
        buf: list[bytes] = []
        size = 0
        while not self.done:
            line = _readline(self.reader)
            if not line:
                raise RestoreError(f"The backup file is truncated: the COPY data that starts after "
                                   f"line {self.start} has no end marker.")
            self.lineno += 1
            if line.rstrip(b"\r\n") == b"\\.":
                self.done = True
                break
            buf.append(line)
            size += len(line)
            if size >= COPY_BATCH_BYTES:
                yield b"".join(buf)
                buf, size = [], 0
        if buf:
            yield b"".join(buf)

    def drain(self) -> None:
        """Skip whatever the consumer did not read."""
        for _ in self:
            pass


class _Scanner:
    """Character-level state machine; `feed` returns the statements completed by a line."""

    def __init__(self) -> None:
        self.state = _NORMAL
        self.tag = ""
        self.block = 0
        self.label = ("", "")
        self._new_statement()

    def _new_statement(self) -> None:
        self.parts: list[str] = []
        self.started = False
        self.start_line = 0
        self.head: list[str] = []
        self.mode = ""
        self.depth = 0
        self.parens = 0

    def feed(self, line: str, lineno: int) -> list[tuple[str, int, tuple[str, str]]]:
        done = []
        pos, end = 0, len(line)
        seg = 0
        while pos < end:
            if self.state == _NORMAL:
                if not self.started:
                    pos = _SPACE.match(line, pos).end()
                    if pos >= end:
                        break
                    if line.startswith("--", pos):
                        self._read_label(line[pos:])
                        pos = end
                        break
                    if line.startswith("/*", pos):
                        self.state, self.block, pos = _BLOCK, 1, pos + 2
                        continue
                    self.started, self.start_line, seg = True, lineno, pos
                pos, finished = self._normal(line, pos, end)
                if finished:
                    self.parts.append(line[seg:pos])
                    done.append(("".join(self.parts), self.start_line, self.label))
                    self._new_statement()
            elif self.state in (_SQ, _DQ):
                q = "'" if self.state == _SQ else '"'
                i = line.find(q, pos)
                if i < 0:
                    pos = end
                elif line.startswith(q + q, i):
                    pos = i + 2
                else:
                    pos, self.state = i + 1, _NORMAL
            elif self.state == _ESQ:
                m = _ESQ_STOP.search(line, pos)
                if not m:
                    pos = end
                elif m.group() == "\\" or line.startswith("''", m.start()):
                    pos = m.start() + 2
                else:
                    pos, self.state = m.end(), _NORMAL
            elif self.state == _DOLLAR:
                i = line.find(self.tag, pos)
                pos, self.state = (end, _DOLLAR) if i < 0 else (i + len(self.tag), _NORMAL)
            else:
                m = _BLOCK_STOP.search(line, pos)
                if not m:
                    pos = end
                else:
                    pos = m.end()
                    self.block += 1 if m.group() == "/*" else -1
                    if self.block == 0:
                        self.state = _NORMAL
        if self.started:
            self.parts.append(line[seg:])
        return done

    def _normal(self, line: str, pos: int, end: int) -> tuple[int, bool]:
        """Scan NORMAL text up to a state change; True when a statement ends."""
        while True:
            m = _TOKEN.search(line, pos)
            if not m:
                return end, False
            kind, pos = m.lastgroup, m.end()
            if kind == "semi":
                if self.depth <= 0 and self.parens <= 0:
                    return pos, True
            elif kind == "word":
                self._word(m.group())
            elif kind == "paren":
                if self.mode == "rule":
                    self.parens += 1 if m.group() == "(" else -1
            elif kind == "lc":
                return end, False
            elif kind in ("sq", "esq", "dq", "dollar", "bc"):
                self.state = {"sq": _SQ, "esq": _ESQ, "dq": _DQ, "dollar": _DOLLAR, "bc": _BLOCK}[kind]
                self.tag = m.group() if kind == "dollar" else ""
                self.block = 1 if kind == "bc" else 0
                return pos, False

    def _word(self, word: str) -> None:
        """Decide the statement mode from its first words; count BEGIN/CASE/END in bodies."""
        upper = word.upper()
        if not self.mode:
            self.head.append(upper)
            self.mode = _statement_mode(self.head)
            return
        if self.mode == "body":
            if upper in ("BEGIN", "CASE"):
                self.depth += 1
            elif upper == "END":
                self.depth -= 1

    def _read_label(self, comment: str) -> None:
        m = _LABEL.match(comment)
        if m:
            self.label = (m.group("kind"), m.group("name"))

    def finish(self, lineno: int) -> tuple[str, int, tuple[str, str]] | None:
        """End of file: return a last statement without `;`, or raise if the file is cut off."""
        if self.state != _NORMAL or (self.started and (self.depth > 0 or self.parens > 0)):
            where = self.start_line or lineno
            raise RestoreError(f"The backup file is truncated: the statement that starts at line {where} "
                               f"never ends (open quote, comment or body).")
        if self.started:
            text = "".join(self.parts).strip()
            if text:
                return text, self.start_line, self.label
        return None


def _statement_mode(head: list[str]) -> str:
    """'' = undecided, 'body' = CREATE FUNCTION/PROCEDURE, 'rule' = CREATE RULE, else 'plain'."""
    if head[0] != "CREATE":
        return "plain"
    if len(head) == 2 and head[1] != "OR":
        target = head[1]
    elif len(head) == 4:
        target = head[3]
    else:
        return ""
    if target in ("FUNCTION", "PROCEDURE"):
        return "body"
    return "rule" if target == "RULE" else "plain"


def split_sql(reader: BinaryIO) -> Iterator[Statement]:
    """Yield the statements of a backup file read in binary mode, line by line.

    For a COPY statement, `data` yields the block's raw lines in ~1 MB batches; whatever
    the caller leaves unread is skipped before the next statement.
    """
    scanner = _Scanner()
    lineno = 0
    while True:
        raw = _readline(reader)
        if not raw:
            break
        lineno += 1
        if lineno == 1 and raw.startswith(_BOM):
            raw = raw[len(_BOM):]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise RestoreError(f"The backup file is not valid UTF-8 (line {lineno}).") from e
        for sql, start, label in scanner.feed(text, lineno):
            if _COPY_FROM_STDIN.match(sql):
                block = _CopyData(reader, lineno)
                yield Statement(sql, "copy", label[0], label[1], start, block)
                block.drain()
                lineno = block.lineno
            else:
                yield Statement(sql, "sql", label[0], label[1], start)
    last = scanner.finish(lineno)
    if last:
        yield Statement(last[0], "sql", last[2][0], last[2][1], last[1])


def open_backup(path: str) -> tuple[BinaryIO, BinaryIO]:
    """Open a backup file; returns (raw file, reader). Gzip is detected by its magic bytes."""
    raw = open(path, "rb")
    try:
        magic = raw.read(2)
        raw.seek(0)
        if magic == b"\x1f\x8b":
            return raw, gzip.GzipFile(fileobj=raw, mode="rb")
        return raw, raw
    except Exception:
        raw.close()
        raise


@dataclass
class RestoreOptions:
    """on_error: 'continue' (default) or 'stop'; single_transaction implies stop."""
    on_error: str = "continue"
    single_transaction: bool = False

    @classmethod
    def from_value(cls, value: "RestoreOptions | dict | None") -> "RestoreOptions":
        if isinstance(value, RestoreOptions):
            opts = value
        else:
            known = {f.name for f in fields(cls)}
            opts = cls(**{k: v for k, v in (value or {}).items() if k in known})
        if opts.on_error not in ("continue", "stop"):
            raise RestoreError(f"Unknown on_error option {opts.on_error!r}; use 'continue' or 'stop'.")
        if opts.single_transaction:
            opts.on_error = "stop"
        return opts


class _Stop(Exception):
    """Internal: end the run after an error (stop mode, single transaction, fatal)."""


class _RestoreRun:
    """State of one restore: counters, collected errors/warnings and progress reporting."""

    def __init__(self, opts: RestoreOptions, progress: Callable | None,
                 cancel: threading.Event | None, file_size: int):
        self.opts = opts
        self.progress = progress
        self.cancel = cancel
        self.file_size = file_size
        self.statements = self.objects = self.rows = 0
        self.errors: list[dict] = []
        self.warnings: list[dict] = []
        self.error_count = self.warning_count = 0
        self.missing_roles: list[str] = []
        self.label = ("", "")
        self.status = "ok"
        self._last_emit = 0.0

    def emit(self, event: str, data: dict) -> None:
        if self.progress is not None:
            try:
                self.progress(event, data)
            except Exception:  # noqa: BLE001 - a broken observer must not stop the restore
                log.exception("progress callback failed")

    def execute(self, conn: psycopg.Connection, raw: BinaryIO, reader: BinaryIO) -> None:
        self.emit("phase", {"name": "restore"})
        self.emit("totals", {"file_size": self.file_size})
        try:
            if self.opts.single_transaction:
                with conn.transaction():
                    self._run_all(conn, raw, reader)
            else:
                self._run_all(conn, raw, reader)
            self.status = "completed_with_errors" if self.error_count else "ok"
        except Cancelled:
            self.status = "cancelled"
            self.emit("log", {"level": "warning", "msg": "Restore cancelled by user"})
        except _Stop:
            self.status = "failed"
        except psycopg.Error as e:
            if self._cancel_requested():
                self.status = "cancelled"
                self.emit("log", {"level": "warning", "msg": "Restore cancelled by user"})
            else:
                log.exception("restore aborted")
                self._record(self.errors, "error", _diag(e), "", 0)
                self.status = "failed"
        self._position(raw, force=True)

    def _cancel_requested(self) -> bool:
        return self.cancel is not None and self.cancel.is_set()

    def _run_all(self, conn: psycopg.Connection, raw: BinaryIO, reader: BinaryIO) -> None:
        cur = conn.cursor()
        statements = split_sql(reader)
        while True:
            check_cancel(self.cancel)
            try:
                st = next(statements)
            except StopIteration:
                return
            except RestoreError as e:
                self._record(self.errors, "error", (None, str(e)), "", 0)
                raise _Stop from e
            self._track_label(st)
            if st.kind == "copy":
                self._copy(conn, cur, st, raw)
            else:
                self._sql(conn, cur, st)
            self._position(raw)

    def _track_label(self, st: Statement) -> None:
        label = (st.label_kind, st.label_name)
        if label != self.label:
            self.label = label
            self.objects += 1
            self.emit("object", {"kind": label[0], "name": label[1], "objects_done": self.objects})

    def _sql(self, conn: psycopg.Connection, cur: psycopg.Cursor, st: Statement) -> None:
        privilege = bool(_PRIVILEGE.match(st.sql))
        try:
            if privilege and self.opts.single_transaction:
                with conn.transaction():
                    cur.execute(st.sql)
            else:
                cur.execute(st.sql)
            self.statements += 1
        except psycopg.Error as e:
            self._failed(conn, st, e, privilege)

    def _copy(self, conn: psycopg.Connection, cur: psycopg.Cursor, st: Statement, raw: BinaryIO) -> None:
        rows = 0
        try:
            with cur.copy(st.sql) as cp:
                for batch in st.data:
                    check_cancel(self.cancel)
                    cp.write(batch)
                    rows += batch.count(b"\n")
                    self._position(raw, rows)
            self.rows += rows
            self.statements += 1
        except RestoreError as e:
            self._record(self.errors, "error", (None, str(e)), st.sql, st.line)
            raise _Stop from e
        except psycopg.Error as e:
            self._failed(conn, st, e, False)

    def _failed(self, conn: psycopg.Connection, st: Statement, e: psycopg.Error, privilege: bool) -> None:
        """Classify a failed statement as warning or error; raise _Stop when the run must end.

        A statement interrupted by Cancel (the watcher cancels it on the server) is not an error.
        """
        if self._cancel_requested():
            raise Cancelled("Cancelled by user") from e
        if conn.broken:
            self._record(self.errors, "error", (e.sqlstate, f"The connection to the server was lost: {_diag(e)[1]}"),
                         st.sql, st.line)
            raise _Stop from e
        if privilege and e.sqlstate == "42704":
            self._record(self.warnings, "warning", _diag(e), st.sql, st.line)
            self._note_missing_role(st.sql)
            return
        self._record(self.errors, "error", _diag(e), st.sql, st.line)
        if self.opts.on_error == "stop":
            raise _Stop from e

    def _record(self, target: list, event: str, diag: tuple, sql: str, line: int) -> None:
        item = {"object": f"{self.label[0]} {self.label[1]}".strip(), "sqlstate": diag[0],
                "message": diag[1], "sql": sql[:SQL_PREVIEW], "line": line}
        if event == "error":
            self.error_count += 1
        else:
            self.warning_count += 1
        if len(target) < MAX_LISTED:
            target.append(item)
        self.emit(event, item)

    def _note_missing_role(self, sql: str) -> None:
        m = _GRANTEE.search(sql)
        if m:
            role = m.group(1)
            role = role[1:-1].replace('""', '"') if role.startswith('"') else role
            if role not in self.missing_roles:
                self.missing_roles.append(role)

    def _position(self, raw: BinaryIO, current_rows: int = 0, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_emit >= PROGRESS_INTERVAL:
            self._last_emit = now
            try:
                pos = raw.tell()
            except (OSError, ValueError):
                pos = self.file_size
            self.emit("position", {"pos": pos, "rows_loaded": self.rows + current_rows,
                                   "errors": self.error_count, "warnings": self.warning_count})

    def result(self, duration: float) -> dict:
        res = {
            "status": self.status, "statements": self.statements, "objects": self.objects,
            "rows": self.rows, "errors": self.errors, "warnings": self.warnings,
            "error_count": self.error_count, "warning_count": self.warning_count,
            "missing_roles": self.missing_roles, "duration_s": round(duration, 3),
        }
        if self.missing_roles:
            res["missing_roles_note"] = (f"{self.warning_count} privilege statements skipped: role(s) "
                                         f"{', '.join(self.missing_roles)} not found")
        return res


def _diag(e: psycopg.Error) -> tuple[str | None, str]:
    """(SQLSTATE, primary message) of a psycopg error."""
    diag = getattr(e, "diag", None)
    message = (diag.message_primary if diag else None) or str(e).strip() or type(e).__name__
    return getattr(e, "sqlstate", None), message


def run_restore(server: dict, dbname: str, path: str, options: RestoreOptions | dict | None = None,
                progress: Callable[[str, dict], None] | None = None,
                cancel: threading.Event | None = None) -> dict:
    """Apply a backup file to an existing database and return the result dict (see ARCHITECTURE 8.1)."""
    opts = RestoreOptions.from_value(options)
    if not os.path.isfile(path):
        raise RestoreError(f"Backup file not found: {path}")
    started = time.monotonic()
    run = _RestoreRun(opts, progress, cancel, os.path.getsize(path))
    conn = connect(server, dbname, autocommit=True)
    try:
        raw, reader = open_backup(path)
        try:
            with cancel_on_event(conn, cancel):
                run.execute(conn, raw, reader)
        finally:
            if reader is not raw:
                reader.close()
            raw.close()
    finally:
        conn.close()
    return run.result(time.monotonic() - started)
