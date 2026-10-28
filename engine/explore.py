"""Database metadata for the SQL workspace: names for suggestions, the explorer tree, object details, DDL.

All reads go through pg_catalog with bind parameters; nothing is written.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from . import catalog
from .backup import BackupOptions, _PlanBuilder   # DDL reuses the backup's renderers
from .util import EngineError

MAX_RELATIONS = 20000
MAX_FUNCTIONS = 20000

_USER_SCHEMAS = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
_REL_KINDS = {"r": "table", "p": "table", "v": "view", "m": "matview", "f": "foreign"}


def _rows(conn: psycopg.Connection, query: str, params=None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def completions(conn: psycopg.Connection) -> dict:
    """Names for the editor's suggestions.

    `schemas`: every schema; `search_path`: schemas searched for unqualified names; `relations`:
    tables, views, materialized views and foreign tables of the user schemas plus pg_catalog and
    information_schema (`system: true`); `functions`: user functions with arguments and the names
    of the built-in ones.
    """
    schemas = [r["nspname"] for r in _rows(conn, "SELECT nspname FROM pg_namespace"
                                                 " WHERE nspname !~ '^pg_(toast|temp)' ORDER BY nspname")]
    search_path = conn.execute("SELECT current_schemas(false)").fetchone()[0]
    rels = _rows(conn, "SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind,"
                       f" NOT ({_USER_SCHEMAS}) AS system"
                       " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                       " WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition"
                       " AND n.nspname !~ '^pg_(toast|temp)'"
                       f" ORDER BY system, n.nspname, c.relname LIMIT {MAX_RELATIONS + 1}")
    funcs = _rows(conn, "SELECT n.nspname AS schema, p.proname AS name, pg_get_function_arguments(p.oid) AS args,"
                        " pg_get_function_result(p.oid) AS result, p.prokind AS kind"
                        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                        f" WHERE {_USER_SCHEMAS} ORDER BY n.nspname, p.proname LIMIT {MAX_FUNCTIONS}")
    builtin = [r["proname"] for r in _rows(conn, "SELECT DISTINCT proname FROM pg_proc"
                                                 " WHERE pronamespace = 'pg_catalog'::regnamespace"
                                                 " AND proname !~ '^_' ORDER BY proname")]
    for r in rels:
        r["kind"] = _REL_KINDS[r["kind"]]
    for f in funcs:
        f["kind"] = "procedure" if f["kind"] == "p" else "aggregate" if f["kind"] == "a" else "function"
    return {"schemas": schemas, "search_path": list(search_path), "relations": rels[:MAX_RELATIONS],
            "relations_truncated": len(rels) > MAX_RELATIONS, "functions": funcs, "builtin_functions": builtin}


def columns(conn: psycopg.Connection, schema: str, table: str) -> list[dict]:
    """Columns of a table, view or materialized view: name, type, nullable, default, pk."""
    return _rows(conn, "SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,"
                       " NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS default,"
                       " COALESCE(a.attnum = ANY(k.conkey), false) AS pk"
                       " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                       " JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped"
                       " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                       " LEFT JOIN pg_constraint k ON k.conrelid = c.oid AND k.contype = 'p'"
                       " WHERE n.nspname = %s AND c.relname = %s ORDER BY a.attnum", (schema, table))


# ---------------------------------------------------------------- explorer tree (8b)

MAX_OBJECTS = 20000
_GROUPS = {"r": "tables", "p": "tables", "v": "views", "m": "matviews", "f": "foreign", "S": "sequences"}
_PROC_GROUPS = {"f": "functions", "w": "functions", "p": "procedures", "a": "aggregates"}
_TYPE_KINDS = {"e": "enum", "d": "domain", "r": "range", "c": "composite"}
_REL_DETAIL_KINDS = {"r": "table", "p": "table", "v": "view", "m": "matview", "f": "foreign", "S": "sequence"}


def schemas(conn: psycopg.Connection) -> list[dict]:
    """Every schema except toast/temp ones: name, owner, comment, `system` (pg_catalog, information_schema)."""
    return _rows(conn, "SELECT n.nspname AS name, pg_get_userbyid(n.nspowner) AS owner,"
                       " obj_description(n.oid, 'pg_namespace') AS comment,"
                       " n.nspname IN ('pg_catalog', 'information_schema') AS system"
                       " FROM pg_namespace n WHERE n.nspname !~ '^pg_(toast|temp)'"
                       " AND (n.nspname !~ '^pg_' OR n.nspname = 'pg_catalog')"
                       " ORDER BY 4, n.nspname")


def objects(conn: psycopg.Connection, schema: str) -> dict:
    """The objects of one schema, grouped for the explorer tree.

    Groups: tables (with partitioned ones; partitions are left out, their parent shows all rows),
    views, matviews, foreign, sequences (name, rows estimate, size, comment), functions, procedures,
    aggregates (oid, name, args, result) and types (oid, name, kind). `truncated` names the
    lists cut at MAX_OBJECTS.
    """
    if not _rows(conn, "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)):
        raise EngineError(f"Schema {schema} does not exist (any more); refresh the explorer.")
    out: dict = {"schema": schema, "tables": [], "views": [], "matviews": [], "foreign": [], "sequences": [],
                 "functions": [], "procedures": [], "aggregates": [], "types": [], "truncated": []}
    rels = _rows(conn, "SELECT c.oid, c.relname AS name, c.relkind AS kind,"
                       " CASE WHEN c.relkind IN ('r', 'm') THEN c.reltuples::float8 END AS rows,"
                       " CASE WHEN c.relkind IN ('r', 'm') THEN pg_total_relation_size(c.oid) END AS size,"
                       " obj_description(c.oid, 'pg_class') AS comment"
                       " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                       " WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND NOT c.relispartition"
                       f" ORDER BY c.relname LIMIT {MAX_OBJECTS + 1}", (schema,))
    procs = _rows(conn, "SELECT p.oid, p.proname AS name, pg_get_function_identity_arguments(p.oid) AS args,"
                        " pg_get_function_result(p.oid) AS result, p.prokind AS kind,"
                        " obj_description(p.oid, 'pg_proc') AS comment"
                        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s"
                        f" ORDER BY p.proname, 3 LIMIT {MAX_OBJECTS + 1}", (schema,))
    types = _rows(conn, "SELECT t.oid, t.typname AS name, t.typtype AS kind,"
                        " obj_description(t.oid, 'pg_type') AS comment"
                        " FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = %s"
                        " AND (t.typtype IN ('e', 'd', 'r')"
                        "   OR (t.typtype = 'c' AND (SELECT c.relkind FROM pg_class c WHERE c.oid = t.typrelid) = 'c'))"
                        f" ORDER BY t.typname LIMIT {MAX_OBJECTS + 1}", (schema,))
    for label, rows in (("relations", rels), ("routines", procs), ("types", types)):
        if len(rows) > MAX_OBJECTS:
            out["truncated"].append(label)
            del rows[MAX_OBJECTS:]
    for r in rels:
        r["partitioned"] = r["kind"] == "p"
        out[_GROUPS[r.pop("kind")]].append(r)
    for p in procs:
        out[_PROC_GROUPS[p.pop("kind")]].append(p)
    for t in types:
        t["kind"] = _TYPE_KINDS[t["kind"]]
        out["types"].append(t)
    return out


def _relation(conn: psycopg.Connection, schema: str, name: str) -> dict:
    rows = _rows(conn, "SELECT c.oid, c.relkind AS kind, c.reltuples::float8 AS rows,"
                       " CASE WHEN c.relkind IN ('r', 'p', 'm') THEN pg_total_relation_size(c.oid) END AS size,"
                       " obj_description(c.oid, 'pg_class') AS comment"
                       " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                       " WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')",
                 (schema, name))
    if not rows:
        raise EngineError(f"{schema}.{name} does not exist (any more); refresh the explorer.")
    return rows[0]


def object_detail(conn: psycopg.Connection, schema: str, name: str) -> dict:
    """A table, view, materialized view or foreign table: columns, constraints, indexes, triggers."""
    rel = _relation(conn, schema, name)
    oid = rel["oid"]
    cols = _rows(conn, "SELECT a.attname AS name, format_type(a.atttypid, a.atttypmod) AS type,"
                       " NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS default,"
                       " COALESCE(a.attnum = ANY(k.conkey), false) AS pk,"
                       " col_description(a.attrelid, a.attnum) AS comment"
                       " FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                       " LEFT JOIN pg_constraint k ON k.conrelid = a.attrelid AND k.contype = 'p'"
                       " WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum", (oid,))
    cons = _rows(conn, "SELECT conname AS name, contype AS kind, pg_get_constraintdef(oid) AS def"
                       " FROM pg_constraint WHERE conrelid = %s AND contype <> 'n'"
                       " ORDER BY position(contype in 'pufxct'), conname", (oid,))
    kinds = {"p": "primary key", "u": "unique", "f": "foreign key", "x": "exclusion", "c": "check", "t": "trigger"}
    for k in cons:
        k["kind"] = kinds.get(k["kind"], k["kind"])
    idx = _rows(conn, "SELECT ic.relname AS name, pg_get_indexdef(x.indexrelid) AS def, x.indisprimary AS primary,"
                      " x.indisunique AS unique, pg_relation_size(x.indexrelid) AS size"
                      " FROM pg_index x JOIN pg_class ic ON ic.oid = x.indexrelid WHERE x.indrelid = %s"
                      " ORDER BY NOT x.indisprimary, ic.relname", (oid,))
    trg = _rows(conn, "SELECT tgname AS name, pg_get_triggerdef(oid) AS def, tgenabled <> 'D' AS enabled"
                      " FROM pg_trigger WHERE tgrelid = %s AND NOT tgisinternal ORDER BY tgname", (oid,))
    return {"schema": schema, "name": name, "oid": oid, "kind": _REL_DETAIL_KINDS[rel["kind"]],
            "partitioned": rel["kind"] == "p", "rows": rel["rows"], "size": rel["size"], "comment": rel["comment"],
            "columns": cols, "constraints": cons, "indexes": idx, "triggers": trg}


# ---------------------------------------------------------------- DDL (8b)

DDL_KINDS = ("table", "view", "matview", "foreign", "sequence", "function", "procedure", "aggregate", "type")
_ROUTINES = ("function", "procedure", "aggregate")


def _target(conn: psycopg.Connection, schema: str, kind: str, name: str | None, oid) -> tuple[str, int, str]:
    """(catalog, oid, display name) of the object asked for; routines need `oid` (names can be overloaded)."""
    if kind not in DDL_KINDS:
        raise EngineError(f"Unknown object kind {kind!r}; use one of {', '.join(DDL_KINDS)}.")
    if kind in _ROUTINES:
        if not isinstance(oid, int) or isinstance(oid, bool):
            raise EngineError("'oid' is required for a function, procedure or aggregate.")
        rows = _rows(conn, "SELECT p.oid::regprocedure::text AS label FROM pg_proc p"
                           " JOIN pg_namespace n ON n.oid = p.pronamespace WHERE p.oid = %s AND n.nspname = %s",
                     (oid, schema))
        if not rows:
            raise EngineError("This routine does not exist (any more); refresh the explorer.")
        return "pg_proc", oid, rows[0]["label"]
    if not name:
        raise EngineError("'name' is required.")
    if kind == "type":
        rows = _rows(conn, "SELECT t.oid FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace"
                           " WHERE n.nspname = %s AND t.typname = %s", (schema, name))
        if not rows:
            raise EngineError(f"Type {schema}.{name} does not exist (any more); refresh the explorer.")
        return "pg_type", rows[0]["oid"], f"{schema}.{name}"
    return "pg_class", _relation(conn, schema, name)["oid"], f"{schema}.{name}"


def _object_keys(builder, cat, target: tuple[str, int]) -> set:
    """Plan nodes that belong to one object: the object itself plus what hangs on it."""
    cat_name, oid = target
    keys = {target}
    if cat_name == "pg_class":
        keys.add(("rls", oid))
        keys |= {("pg_attrdef", c["def_oid"]) for c in cat.columns if c["relid"] == oid and c["def_oid"]}
        for group, catalog_name in ((cat.constraints, "pg_constraint"), (cat.indexes, "pg_class"),
                                    (cat.statistics, "pg_statistic_ext"), (cat.triggers, "pg_trigger"),
                                    (cat.rules, "pg_rewrite"), (cat.policies, "pg_policy")):
            keys |= {(catalog_name, x["oid"]) for x in group if x["relid"] == oid}
        table = next((t for t in cat.tables if t["oid"] == oid), None)
        for s in cat.sequences:   # serial / OWNED BY sequences come with their table
            if table and (s["owned_by"] or "").startswith(table["qname"] + "."):
                keys |= {("pg_class", s["oid"]), ("owned_by", s["oid"])}
            elif s["oid"] == oid:
                keys.add(("owned_by", oid))
    elif cat_name == "pg_type":
        keys |= {("pg_constraint", k["oid"]) for k in cat.constraints if k["typid"] == oid}
    return {k for k in keys if k in builder.nodes}


def ddl(conn: psycopg.Connection, schema: str, kind: str, name: str | None = None, oid=None) -> dict:
    """CREATE statements for one object, as a schema-only backup would write them.

    User schemas go through the backup's own plan builder (owner, grants, comments, constraints,
    indexes, triggers, policies and owned sequences included). Objects it does not cover (system
    schemas, extension members) fall back to pg_get_viewdef / pg_get_functiondef.
    """
    target = _target(conn, schema, kind, name, oid)
    text = ""
    if schema in {s["name"] for s in catalog.resolve_schemas(conn, None)}:
        cat = catalog.load_all(conn, [schema], conn.info.server_version)
        builder = _PlanBuilder(cat, BackupOptions(mode="schema"))
        builder.add_nodes()
        builder.add_dependencies()
        builder.assign_phases(collect_warnings=False)
        keys = sorted(_object_keys(builder, cat, target[:2]), key=lambda k: (builder.eff[k], builder.nodes[k].sort))
        text = "\n".join(t for t in (builder.nodes[k].render() for k in keys) if t)
    return {"schema": schema, "kind": kind, "name": target[2], "ddl": text or _fallback_ddl(conn, target)}


def _fallback_ddl(conn: psycopg.Connection, target: tuple[str, int, str]) -> str:
    cat_name, oid, label = target
    if cat_name == "pg_proc":
        row = conn.execute("SELECT CASE WHEN prokind <> 'a' THEN pg_get_functiondef(oid) END FROM pg_proc"
                           " WHERE oid = %s", (oid,)).fetchone()
        if row and row[0]:
            return row[0].rstrip() + ";\n"
    elif cat_name == "pg_class":
        row = conn.execute("SELECT relkind, oid::regclass::text, pg_get_viewdef(oid) FROM pg_class WHERE oid = %s",
                           (oid,)).fetchone()
        if row and row[0] in ("v", "m"):
            word = "MATERIALIZED VIEW" if row[0] == "m" else "VIEW"
            return f"CREATE {word} {row[1]} AS\n{row[2].strip()}\n"
    return (f"-- No DDL for {label}: it is in a system schema or belongs to an extension.\n"
            "-- Extensions create their own objects (see CREATE EXTENSION).\n")
