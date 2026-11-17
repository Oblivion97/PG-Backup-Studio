"""Table transfer (decision 013): copy the rows of one table into a table of another database.

Source and target can be any two saved servers (or the same one). Rows travel as a COPY text stream through the
app: `COPY (SELECT <mapped source columns> FROM <table> [WHERE filter]) TO STDOUT` on the source and
`COPY <target> (<mapped target columns>) FROM STDIN` on the target, so each value is converted by the target
column's type, exactly as if it had been typed in. The source runs in a READ ONLY, REPEATABLE READ transaction
(a consistent snapshot; a row filter cannot write). Everything on the target (create table, delete existing rows,
copy, insert, sequences) runs in ONE transaction: on any error or a cancel nothing is changed.

`compare` tells the UI whether the target matches the source exactly (direct transfer), differs (map columns)
or is missing (create it); `preview` validates a spec and returns the SQL that will run; `run_transfer` runs it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import psycopg
from psycopg import errors, sql

from . import db
from .db import _primary
from .util import Cancelled, EngineError, check_cancel

LOCK_TIMEOUT = "15s"
MAX_FILTER_CHARS = 4000
MAX_QUERY_CHARS = 200_000
MAX_NAME_BYTES = 63
PROGRESS_EVERY_S = 0.5
STAGE_TABLE = "pgbs_transfer_stage"
EXISTING_MODES = ("append", "skip", "replace")
SOURCE_KINDS = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table"}
TARGET_KINDS = {"r": "table", "p": "partitioned table"}
_SERIAL_TYPES = {"smallint", "integer", "bigint"}
_NEXTVAL = re.compile(r"^nextval\(", re.I)
_ROWS_QUERY = re.compile(r"(?is)^(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*(select|with|values|table|\()")


class TransferError(EngineError):
    """A transfer was refused or failed. `code`: invalid | not_found | exists | permission | failed."""

    def __init__(self, message: str, code: str = "invalid"):
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------- catalog

_REL_SQL = ("SELECT c.oid, c.relkind,"
            " (SELECT sum(GREATEST(x.reltuples, 0)) FROM pg_catalog.pg_class x"
            "   WHERE x.oid = c.oid OR x.oid IN (SELECT inhrelid FROM pg_catalog.pg_inherits WHERE inhparent = c.oid)),"
            " (SELECT bool_or(x.reltuples >= 0) FROM pg_catalog.pg_class x"
            "   WHERE x.oid = c.oid OR x.oid IN (SELECT inhrelid FROM pg_catalog.pg_inherits WHERE inhparent = c.oid))"
            " FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = %s")

_COLS_SQL = ("SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), t.typcategory, a.attnotnull,"
             " pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated,"
             " pg_catalog.pg_get_serial_sequence(%s, a.attname)"
             " FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_type t ON t.oid = a.atttypid"
             " LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
             " WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum")

_KEYS_SQL = ("SELECT i.indisprimary, ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(n, o)"
             "   JOIN pg_catalog.pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.n ORDER BY k.o)"
             " FROM pg_catalog.pg_index i WHERE i.indrelid = %s AND i.indisunique AND i.indisvalid"
             " AND i.indpred IS NULL ORDER BY NOT i.indisprimary")


def _qualified_text(schema: str, table: str) -> str:
    """'"schema"."table"' as text, for regclass arguments such as pg_get_serial_sequence."""
    return '"' + schema.replace('"', '""') + '"."' + table.replace('"', '""') + '"'


def describe(conn: psycopg.Connection, schema: str, table: str) -> dict:
    """What a table looks like: exists, kind, columns, primary key, unique keys, row estimate."""
    schema_exists = conn.execute("SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = %s", (schema,)).fetchone()
    row = conn.execute(_REL_SQL, (schema, table)).fetchone()
    out = {"schema": schema, "table": table, "schema_exists": bool(schema_exists), "exists": row is not None}
    if row is None:
        return out
    oid, kind, est, analyzed = row
    cols = []
    for name, typ, cat, notnull, default, identity, generated, seq in conn.execute(
            _COLS_SQL, (_qualified_text(schema, table), oid)).fetchall():
        cols.append({"name": name, "type": typ, "category": cat, "not_null": notnull, "default": default,
                     "identity": {"a": "always", "d": "by default"}.get(identity), "generated": generated == "s",
                     "sequence": seq})
    keys = conn.execute(_KEYS_SQL, (oid,)).fetchall()
    out.update(kind=kind, kind_name=SOURCE_KINDS.get(kind, kind), columns=cols,
               primary_key=next((list(k) for p, k in keys if p), []), unique_keys=[list(k) for _, k in keys],
               est_rows=int(est) if analyzed else None)
    return out


def clean_query(text) -> str:
    """The query without trailing semicolons; it must return rows (SELECT, WITH … SELECT, VALUES, TABLE)."""
    if not isinstance(text, str) or not text.strip():
        raise TransferError("Type the query whose rows are copied.")
    if len(text) > MAX_QUERY_CHARS:
        raise TransferError(f"The query is longer than {MAX_QUERY_CHARS:,} characters.")
    q = text.strip()
    while q.endswith(";"):
        q = q[:-1].rstrip()
    if not _ROWS_QUERY.match(q):
        raise TransferError("Only a query that returns rows can be copied: SELECT, WITH … SELECT, VALUES or TABLE.")
    return q


def _query_from(query: str) -> sql.Composable:
    # new lines around the query: a trailing -- comment must not swallow the closing parenthesis
    return sql.SQL("(\n{}\n) AS pgbs_q").format(sql.SQL(query))


def describe_query(conn: psycopg.Connection, query: str) -> dict:
    """The columns a query returns (names, types) and its row estimate, like `describe` for a table."""
    try:
        cur = conn.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(_query_from(query)))
        res = cur.pgresult
        names = [d.name for d in cur.description or []]
        oids = [res.ftype(i) for i in range(res.nfields)]
        mods = [res.fmod(i) for i in range(res.nfields)]
        types = conn.execute("SELECT pg_catalog.format_type(t.oid, NULLIF(m.mod, -1)), t.typcategory"
                             " FROM unnest(%s::oid[], %s::int[]) WITH ORDINALITY m(oid, mod, n)"
                             " JOIN pg_catalog.pg_type t ON t.oid = m.oid ORDER BY m.n", (oids, mods)).fetchall()
        est = _estimate(conn, sql.SQL("SELECT * FROM {}").format(_query_from(query)))
    except psycopg.Error as e:
        raise TransferError(f"The query does not run: {_primary(e)}") from e
    if not names:
        raise TransferError("The query returns no columns.")
    seen, dupes = set(), []
    for n in names:
        if n in seen and n not in dupes:
            dupes.append(n)
        seen.add(n)
    if dupes:
        raise TransferError(f"The query returns more than one column named {', '.join(dupes)}; give them different "
                            f"names with AS.")
    if "?column?" in names:
        raise TransferError("A column of the query has no name; name it with AS (for example count(*) AS total).")
    cols = [{"name": n, "type": typ, "category": cat, "not_null": False, "default": None, "identity": None,
             "generated": False, "sequence": None} for n, (typ, cat) in zip(names, types)]
    return {"schema": None, "table": None, "schema_exists": True, "exists": True, "kind": "query", "kind_name": "query",
            "columns": cols, "primary_key": [], "unique_keys": [], "est_rows": est}


def _require_supported(conn: psycopg.Connection) -> None:
    try:
        db.require_supported(conn)
    except EngineError:
        conn.close()
        raise


def _connect(server: dict, dbname: str, *, source: bool) -> psycopg.Connection:
    """A transaction-mode connection: the source READ ONLY + REPEATABLE READ, both with a lock timeout."""
    try:
        conn = db.connect(server, dbname)
    except EngineError as e:
        if db._missing_database(e):
            raise TransferError(f"Database {dbname} does not exist on {server.get('name') or server.get('host')}.",
                                "not_found") from e
        raise
    _require_supported(conn)
    if source:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
    conn.execute(sql.SQL("SET lock_timeout = {}").format(sql.Literal(LOCK_TIMEOUT)))
    return conn


# ---------------------------------------------------------------- compare

def _note(src: dict | None, tgt: dict) -> str:
    """How a source column's values reach a target column: same | convert | check."""
    if src is None:
        return "default"
    if src["type"] == tgt["type"]:
        return "same"
    if src["category"] == tgt["category"] or tgt["category"] == "S":
        return "convert"
    return "check"


def suggest_mapping(source: dict, target: dict) -> list[dict]:
    """One row per target column: the source column with the same name (case-insensitive as a fallback)."""
    by_name = {c["name"]: c for c in source["columns"]}
    by_lower: dict[str, dict] = {}
    for c in source["columns"]:
        by_lower.setdefault(c["name"].lower(), c)
    rows = []
    for t in target["columns"]:
        if t["generated"]:
            rows.append({"target": t["name"], "source": None, "note": "generated"})
            continue
        s = by_name.get(t["name"]) or by_lower.get(t["name"].lower())
        rows.append({"target": t["name"], "source": s["name"] if s else None, "note": _note(s, t)})
    return rows


def _identical(source: dict, target: dict) -> bool:
    """Same column names with the same types (order may differ) and the same generated columns."""
    def shape(d):
        return {c["name"]: (c["type"], c["generated"]) for c in d["columns"]}
    return shape(source) == shape(target)


def compare(src_server: dict, src_db: str, src_schema: str | None, src_table: str | None,
            tgt_server: dict, tgt_db: str, tgt_schema: str, tgt_table: str, *, query: str | None = None) -> dict:
    """Source and target side by side: match = identical | different | missing, a suggested mapping, and the
    CREATE TABLE that would be used when the target is missing. With `query` the source is that query's result."""
    names = [(tgt_schema, "target schema"), (tgt_table, "target table")]
    if query is None:
        names = [(src_schema, "source schema"), (src_table, "source table"), *names]
    else:
        query = clean_query(query)
    for value, what in names:
        _check_name(value, what)
    with _closing(_connect(src_server, src_db, source=True)) as src:
        source = describe_query(src, query) if query is not None else describe(src, src_schema, src_table)
    if not source["exists"]:
        raise TransferError(f"Table {src_schema}.{src_table} does not exist in {src_db}.", "not_found")
    with _closing(_connect(tgt_server, tgt_db, source=False)) as tgt:
        target = describe(tgt, tgt_schema, tgt_table)
        create = None
        if not target["exists"]:
            create = [s.as_string(tgt) for s in create_statements(source, tgt_schema, tgt_table,
                                                                     schema_exists=target["schema_exists"])]
    if not target["exists"]:
        match, mapping = "missing", [{"target": c["name"], "source": c["name"], "note": "same"}
                                     for c in source["columns"] if not c["generated"]]
    else:
        if target["kind"] not in TARGET_KINDS:
            raise TransferError(f"{tgt_schema}.{tgt_table} in {tgt_db} is a {SOURCE_KINDS.get(target['kind'], 'relation')}"
                                f"; rows can only be written into a table.")
        match = "identical" if _identical(source, target) else "different"
        mapping = suggest_mapping(source, target)
    return {"match": match, "source": source, "target": target, "mapping": mapping, "create_sql": create}


class _closing:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()


# ---------------------------------------------------------------- spec

@dataclass
class Spec:
    src_db: str
    src_schema: str
    src_table: str
    where: str | None
    tgt_db: str
    tgt_schema: str
    tgt_table: str
    create: bool
    columns: list[tuple[str, str]]          # (target column, source column)
    existing: str = "append"
    confirm: str | None = None
    same_server: bool = False
    tgt_server_id: str | None = None
    query: str | None = None                # the source is this query's result instead of a table

    @property
    def source_table(self) -> str:
        return "query" if self.query is not None else f"{self.src_schema}.{self.src_table}"

    @property
    def source_label(self) -> str:
        return f"{self.src_db} (query)" if self.query is not None else f"{self.src_db}.{self.src_schema}.{self.src_table}"

    @property
    def target_label(self) -> str:
        return f"{self.tgt_db}.{self.tgt_schema}.{self.tgt_table}"


def _check_name(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TransferError(f"Choose the {what}.")
    if len(value.encode("utf-8")) > MAX_NAME_BYTES:
        raise TransferError(f"The {what} is longer than {MAX_NAME_BYTES} bytes.")
    if "\x00" in value:
        raise TransferError(f"The {what} contains a NUL character.")
    return value


def parse_spec(data, *, same_server: bool) -> Spec:
    """Check the shape of a transfer request (the catalog checks come later, in `plan`)."""
    if not isinstance(data, dict):
        raise TransferError("The transfer request must be an object.")
    src, tgt = data.get("source"), data.get("target")
    if not isinstance(src, dict) or not isinstance(tgt, dict):
        raise TransferError("Choose a source table and a target table.")
    query = src.get("query")
    if query is not None:
        query = clean_query(query)
    where = src.get("where") if query is None else None
    if where is not None:
        if not isinstance(where, str):
            raise TransferError("The row filter must be text.")
        where = where.strip() or None
        if where and len(where) > MAX_FILTER_CHARS:
            raise TransferError(f"The row filter is longer than {MAX_FILTER_CHARS:,} characters.")
        if where and re.match(r"(?is)^where\s", where):
            where = where[5:].strip() or None
    create = data.get("create", False)
    if not isinstance(create, bool):
        raise TransferError("'create' must be true or false.")
    existing = data.get("existing") or "append"
    if existing not in EXISTING_MODES:
        raise TransferError("Choose what happens to rows already in the target: append, skip or replace.")
    columns = []
    raw = data.get("columns")
    if not create:
        if not isinstance(raw, list) or not raw:
            raise TransferError("Map at least one column.")
        seen = set()
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("target"), str) or not isinstance(item.get("source"), str):
                raise TransferError("Each column mapping needs a target column and a source column.")
            if item["target"] in seen:
                raise TransferError(f"Target column {item['target']} is mapped twice.")
            seen.add(item["target"])
            columns.append((item["target"], item["source"]))
    confirm = data.get("confirm")
    spec = Spec(src_db=_check_name(src.get("database"), "source database"),
                src_schema=None if query is not None else _check_name(src.get("schema"), "source schema"),
                src_table=None if query is not None else _check_name(src.get("table"), "source table"), where=where,
                tgt_db=_check_name(tgt.get("database"), "target database"),
                tgt_schema=_check_name(tgt.get("schema"), "target schema"),
                tgt_table=_check_name(tgt.get("table"), "target table"),
                create=create, columns=columns, existing="append" if create else existing,
                confirm=confirm if isinstance(confirm, str) else None, same_server=same_server,
                tgt_server_id=tgt.get("server_id"), query=query)
    if query is None and same_server and (spec.src_db, spec.src_schema, spec.src_table) == (spec.tgt_db, spec.tgt_schema, spec.tgt_table):
        raise TransferError("The source and the target are the same table; choose another target.")
    if spec.existing == "replace" and spec.confirm != spec.tgt_table:
        raise TransferError(f"Type the target table name ({spec.tgt_table}) to confirm that its rows are deleted first.")
    return spec


# ---------------------------------------------------------------- plan

def create_statements(source: dict, schema: str, table: str, *, schema_exists: bool) -> list[sql.Composable]:
    """CREATE [SCHEMA and] TABLE with the source's columns, NOT NULLs, defaults and primary key. Serial columns
    become identity columns (the source's sequence does not exist on the target)."""
    defs = []
    for c in source["columns"]:
        parts = [sql.Identifier(c["name"]), sql.SQL(c["type"])]
        default = c["default"] or ""
        if c["generated"]:
            parts.append(sql.SQL("GENERATED ALWAYS AS ({}) STORED").format(sql.SQL(default)))
        elif c["identity"]:
            parts.append(sql.SQL("GENERATED ALWAYS AS IDENTITY" if c["identity"] == "always"
                                 else "GENERATED BY DEFAULT AS IDENTITY"))
        elif _NEXTVAL.match(default) and c["type"] in _SERIAL_TYPES:
            parts.append(sql.SQL("GENERATED BY DEFAULT AS IDENTITY"))
        elif default and not _NEXTVAL.match(default):
            parts.append(sql.SQL("DEFAULT {}").format(sql.SQL(default)))
        if c["not_null"] and not c["identity"] and not (_NEXTVAL.match(default) and c["type"] in _SERIAL_TYPES):
            parts.append(sql.SQL("NOT NULL"))
        defs.append(sql.SQL(" ").join(parts))
    if source.get("primary_key"):
        defs.append(sql.SQL("PRIMARY KEY ({})").format(sql.SQL(", ").join(map(sql.Identifier, source["primary_key"]))))
    out: list[sql.Composable] = []
    if not schema_exists:
        out.append(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    out.append(sql.SQL("CREATE TABLE {} (\n  {}\n)").format(sql.Identifier(schema, table), sql.SQL(",\n  ").join(defs)))
    return out


@dataclass
class Plan:
    source: dict
    target: dict
    pairs: list[tuple[dict, dict]]                          # (target column, source column)
    source_copy: sql.Composable
    prepare: list[tuple[str, sql.Composable]] = field(default_factory=list)   # (log text, statement)
    target_copy: sql.Composable | None = None
    stage: sql.Composable | None = None
    insert: sql.Composable | None = None
    sequences: list[tuple[str, str]] = field(default_factory=list)            # (column, sequence)
    est_rows: int | None = None
    warnings: list[str] = field(default_factory=list)


def _target_after_create(source: dict, spec: Spec) -> dict:
    cols = []
    for c in source["columns"]:
        serial = bool(_NEXTVAL.match(c["default"] or "")) and c["type"] in _SERIAL_TYPES
        identity = c["identity"] or ("by default" if serial else None)
        cols.append({**c, "identity": identity, "default": None if serial else c["default"],
                     "sequence": "(new identity sequence)" if identity else None})
    return {"schema": spec.tgt_schema, "table": spec.tgt_table, "exists": False, "kind": "r", "columns": cols,
            "primary_key": source.get("primary_key") or [], "unique_keys": [], "est_rows": 0}


def _estimate(src: psycopg.Connection, query: sql.Composable) -> int | None:
    row = src.execute(sql.SQL("EXPLAIN (FORMAT JSON) {}").format(query)).fetchone()
    plan = row[0] if not isinstance(row[0], str) else json.loads(row[0])
    try:
        return int(plan[0]["Plan"]["Plan Rows"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def plan(src: psycopg.Connection, tgt: psycopg.Connection, spec: Spec) -> Plan:
    """Check the spec against both catalogs and build every statement. Raises TransferError."""
    if spec.query is not None:
        source = describe_query(src, spec.query)
    else:
        source = describe(src, spec.src_schema, spec.src_table)
    if not source["exists"]:
        raise TransferError(f"Table {spec.src_schema}.{spec.src_table} does not exist in {spec.src_db}.", "not_found")
    if spec.query is None and source["kind"] not in SOURCE_KINDS:
        raise TransferError(f"{spec.src_schema}.{spec.src_table} cannot be read as a table.")
    target = describe(tgt, spec.tgt_schema, spec.tgt_table)
    prepare: list[tuple[str, sql.Composable]] = []
    tname = sql.Identifier(spec.tgt_schema, spec.tgt_table)
    if spec.create:
        if target["exists"]:
            raise TransferError(f"Table {spec.tgt_schema}.{spec.tgt_table} already exists in {spec.tgt_db}; "
                                f"compare again to map its columns.", "exists")
        for st in create_statements(source, spec.tgt_schema, spec.tgt_table, schema_exists=target["schema_exists"]):
            prepare.append(("Created " + ("schema " + spec.tgt_schema if st.as_string(tgt).startswith("CREATE SCHEMA")
                                          else "table " + f"{spec.tgt_schema}.{spec.tgt_table}"), st))
        target = _target_after_create(source, spec)
        names = [c["name"] for c in source["columns"] if not c["generated"]]
        columns = [(n, n) for n in names]
    else:
        if not target["exists"]:
            raise TransferError(f"Table {spec.tgt_schema}.{spec.tgt_table} does not exist in {spec.tgt_db}"
                                f"{'' if target['schema_exists'] else ' (nor does the schema)'}; choose to create it.",
                                "not_found")
        if target["kind"] not in TARGET_KINDS:
            raise TransferError(f"{spec.tgt_schema}.{spec.tgt_table} is a {SOURCE_KINDS.get(target['kind'], 'relation')}; "
                                f"rows can only be written into a table.")
        columns = spec.columns
    src_cols = {c["name"]: c for c in source["columns"]}
    tgt_cols = {c["name"]: c for c in target["columns"]}
    pairs = []
    for t, s in columns:
        if t not in tgt_cols:
            raise TransferError(f"Target column {t} does not exist in {spec.tgt_schema}.{spec.tgt_table}.")
        if s not in src_cols:
            raise TransferError(f"Source column {s} does not exist in "
                                f"{'the query result' if spec.query is not None else spec.source_table}.")
        if tgt_cols[t]["generated"]:
            raise TransferError(f"Target column {t} is generated; its values are computed and cannot be written.")
        pairs.append((tgt_cols[t], src_cols[s]))
    mapped = {t["name"] for t, _ in pairs}
    missing = [c["name"] for c in target["columns"] if c["name"] not in mapped and c["not_null"]
               and not c["default"] and not c["identity"] and not c["generated"]]
    if missing:
        raise TransferError(f"Target column{'s' if len(missing) > 1 else ''} {', '.join(missing)} cannot be empty "
                            f"(NOT NULL with no default); map a source column to {'them' if len(missing) > 1 else 'it'}.")
    warnings = []
    for t, s in pairs:
        if _note(s, t) == "check":
            warnings.append(f"{s['name']} ({s['type']}) → {t['name']} ({t['type']}): different kinds of type; "
                            f"rows whose value cannot be read as {t['type']} stop the transfer.")
    unmapped_src = [c for c in src_cols if c not in {s["name"] for _, s in pairs}]
    if unmapped_src:
        warnings.append(f"Not transferred: {', '.join(unmapped_src)}.")

    select = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(sql.Identifier(s["name"]) for _, s in pairs),
        _query_from(spec.query) if spec.query is not None else sql.Identifier(spec.src_schema, spec.src_table))
    if spec.where:
        select = sql.SQL("{} WHERE ({})").format(select, sql.SQL(spec.where))
    try:
        est = _estimate(src, select) if spec.where else source["est_rows"]
    except psycopg.Error as e:
        raise TransferError(f"The row filter is not valid: {_primary(e)}") from e

    tcols = sql.SQL(", ").join(sql.Identifier(t["name"]) for t, _ in pairs)
    out = Plan(source=source, target=target, pairs=pairs, est_rows=est, warnings=warnings, prepare=prepare,
               source_copy=sql.SQL("COPY ({}) TO STDOUT").format(select))
    if spec.existing == "replace":
        out.prepare.append((f"Deleted the rows in {spec.tgt_schema}.{spec.tgt_table}", sql.SQL("TRUNCATE TABLE {}").format(tname)))
    if spec.existing == "skip":
        if not target["unique_keys"]:
            raise TransferError(f"{spec.tgt_schema}.{spec.tgt_table} has no primary key or unique constraint, so rows "
                                f"that already exist cannot be recognised; choose append or replace.")
        stage = sql.Identifier(STAGE_TABLE)
        out.stage = sql.SQL("CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT {} FROM {} WITH NO DATA").format(stage, tcols, tname)
        out.target_copy = sql.SQL("COPY {} ({}) FROM STDIN").format(stage, tcols)
        overriding = sql.SQL(" OVERRIDING SYSTEM VALUE") if any(t["identity"] == "always" for t, _ in pairs) else sql.SQL("")
        out.insert = sql.SQL("INSERT INTO {} ({}){} SELECT {} FROM {} ON CONFLICT DO NOTHING").format(
            tname, tcols, overriding, tcols, stage)
    else:
        out.target_copy = sql.SQL("COPY {} ({}) FROM STDIN").format(tname, tcols)
    out.sequences = [(t["name"], t["sequence"]) for t, _ in pairs if t.get("sequence")]
    return out


def _sequence_sql(spec: Spec, column: str) -> sql.Composable:
    """Move the column's sequence forward past the largest value (never backwards)."""
    seq = sql.SQL("pg_catalog.pg_get_serial_sequence({}, {})").format(
        sql.Literal(_qualified_text(spec.tgt_schema, spec.tgt_table)), sql.Literal(column))
    return sql.SQL("SELECT pg_catalog.setval({seq}, m) FROM (SELECT max({col}) AS m FROM {t}) x"
                   " WHERE m > COALESCE(pg_catalog.pg_sequence_last_value({seq}), 0)").format(
        seq=seq, col=sql.Identifier(column), t=sql.Identifier(spec.tgt_schema, spec.tgt_table))


def statements(p: Plan, spec: Spec, tgt: psycopg.Connection) -> list[dict]:
    """The SQL to show before starting: [{on: source | target, sql}]."""
    out = [{"on": "source", "sql": p.source_copy.as_string(tgt)}]
    out += [{"on": "target", "sql": st.as_string(tgt)} for _, st in p.prepare]
    if p.stage is not None:
        out.append({"on": "target", "sql": p.stage.as_string(tgt)})
    out.append({"on": "target", "sql": p.target_copy.as_string(tgt)})
    if p.insert is not None:
        out.append({"on": "target", "sql": p.insert.as_string(tgt)})
    out += [{"on": "target", "sql": _sequence_sql(spec, c).as_string(tgt)} for c, _ in p.sequences]
    return out


def preview(src_server: dict, tgt_server: dict, spec: Spec) -> dict:
    """Validate the spec and return the statements, the row estimate and warnings (nothing is changed)."""
    with _closing(_connect(src_server, spec.src_db, source=True)) as src, \
            _closing(_connect(tgt_server, spec.tgt_db, source=False)) as tgt:
        try:
            p = plan(src, tgt, spec)
            return {"statements": statements(p, spec, tgt), "est_rows": p.est_rows, "warnings": p.warnings,
                    "columns": [{"target": t["name"], "source": s["name"]} for t, s in p.pairs],
                    "sequences": [c for c, _ in p.sequences]}
        except psycopg.Error as e:
            raise _error(e) from e


# ---------------------------------------------------------------- run

def _error(e: psycopg.Error) -> TransferError:
    """A clear sentence for a failed transfer; nothing was committed on the target."""
    diag = getattr(e, "diag", None)
    context = (diag.context or "") if diag else ""
    where = ""
    m = re.search(r"COPY [^,]+, line (\d+)(?:, column ([^:]+))?", context)
    if m:
        where = f" (row {int(m.group(1)):,}" + (f", column {m.group(2)}" if m.group(2) else "") + ")"
    detail = f" {diag.message_detail}" if diag and diag.message_detail else ""
    hint = ""
    code = "failed"
    if isinstance(e, errors.UniqueViolation):
        hint = " Choose \"skip rows that already exist\" or \"delete existing rows first\"."
    elif isinstance(e, errors.InsufficientPrivilege):
        code = "permission"
    elif isinstance(e, errors.LockNotAvailable):
        hint = f" Another session holds a lock on the table (waited {LOCK_TIMEOUT})."
    elif isinstance(e, errors.FeatureNotSupported) and "truncate" in str(e).lower():
        hint = " Other tables point at its rows (foreign keys); choose append or skip instead."
    text = f"{_primary(e)}{where}".rstrip(".") + "." + detail + hint
    return TransferError(f"{text} Nothing was changed in the target.", code)


def run_transfer(src_server: dict, tgt_server: dict, spec: Spec, progress=None, cancel=None) -> dict:
    """Copy the rows. Progress events: phase {name}, totals {est_rows}, table_start/table_progress {rows},
    log {level, msg}. Returns {rows_read, rows_written, rows_skipped, created, truncated, sequences}."""
    emit = progress or (lambda event, data: None)
    emit("phase", {"name": "connecting"})
    src = _connect(src_server, spec.src_db, source=True)
    try:
        tgt = _connect(tgt_server, spec.tgt_db, source=False)
        try:
            return _run(src, tgt, spec, emit, cancel)
        finally:
            tgt.close()
    finally:
        src.close()


def _run(src, tgt, spec: Spec, emit, cancel) -> dict:
    check_cancel(cancel)
    try:
        with db.cancel_on_event(src, cancel), db.cancel_on_event(tgt, cancel):
            emit("phase", {"name": "preparing"})
            p = plan(src, tgt, spec)
            for msg in p.warnings:
                emit("warning", {"msg": msg})
            emit("totals", {"est_rows": p.est_rows or 0})
            for text, st in p.prepare:
                check_cancel(cancel)
                tgt.execute(st)
                emit("log", {"level": "info", "msg": text})
            if p.stage is not None:
                tgt.execute(p.stage)
            check_cancel(cancel)
            emit("phase", {"name": "copy"})
            emit("table_start", {"name": f"{spec.source_label} → {spec.target_label}", "est_rows": p.est_rows or 0})
            rows, last = 0, time.monotonic()
            with src.cursor().copy(p.source_copy) as out, tgt.cursor().copy(p.target_copy) as into:
                for chunk in out:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    data = bytes(chunk)
                    into.write(data)
                    rows += data.count(b"\n")
                    if time.monotonic() - last >= PROGRESS_EVERY_S:
                        emit("table_progress", {"rows": rows})
                        last = time.monotonic()
            emit("table_progress", {"rows": rows})
            check_cancel(cancel)
            emit("phase", {"name": "finishing"})
            written = rows
            if p.insert is not None:
                written = tgt.execute(p.insert).rowcount
                emit("log", {"level": "info", "msg": f"Inserted {written:,} rows; {rows - written:,} already existed"})
            moved = []
            for column, _seq in p.sequences:
                check_cancel(cancel)
                try:
                    with tgt.transaction():
                        if tgt.execute(_sequence_sql(spec, column)).fetchone():
                            moved.append(column)
                except psycopg.Error as e:
                    if isinstance(e, errors.QueryCanceled) and cancel is not None and cancel.is_set():
                        raise
                    emit("warning", {"msg": f"The sequence of {column} was not moved forward: {_primary(e)}"})
            if moved:
                emit("log", {"level": "info", "msg": f"Moved sequences forward: {', '.join(moved)}"})
            check_cancel(cancel)
            tgt.commit()
    except errors.QueryCanceled as e:
        if cancel is not None and cancel.is_set():
            raise Cancelled() from e
        raise _error(e) from e
    except psycopg.Error as e:
        raise _error(e) from e
    return {"rows_read": rows, "rows_written": written, "rows_skipped": rows - written, "created": spec.create,
            "truncated": spec.existing == "replace", "sequences": moved, "mode": spec.existing,
            "source": spec.source_label, "target": spec.target_label}
