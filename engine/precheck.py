"""Pre-backup check: permissions, sizes, large flags, "latest N" ordering advice, impact (Step 2).

`run_precheck` opens its own short autocommit, read-only connection; `run_check` does the work
on an open one. The pure helpers (`TableOption`, `Thresholds`, `read_set`,
`reconcile_table_options`) are shared with the backup. Nothing here writes, locks for long or
creates anything: sizes of tables held under ACCESS EXCLUSIVE fall back to page estimates.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, fields

import psycopg
from psycopg import errors
from psycopg.rows import dict_row

from . import catalog
from .db import cancel_on_event, connect, require_supported, server_info
from .util import BackupError, Cancelled, EngineError, check_cancel

DATA_OPTIONS = ("full", "latest", "schema")
DEFAULT_LIMIT = 100_000
LOAD_NOTE = "The backup only reads, but it adds load: run it off-peak or against a read replica."
LARGEST_LISTED = 10
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_$]*")
_TIMESTAMP_TYPES = "'pg_catalog.date'::regtype, 'pg_catalog.timestamp'::regtype, 'pg_catalog.timestamptz'::regtype"
_PROBLEM_ORDER = {"schema_usage": 0, "table_select": 1, "rls": 2, "sequence_select": 3}


def display_ident(name: str) -> str:
    """Name as PostgreSQL prints it: bare when it is a plain lower-case word, else double-quoted."""
    return name if _PLAIN_NAME.fullmatch(name) else '"' + name.replace('"', '""') + '"'


def display_name(schema: str, name: str) -> str:
    return f"{display_ident(schema)}.{display_ident(name)}"


@dataclass
class Thresholds:
    """When a table counts as large (both editable in settings)."""
    large_mb: float = 500
    large_rows: int = 1_000_000

    @classmethod
    def from_settings(cls, settings: dict | None) -> "Thresholds":
        """Read `large_table_mb` / `large_table_rows` from settings.json content; bad values use defaults."""
        d = settings or {}
        out = cls()
        mb, rows = d.get("large_table_mb"), d.get("large_table_rows")
        if isinstance(mb, (int, float)) and not isinstance(mb, bool) and mb > 0:
            out.large_mb = float(mb)
        if isinstance(rows, (int, float)) and not isinstance(rows, bool) and rows > 0:
            out.large_rows = int(rows)
        return out


@dataclass
class TableOption:
    """Data option for one (root) table: all rows, the newest `limit` rows by `order_by`, or none."""
    schema: str
    table: str
    data: str = "full"
    order_by: str | None = None
    limit: int = DEFAULT_LIMIT

    @property
    def key(self) -> tuple[str, str]:
        return self.schema, self.table

    @property
    def name(self) -> str:
        return display_name(str(self.schema), str(self.table))

    @classmethod
    def from_dict(cls, d: "dict | TableOption") -> "TableOption":
        if isinstance(d, TableOption):
            return d
        if not isinstance(d, dict):
            raise BackupError("Each table option must be an object with schema, table and data.")
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in d.items() if k in known and v is not None}
        if "schema" not in values or "table" not in values:
            raise BackupError("A table option is missing its schema or table name.")
        return cls(**values)

    def to_dict(self) -> dict:
        out = {"schema": self.schema, "table": self.table, "data": self.data}
        if self.data == "latest":
            out.update(order_by=self.order_by, limit=self.limit)
        return out

    def validate(self) -> None:
        """Raise BackupError with a clear sentence when the option cannot be used."""
        if not isinstance(self.schema, str) or not isinstance(self.table, str) or not self.schema or not self.table:
            raise BackupError("A table option needs a schema and a table name.")
        if self.data not in DATA_OPTIONS:
            raise BackupError(f"Unknown data option {self.data!r} for table {self.name}; "
                              f"use full, latest or schema.")
        if self.data != "latest":
            return
        if not isinstance(self.order_by, str) or not self.order_by:
            raise BackupError(f"Table {self.name}: choose the ordering column for \"latest N rows\".")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) or self.limit < 1:
            raise BackupError(f"Table {self.name}: the number of rows must be a whole number of at least 1.")


def parse_table_options(items: list | None) -> list[TableOption]:
    """Validated options; a table named twice is refused."""
    out, seen = [], set()
    for item in items or []:
        opt = TableOption.from_dict(item)
        opt.validate()
        if opt.key in seen:
            raise BackupError(f"Table {opt.name} has more than one data option.")
        seen.add(opt.key)
        out.append(opt)
    return out


def options_by_root(relations: list[dict], options: list[TableOption]) -> dict[int, TableOption]:
    """{root table oid: option} for options naming a root table (not a partition) in `relations`."""
    roots = {(r["schema"], r["name"]): r["oid"] for r in relations
             if r["kind"] in ("r", "p") and r["oid"] == r["root_oid"]}
    return {roots[o.key]: o for o in options if o.key in roots}


def reconcile_table_options(relations: list[dict], saved: list | None) -> tuple[list[TableOption], list[str]]:
    """Saved options that still apply, and the names of those dropped.

    Dropped: tables that no longer exist (or are outside the selected schemas), partitions,
    invalid entries and repeats. Full options are not kept (Full is the default); tables
    without a saved option stay Full.
    """
    roots = {(r["schema"], r["name"]): r for r in relations if r["kind"] in ("r", "p")}
    kept, ignored, seen = [], [], set()
    for item in saved or []:
        try:
            opt = TableOption.from_dict(item)
            opt.validate()
        except BackupError:
            ignored.append(_saved_name(item))
            continue
        rel = roots.get(opt.key)
        if rel is None or rel["oid"] != rel["root_oid"] or opt.key in seen:
            ignored.append(rel["qname"] if rel else opt.name)
            continue
        seen.add(opt.key)
        if opt.data != "full":
            kept.append(opt)
    return kept, ignored


def _saved_name(item) -> str:
    if isinstance(item, dict) and isinstance(item.get("schema"), str) and isinstance(item.get("table"), str):
        return display_name(item["schema"], item["table"])
    return "(invalid entry)"


def read_set(relations: list[dict], mode: str, options: list[TableOption]) -> dict:
    """Exactly what the backup will read: `{tables, sequences, schemas, skipped_sequences}`.

    Tables: leaves of Full roots and Latest-N roots (`only` = plain table); nothing for
    Schema-only roots. Sequences: all, except those owned by a Schema-only table that the login
    cannot SELECT (their value is skipped with a warning). Schema mode reads no data.
    """
    out = {"tables": [], "sequences": [], "schemas": set(), "skipped_sequences": []}
    if mode == "schema":
        return out
    by_oid = {r["oid"]: r for r in relations}
    chosen = options_by_root(relations, options)

    def data_of(oid: int) -> str:
        rel = by_oid.get(oid)
        opt = chosen.get(rel["root_oid"]) if rel else None
        return opt.data if opt else "full"

    for r in relations:
        entry = {"oid": r["oid"], "qname": r["qname"]}
        if r["kind"] == "S":
            owner = r["owned_by_relid"]
            if owner is not None and data_of(owner) == "schema" and not r["can_lock"]:
                out["skipped_sequences"].append(entry)
                continue
            out["sequences"].append(entry)
        elif data_of(r["oid"]) == "full" and r["kind"] == "r":
            out["tables"].append({**entry, "only": True})
        elif data_of(r["oid"]) == "latest" and r["oid"] == r["root_oid"]:
            out["tables"].append({**entry, "only": r["kind"] == "r"})
        else:
            continue
        out["schemas"].add(r["schema_oid"])
    return out


_PERMISSIONS_SQL = """
SELECT 'schema_usage' AS kind, n.nspname AS schema, NULL::name AS name, quote_ident(n.nspname) AS qname
FROM pg_namespace n
WHERE n.oid = ANY(%(schemas)s::oid[]) AND NOT has_schema_privilege(n.oid, 'USAGE')
UNION ALL
SELECT CASE WHEN p.can_select THEN 'rls' ELSE 'table_select' END, n.nspname, c.relname, c.oid::regclass::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL (SELECT has_table_privilege(c.oid, 'SELECT') OR (
    EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped)
    AND NOT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
                    AND NOT has_column_privilege(c.oid, a.attnum, 'SELECT'))) AS can_select) p
WHERE c.oid = ANY(%(tables)s::oid[]) AND (NOT p.can_select OR row_security_active(c.oid))
UNION ALL
SELECT 'sequence_select', n.nspname, c.relname, c.oid::regclass::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.oid = ANY(%(sequences)s::oid[])
  AND CASE WHEN c.relkind = 'S' THEN NOT has_sequence_privilege(c.oid, 'SELECT') ELSE false END
"""


def _problem_message(kind: str, qname: str) -> str:
    if kind == "schema_usage":
        return f"Permission denied for schema {qname}: the login has no USAGE privilege on it."
    if kind == "table_select":
        return f"Permission denied for table {qname}: the login has no SELECT privilege on it."
    if kind == "sequence_select":
        return (f"Permission denied for sequence {qname}: the login has no SELECT privilege on it, "
                f"so its current value cannot be saved.")
    return (f"Table {qname} has row-level security that applies to this login (not the owner, no BYPASSRLS), "
            f"so its rows would be filtered.")


def check_permissions(conn: psycopg.Connection, reads: dict) -> list[dict]:
    """Every object of the read set the login cannot read completely (one problem per object)."""
    if not (reads["tables"] or reads["sequences"]):
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_PERMISSIONS_SQL, {"schemas": sorted(reads["schemas"]),
                                       "tables": [t["oid"] for t in reads["tables"]],
                                       "sequences": [s["oid"] for s in reads["sequences"]]})
        rows = cur.fetchall()
    problems = []
    for r in sorted(rows, key=lambda r: (_PROBLEM_ORDER[r["kind"]], r["schema"], r["name"] or "")):
        fixes = ["exclude_schema"] + (["schema_only"] if r["kind"] in ("table_select", "rls") else [])
        problems.append({"kind": r["kind"], "schema": r["schema"], "name": r["name"], "qname": r["qname"],
                         "message": _problem_message(r["kind"], r["qname"]), "fixes": fixes})
    return problems


def order_index_check(conn: psycopg.Connection, relid: int, column: str) -> dict:
    """Can an index serve the backup's "latest N" query on `column`?

    The backup orders by `col DESC` when the column cannot hold NULLs (`null_safe`) and by
    `col DESC NULLS LAST` otherwise; a btree index whose first key is the column serves the
    first form when it is ASC NULLS LAST / DESC NULLS FIRST and the second when it is
    DESC NULLS LAST / ASC NULLS FIRST. Partial and invalid indexes do not count.
    """
    nn_valid = ("COALESCE((SELECT k.convalidated FROM pg_constraint k WHERE k.conrelid = a.attrelid"
                " AND k.contype = 'n' AND k.conkey[1] = a.attnum LIMIT 1), true)"
                if conn.info.server_version >= 180000 else "true")
    row = conn.execute(f"SELECT a.attnum, a.attnotnull AND {nn_valid} FROM pg_attribute a WHERE a.attrelid = %s"
                       " AND a.attname = %s AND a.attnum > 0 AND NOT a.attisdropped", (relid, column)).fetchone()
    col = display_ident(column)
    if row is None:
        return {"exists": False, "indexed": False, "usable": False, "index": None, "null_safe": False,
                "message": f"Column {col} does not exist."}
    attnum, null_safe = row[0], bool(row[1])
    indexes = conn.execute("SELECT ic.relname, x.indoption[0] FROM pg_index x"
                           " JOIN pg_class ic ON ic.oid = x.indexrelid JOIN pg_am am ON am.oid = ic.relam"
                           " WHERE x.indrelid = %s AND x.indkey[0] = %s AND am.amname = 'btree'"
                           " AND x.indpred IS NULL AND x.indisvalid ORDER BY ic.relname", (relid, attnum)).fetchall()
    wanted = (0, 3) if null_safe else (1, 2)
    usable = [name for name, opt in indexes if opt in wanted]
    result = {"exists": True, "indexed": bool(indexes), "usable": bool(usable), "null_safe": null_safe,
              "index": usable[0] if usable else (indexes[0][0] if indexes else None)}
    if usable:
        result["message"] = f"Index {display_ident(usable[0])} serves the newest-rows query."
    elif not indexes:
        result["message"] = f"No index starts with {col}: the whole table is sorted to find the newest rows."
    elif null_safe:
        result["message"] = (f"Index {display_ident(indexes[0][0])} cannot be used for ORDER BY {col} DESC "
                             f"because of its NULLS order; an index on ({col}) would be used.")
    else:
        result["message"] = (f"Index {display_ident(indexes[0][0])} cannot be used because {col} allows NULLs; "
                             f"an index on ({col} DESC NULLS LAST) would be used.")
    return result


def run_precheck(server: dict, dbname: str, schemas: list[str] | None = None, mode: str = "full",
                 tables: list | None = None, thresholds: Thresholds | dict | None = None,
                 lock_wait_s: float = 2.0, cancel: threading.Event | None = None) -> dict:
    """Pre-backup check on its own short autocommit, read-only connection (ARCHITECTURE 9.2)."""
    conn = connect(server, dbname, autocommit=True)
    try:
        with cancel_on_event(conn, cancel):
            require_supported(conn)
            for setting in ("SET default_transaction_read_only = on", "SET search_path = pg_catalog",
                            "SET statement_timeout = '60s'", "SET client_encoding = 'UTF8'"):
                conn.execute(setting)
            conn.execute(f"SET lock_timeout = '{int(max(lock_wait_s, 0) * 1000)}ms'")
            return run_check(conn, schemas, mode, tables, thresholds, cancel)
    except (Cancelled, EngineError):
        raise
    except psycopg.Error as e:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelled by user") from e
        raise EngineError(f"The pre-backup check of {dbname} failed: {_message(e)}") from e
    finally:
        conn.close()


def run_check(conn: psycopg.Connection, schemas: list[str] | None = None, mode: str = "full",
              tables: list | None = None, thresholds: Thresholds | dict | None = None,
              cancel: threading.Event | None = None) -> dict:
    """The pre-check on an open connection (search_path = pg_catalog); see `run_precheck`."""
    limits = thresholds if isinstance(thresholds, Thresholds) else Thresholds.from_settings(thresholds)
    info = _server(conn)
    relations = catalog.list_relations(conn, schemas)
    options, ignored = reconcile_table_options(relations, tables)
    chosen = options_by_root(relations, options)
    check_cancel(cancel)
    roots = [r for r in relations if r["kind"] in ("r", "p") and r["oid"] == r["root_oid"]]
    leaves = [r for r in relations if r["kind"] == "r"]
    sizes = _leaf_sizes(conn, [r["oid"] for r in leaves])
    check_cancel(cancel)
    candidates = _order_candidates(conn, [r["oid"] for r in roots])
    entries = [_table_entry(conn, r, [x for x in leaves if x["root_oid"] == r["oid"]], sizes,
                            candidates.get(r["oid"], []), chosen.get(r["oid"]), limits, cancel) for r in roots]
    problems = check_permissions(conn, read_set(relations, mode, options))
    return {"server": info, "thresholds": {"large_mb": limits.large_mb, "large_rows": limits.large_rows},
            "tables": entries, "problems": problems, "blocking": bool(problems),
            "locks": _exclusive_locks(conn, relations), "ignored_options": ignored,
            "impact": _impact(entries, mode), "notes": [LOAD_NOTE]}


def _server(conn: psycopg.Connection) -> dict:
    info = server_info(conn)
    bypass = conn.execute("SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()
    read_all = (conn.execute("SELECT pg_has_role('pg_read_all_data', 'MEMBER')").fetchone()[0]
                if info["version_num"] >= 140000 else False)
    return {"version_num": info["version_num"], "version": info["version"], "user": info["user"],
            "is_superuser": info["is_superuser"], "bypassrls": bool(bypass and bypass[0]),
            "read_all_data": bool(read_all)}


_SIZES_SQL = """
SELECT c.oid, c.reltuples::float8 AS reltuples,
       (c.relpages::bigint + COALESCE(t.relpages, 0)) * current_setting('block_size')::bigint AS est_size,
       CASE WHEN %(exact)s AND NOT EXISTS (
              SELECT 1 FROM pg_locks l WHERE l.locktype = 'relation' AND l.relation = c.oid
              AND l.mode = 'AccessExclusiveLock'
              AND l.database = (SELECT d.oid FROM pg_database d WHERE d.datname = current_database()))
            THEN pg_table_size(c.oid) END AS size
FROM pg_class c LEFT JOIN pg_class t ON t.oid = c.reltoastrelid
WHERE c.oid = ANY(%(oids)s::oid[])
"""


def _leaf_sizes(conn: psycopg.Connection, oids: list[int]) -> dict[int, dict]:
    """{oid: {size, size_exact, reltuples}}; `pg_table_size` waits for locks, so locked tables are estimated."""
    if not oids:
        return {}
    try:
        rows = _dict_rows(conn, _SIZES_SQL, {"exact": True, "oids": oids})
    except errors.LockNotAvailable:
        rows = _dict_rows(conn, _SIZES_SQL, {"exact": False, "oids": oids})
    return {r["oid"]: {"size": int(r["size"] if r["size"] is not None else r["est_size"]),
                       "size_exact": r["size"] is not None, "reltuples": r["reltuples"]} for r in rows}


_CANDIDATES_SQL = f"""
SELECT a.attrelid AS relid, a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,
       a.attnotnull AS not_null, COALESCE(a.attnum = ANY(pk.conkey), false) AS pk,
       bt.base IN ({_TIMESTAMP_TYPES}) AS timestamp_like
FROM pg_attribute a
LEFT JOIN pg_constraint pk ON pk.conrelid = a.attrelid AND pk.contype = 'p'
CROSS JOIN LATERAL (
    WITH RECURSIVE b(oid, typtype, basetype) AS (
        SELECT t.oid, t.typtype, t.typbasetype FROM pg_type t WHERE t.oid = a.atttypid
        UNION ALL
        SELECT t.oid, t.typtype, t.typbasetype FROM pg_type t JOIN b ON t.oid = b.basetype WHERE b.typtype = 'd')
    SELECT b.oid::regtype AS base FROM b WHERE b.typtype <> 'd' LIMIT 1) bt
WHERE a.attrelid = ANY(%(oids)s::oid[]) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""


def _order_candidates(conn: psycopg.Connection, oids: list[int]) -> dict[int, list[dict]]:
    """{table oid: PK and date/timestamp columns in column order}."""
    out: dict[int, list[dict]] = {}
    if oids:
        for r in _dict_rows(conn, _CANDIDATES_SQL, {"oids": oids}):
            if r["pk"] or r["timestamp_like"]:
                out.setdefault(r.pop("relid"), []).append(r)
    return out


def _table_entry(conn, rel: dict, leaves: list[dict], sizes: dict, candidates: list[dict],
                 option: TableOption | None, limits: Thresholds, cancel) -> dict:
    """One logical table (a partitioned table is one entry, its partitions are summed)."""
    check_cancel(cancel)
    stats = [sizes[x["oid"]] for x in leaves if x["oid"] in sizes]
    size = sum(s["size"] for s in stats)
    est_rows = int(sum(max(s["reltuples"], 0) for s in stats))
    reasons = ((["size"] if size > limits.large_mb * 2 ** 20 else [])
               + (["rows"] if est_rows > limits.large_rows else []))
    columns = [{**c, "index": order_index_check(conn, rel["oid"], c["name"])} for c in candidates]
    default = next((c["name"] for c in columns if c["timestamp_like"]), None) or \
        next((c["name"] for c in columns if c["pk"]), None)
    opt = option or TableOption(rel["schema"], rel["name"], "full", default)
    option_error = None
    if opt.data == "latest" and not any(c["name"] == opt.order_by for c in columns):
        check = order_index_check(conn, rel["oid"], opt.order_by)
        if not check["exists"]:
            option_error = (f"Table {rel['qname']}: the ordering column {display_ident(opt.order_by)} "
                            f"no longer exists; choose another column.")
    return {"schema": rel["schema"], "name": rel["name"], "qname": rel["qname"],
            "kind": "partitioned" if rel["kind"] == "p" else "table",
            "partitions": len(leaves) if rel["kind"] == "p" else 0, "size": size,
            "size_exact": all(s["size_exact"] for s in stats),
            "est_rows": est_rows, "rows_unknown": any(s["reltuples"] < 0 for s in stats),
            "large": bool(reasons), "large_reason": reasons,
            "option": {"data": opt.data, "order_by": opt.order_by or default, "limit": opt.limit},
            "option_error": option_error, "order_columns": columns, "default_order_column": default}


def _exclusive_locks(conn: psycopg.Connection, relations: list[dict]) -> list[dict]:
    """ACCESS EXCLUSIVE locks (granted or waited for) on the listed tables: the backup would wait for them."""
    names = {r["oid"]: r["qname"] for r in relations if r["kind"] in ("r", "p")}
    if not names:
        return []
    rows = conn.execute("SELECT l.relation, l.mode, l.granted, l.pid FROM pg_locks l"
                        " WHERE l.locktype = 'relation' AND l.mode = 'AccessExclusiveLock'"
                        " AND l.database = (SELECT d.oid FROM pg_database d WHERE d.datname = current_database())"
                        " AND l.relation = ANY(%s::oid[]) ORDER BY l.relation, l.pid", (list(names),)).fetchall()
    return [{"qname": names[r[0]], "mode": r[1], "granted": r[2], "pid": r[3]} for r in rows]


def scaled_estimate(size: int, est_rows: int, rows_unknown: bool, limit: int) -> tuple[int, int]:
    """(bytes, rows) for "latest `limit` rows" of a table of `size` bytes and `est_rows` rows."""
    if rows_unknown or est_rows <= 0:
        return size, limit
    share = min(1.0, limit / est_rows)
    return int(size * share), min(limit, est_rows)


def _impact(entries: list[dict], mode: str) -> dict:
    """Data the backup reads with the current options, against a backup of every row."""
    data_bytes = rows = tables = 0
    if mode != "schema":
        for e in entries:
            opt = e["option"]
            if opt["data"] == "full":
                data_bytes, rows, tables = data_bytes + e["size"], rows + e["est_rows"], tables + 1
            elif opt["data"] == "latest":
                b, n = scaled_estimate(e["size"], e["est_rows"], e["rows_unknown"], opt["limit"])
                data_bytes, rows, tables = data_bytes + b, rows + n, tables + 1
    largest = sorted(entries, key=lambda e: (-e["size"], e["qname"]))[:LARGEST_LISTED]
    return {"tables": tables, "data_bytes": data_bytes, "est_rows": rows,
            "full_data_bytes": sum(e["size"] for e in entries), "full_est_rows": sum(e["est_rows"] for e in entries),
            "largest": [{"qname": e["qname"], "size": e["size"], "est_rows": e["est_rows"]} for e in largest]}


def _dict_rows(conn: psycopg.Connection, query: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def _message(e: psycopg.Error) -> str:
    diag = getattr(e, "diag", None)
    return (diag.message_primary if diag else None) or " ".join(str(e).split()) or type(e).__name__
