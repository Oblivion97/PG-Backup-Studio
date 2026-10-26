"""Catalog readers: one query per object type, version-gated, limited to the selected schemas.

Every reader takes `(conn, ctx)` and returns a list of dicts. Objects owned by an extension
(`pg_depend.deptype = 'e'`) or internally created (`'i'`) are excluded. The session must run
with `search_path = pg_catalog`, so names rendered by the server are schema-qualified.
All queries use bind parameters; a literal `%` is written `%%`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .util import BackupError

USER_OID = 16384  # FirstNormalObjectId: everything below belongs to the system


@dataclass
class CatalogContext:
    """Server version and the OIDs of the schemas being backed up."""
    version_num: int
    schema_oids: list[int]


@dataclass
class Catalog:
    """Everything `backup.build_plan` needs, read inside one snapshot."""
    version_num: int
    schemas: list[dict] = field(default_factory=list)
    extensions: list[dict] = field(default_factory=list)
    types: list[dict] = field(default_factory=list)
    sequences: list[dict] = field(default_factory=list)
    functions: list[dict] = field(default_factory=list)
    aggregates: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    columns: list[dict] = field(default_factory=list)
    inherits: list[dict] = field(default_factory=list)
    constraints: list[dict] = field(default_factory=list)
    indexes: list[dict] = field(default_factory=list)
    statistics: list[dict] = field(default_factory=list)
    views: list[dict] = field(default_factory=list)
    triggers: list[dict] = field(default_factory=list)
    rules: list[dict] = field(default_factory=list)
    policies: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    acls: list[dict] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    aliases: list[dict] = field(default_factory=list)
    sequence_values: dict = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _not_member(cat: str, oid_expr: str) -> str:
    """SQL condition: the object is neither extension-owned nor internally created.

    Self references are ignored: a partitioned table has an internal dependency on itself
    (partition key columns, verified on PG 18).
    """
    return (f"NOT EXISTS (SELECT 1 FROM pg_depend dx WHERE dx.classid = '{cat}'::regclass"
            f" AND dx.objid = {oid_expr} AND dx.deptype IN ('e', 'i')"
            f" AND NOT (dx.refclassid = dx.classid AND dx.refobjid = dx.objid))")


def _collation_if_differs(coll: str, type_coll: str) -> str:
    """SQL expression: qualified collation name when it is not the type's default."""
    return (f"CASE WHEN {coll} <> 0 AND {coll} IS DISTINCT FROM {type_coll} THEN"
            f" (SELECT format('%%I.%%I', cn.nspname, co.collname) FROM pg_collation co"
            f" JOIN pg_namespace cn ON cn.oid = co.collnamespace WHERE co.oid = {coll}) END")


def _rows(conn: psycopg.Connection, query: str, ctx: CatalogContext, **params) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, {"schemas": ctx.schema_oids, **params})
        return cur.fetchall()


IN_SCHEMAS = "= ANY(%(schemas)s::oid[])"


def resolve_schemas(conn: psycopg.Connection, names: list[str] | None) -> list[dict]:
    """User schemas (oid, name), optionally limited to `names`; unknown names raise BackupError."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT n.oid, n.nspname AS name FROM pg_namespace n"
                    f" WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
                    f" AND {_not_member('pg_namespace', 'n.oid')} ORDER BY n.nspname")
        rows = cur.fetchall()
    if names is None:
        return rows
    by_name = {r["name"]: r for r in rows}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise BackupError(f"Schema(s) not found in the database: {', '.join(missing)}.")
    return [by_name[n] for n in sorted(set(names))]


def list_relations(conn: psycopg.Connection, schemas: list[str] | None) -> list[dict]:
    """Tables, partitioned tables and sequences of the selected schemas, read without taking locks.

    Keys: oid, schema_oid, schema, name, qname, kind ('r'|'p'|'S'), is_partition, parent_oid
    (partition parent), root_oid (top partitioned ancestor inside the selection, else itself),
    owned_by_relid (table owning a sequence via OWNED BY or identity) and can_lock (the login
    may SELECT it, which LOCK ... ACCESS SHARE requires). Extension members are left out.
    """
    selected = resolve_schemas(conn, schemas)
    ctx = CatalogContext(conn.info.server_version, [s["oid"] for s in selected])
    rows = _rows(conn, "SELECT c.oid, n.oid AS schema_oid, n.nspname AS schema, c.relname AS name,"
                       " c.oid::regclass::text AS qname, c.relkind AS kind, c.relispartition AS is_partition,"
                       " (SELECT i.inhparent FROM pg_inherits i WHERE i.inhrelid = c.oid AND c.relispartition)"
                       " AS parent_oid,"
                       " (SELECT d.refobjid FROM pg_depend d WHERE c.relkind = 'S'"
                       "   AND d.classid = 'pg_class'::regclass AND d.objid = c.oid"
                       "   AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i') AND d.refobjsubid > 0"
                       "   LIMIT 1) AS owned_by_relid,"
                       " has_table_privilege(c.oid, 'SELECT') AS can_lock"
                       " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                       f" WHERE c.relkind IN ('r', 'p', 'S') AND c.relnamespace {IN_SCHEMAS}"
                       " AND NOT EXISTS (SELECT 1 FROM pg_depend dx WHERE dx.classid = 'pg_class'::regclass"
                       "   AND dx.objid = c.oid AND dx.deptype = 'e')"
                       " ORDER BY n.nspname, c.relname", ctx)
    parents = {r["oid"]: r["parent_oid"] for r in rows}
    for r in rows:
        oid, seen = r["oid"], set()
        while parents.get(oid) in parents and oid not in seen:
            seen.add(oid)
            oid = parents[oid]
        r["root_oid"] = oid
    return rows


def get_schemas(conn, ctx):
    return _rows(conn, "SELECT n.oid, n.nspname AS name, pg_get_userbyid(n.nspowner) AS owner"
                       f" FROM pg_namespace n WHERE n.oid {IN_SCHEMAS} ORDER BY n.nspname", ctx)


def get_extensions(conn, ctx):
    """All extensions except plpgsql, whatever schema they live in."""
    return _rows(conn, "SELECT e.oid, e.extname AS name, n.nspname AS schema FROM pg_extension e"
                       " JOIN pg_namespace n ON n.oid = e.extnamespace"
                       " WHERE e.extname <> 'plpgsql' ORDER BY e.extname", ctx)


def get_types(conn, ctx):
    """Enums, domains, ranges and stand-alone composite types (`kind` = e/d/r/c)."""
    multirange = ("format_type(r.rngmultitypid, NULL)" if ctx.version_num >= 140000 else "NULL")
    base = ("SELECT t.oid, t.typtype AS kind, n.nspname AS schema, t.typname AS name,"
            " format_type(t.oid, NULL) AS qname, pg_get_userbyid(t.typowner) AS owner, t.typrelid")
    where = f" FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typnamespace {IN_SCHEMAS}" \
            f" AND {_not_member('pg_type', 't.oid')}"
    enums = _rows(conn, base + ", ARRAY(SELECT enumlabel FROM pg_enum WHERE enumtypid = t.oid"
                               " ORDER BY enumsortorder)::text[] AS labels" + where + " AND t.typtype = 'e'", ctx)
    domains = _rows(conn, base + ", format_type(t.typbasetype, t.typtypmod) AS base_type,"
                                 f" {_collation_if_differs('t.typcollation', 'bt.typcollation')} AS collation,"
                                 " pg_get_expr(t.typdefaultbin, 0) AS default_expr, t.typnotnull AS notnull"
                    + where.replace("FROM pg_type t", "FROM pg_type t JOIN pg_type bt ON bt.oid = t.typbasetype")
                    + " AND t.typtype = 'd'", ctx)
    ranges = _rows(conn, base + ", format_type(r.rngsubtype, NULL) AS subtype,"
                                " CASE WHEN NOT opc.opcdefault THEN format('%%I.%%I', opn.nspname, opc.opcname) END"
                                " AS subtype_opclass,"
                                f" {_collation_if_differs('r.rngcollation', 'st.typcollation')} AS collation,"
                                " CASE WHEN r.rngcanonical <> 0 THEN r.rngcanonical::regproc::text END AS canonical,"
                                " CASE WHEN r.rngsubdiff <> 0 THEN r.rngsubdiff::regproc::text END AS subtype_diff,"
                                f" {multirange} AS multirange"
                   + where.replace("FROM pg_type t",
                                   "FROM pg_type t JOIN pg_range r ON r.rngtypid = t.oid"
                                   " JOIN pg_type st ON st.oid = r.rngsubtype"
                                   " JOIN pg_opclass opc ON opc.oid = r.rngsubopc"
                                   " JOIN pg_namespace opn ON opn.oid = opc.opcnamespace")
                   + " AND t.typtype = 'r'", ctx)
    composites = _rows(conn, base + ", (SELECT json_agg(json_build_object('name', a.attname,"
                                    " 'type', format_type(a.atttypid, a.atttypmod), 'collation',"
                                    f" {_collation_if_differs('a.attcollation', 'at.typcollation')})"
                                    " ORDER BY a.attnum) FROM pg_attribute a JOIN pg_type at ON at.oid = a.atttypid"
                                    " WHERE a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped)"
                                    " AS attributes"
                       + where + " AND t.typtype = 'c'"
                       " AND (SELECT relkind FROM pg_class WHERE oid = t.typrelid) = 'c'", ctx)
    return enums + domains + ranges + composites


def get_sequences(conn, ctx):
    """All sequences; identity sequences carry `identity_of` (table oid) and `identity_attnum`."""
    return _rows(conn, "SELECT c.oid, n.nspname AS schema, c.relname AS name, c.oid::regclass::text AS qname,"
                       " pg_get_userbyid(c.relowner) AS owner, format_type(s.seqtypid, NULL) AS type,"
                       " s.seqstart AS start, s.seqincrement AS increment, s.seqmin AS min, s.seqmax AS max,"
                       " s.seqcache AS cache, s.seqcycle AS cycle, c.relpersistence AS persistence,"
                       " di.refobjid AS identity_of, di.refobjsubid AS identity_attnum,"
                       " (SELECT format('%%s.%%I', d.refobjid::regclass::text, a.attname) FROM pg_depend d"
                       "   JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid"
                       "   WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid"
                       "   AND d.refclassid = 'pg_class'::regclass AND d.deptype = 'a' AND d.refobjsubid > 0"
                       "   LIMIT 1) AS owned_by"
                       " FROM pg_class c JOIN pg_sequence s ON s.seqrelid = c.oid"
                       " JOIN pg_namespace n ON n.oid = c.relnamespace"
                       " LEFT JOIN pg_depend di ON di.classid = 'pg_class'::regclass AND di.objid = c.oid"
                       "   AND di.refclassid = 'pg_class'::regclass AND di.deptype = 'i'"
                       f" WHERE c.relkind = 'S' AND c.relnamespace {IN_SCHEMAS}"
                       " AND NOT EXISTS (SELECT 1 FROM pg_depend dx WHERE dx.classid = 'pg_class'::regclass"
                       "   AND dx.objid = c.oid AND dx.deptype = 'e')"
                       " ORDER BY n.nspname, c.relname", ctx)


def get_functions(conn, ctx):
    """Functions and procedures; `sqlbody` marks SQL-standard bodies (PG 14+)."""
    sqlbody = "p.prosqlbody IS NOT NULL" if ctx.version_num >= 140000 else "false"
    return _rows(conn, "SELECT p.oid, n.nspname AS schema, p.proname AS name, p.oid::regprocedure::text AS sig,"
                       " p.prokind AS kind, pg_get_userbyid(p.proowner) AS owner,"
                       f" pg_get_functiondef(p.oid) AS def, {sqlbody} AS sqlbody"
                       " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                       f" WHERE p.pronamespace {IN_SCHEMAS} AND p.prokind IN ('f', 'p', 'w')"
                       f" AND {_not_member('pg_proc', 'p.oid')} ORDER BY n.nspname, p.proname, p.oid", ctx)


def get_aggregates(conn, ctx):
    """Aggregates with the pieces needed for CREATE AGGREGATE."""
    return _rows(conn, "SELECT p.oid, n.nspname AS schema, p.proname AS name, p.oid::regprocedure::text AS sig,"
                       " format('%%I.%%I', n.nspname, p.proname) AS qname,"
                       " pg_get_function_arguments(p.oid) AS args, pg_get_userbyid(p.proowner) AS owner,"
                       " a.aggkind AS aggkind, a.aggmtransfn::oid <> 0 AS moving,"
                       " a.aggtransfn::regproc::text AS sfunc, format_type(a.aggtranstype, NULL) AS stype,"
                       " a.aggtransspace AS sspace,"
                       " CASE WHEN a.aggfinalfn::oid <> 0 THEN a.aggfinalfn::regproc::text END AS finalfunc,"
                       " a.aggfinalextra AS finalextra, a.aggfinalmodify AS finalmodify,"
                       " CASE WHEN a.aggcombinefn::oid <> 0 THEN a.aggcombinefn::regproc::text END AS combinefunc,"
                       " CASE WHEN a.aggserialfn::oid <> 0 THEN a.aggserialfn::regproc::text END AS serialfunc,"
                       " CASE WHEN a.aggdeserialfn::oid <> 0 THEN a.aggdeserialfn::regproc::text END"
                       " AS deserialfunc, a.agginitval AS initcond,"
                       " (SELECT format('OPERATOR(%%I.%%s)', opn.nspname, o.oprname) FROM pg_operator o"
                       "   JOIN pg_namespace opn ON opn.oid = o.oprnamespace WHERE o.oid = a.aggsortop) AS sortop,"
                       " p.proparallel AS parallel"
                       " FROM pg_proc p JOIN pg_aggregate a ON a.aggfnoid = p.oid"
                       " JOIN pg_namespace n ON n.oid = p.pronamespace"
                       f" WHERE p.pronamespace {IN_SCHEMAS} AND p.prokind = 'a'"
                       f" AND {_not_member('pg_proc', 'p.oid')} ORDER BY n.nspname, p.proname, p.oid", ctx)


def get_tables(conn, ctx):
    """Plain and partitioned tables with storage, partitioning and security flags."""
    return _rows(conn, "SELECT c.oid, n.nspname AS schema, c.relname AS name, c.oid::regclass::text AS qname,"
                       " c.relkind AS kind, c.relpersistence AS persistence, pg_get_userbyid(c.relowner) AS owner,"
                       " c.reloptions::text[] AS options, tc.reloptions::text[] AS toast_options,"
                       " CASE WHEN am.amname IS NOT NULL AND am.amname <> 'heap' THEN am.amname END AS am,"
                       " CASE WHEN c.relkind = 'p' THEN pg_get_partkeydef(c.oid) END AS partkey,"
                       " c.relispartition AS is_partition,"
                       " CASE WHEN c.relispartition THEN pg_get_expr(c.relpartbound, c.oid) END AS partbound,"
                       " (SELECT i.inhparent FROM pg_inherits i WHERE i.inhrelid = c.oid AND c.relispartition)"
                       " AS part_parent, (SELECT i.inhparent::regclass::text FROM pg_inherits i"
                       "   WHERE i.inhrelid = c.oid AND c.relispartition) AS part_parent_qname,"
                       " c.relreplident AS replident, c.relrowsecurity AS rls,"
                       " c.relforcerowsecurity AS force_rls, c.reltuples::float8 AS reltuples,"
                       " pg_table_size(c.oid) AS size"
                       " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                       " LEFT JOIN pg_class tc ON tc.oid = c.reltoastrelid"
                       " LEFT JOIN pg_am am ON am.oid = c.relam"
                       f" WHERE c.relkind IN ('r', 'p') AND c.relnamespace {IN_SCHEMAS}"
                       f" AND {_not_member('pg_class', 'c.oid')} ORDER BY n.nspname, c.relname", ctx)


def get_columns(conn, ctx):
    """Columns of tables, views and materialized views (views only need names for comments/ACLs).

    `nn_validated` (PG 18+) is false for a `NOT NULL NOT VALID` column, which may still hold NULLs.
    """
    if ctx.version_num >= 180000:
        nn = ("(SELECT k.conname FROM pg_constraint k WHERE k.conrelid = a.attrelid AND k.contype = 'n'"
              " AND k.conkey[1] = a.attnum LIMIT 1)")
        nn_noinherit = ("(SELECT k.connoinherit FROM pg_constraint k WHERE k.conrelid = a.attrelid"
                        " AND k.contype = 'n' AND k.conkey[1] = a.attnum LIMIT 1)")
        nn_validated = ("(SELECT k.convalidated FROM pg_constraint k WHERE k.conrelid = a.attrelid"
                        " AND k.contype = 'n' AND k.conkey[1] = a.attnum LIMIT 1)")
    else:
        nn = nn_noinherit = nn_validated = "NULL"
    return _rows(conn, "SELECT a.attrelid AS relid, a.attnum AS num, a.attname AS name,"
                       " format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull AS notnull,"
                       " a.attislocal AS islocal, a.attidentity AS identity, a.attgenerated AS generated,"
                       f" {_collation_if_differs('a.attcollation', 'ty.typcollation')} AS collation,"
                       " d.oid AS def_oid, pg_get_expr(d.adbin, d.adrelid) AS def_expr,"
                       f" {nn} AS nn_name, {nn_noinherit} AS nn_noinherit, {nn_validated} AS nn_validated"
                       " FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid"
                       " JOIN pg_type ty ON ty.oid = a.atttypid"
                       " LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
                       f" WHERE c.relkind IN ('r', 'p', 'v', 'm') AND c.relnamespace {IN_SCHEMAS}"
                       " AND a.attnum > 0 AND NOT a.attisdropped"
                       f" AND {_not_member('pg_class', 'c.oid')} ORDER BY a.attrelid, a.attnum", ctx)


def get_inherits(conn, ctx):
    return _rows(conn, "SELECT i.inhrelid AS relid, i.inhparent AS parent, i.inhparent::regclass::text AS parent_qname"
                       " FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                       f" WHERE c.relkind IN ('r', 'p') AND c.relnamespace {IN_SCHEMAS}"
                       " ORDER BY i.inhrelid, i.inhseqno", ctx)


def get_constraints(conn, ctx):
    """Table PK/unique/exclusion/check/FK constraints and domain checks (no clones, no inherited)."""
    return _rows(conn, "SELECT k.oid, k.conname AS name, k.contype AS kind, n.nspname AS schema,"
                       " k.conrelid AS relid, k.contypid AS typid, k.confrelid AS frelid,"
                       " CASE WHEN k.conrelid <> 0 THEN k.conrelid::regclass::text END AS table_qname,"
                       " CASE WHEN k.contypid <> 0 THEN format_type(k.contypid, NULL) END AS domain_qname,"
                       " pg_get_constraintdef(k.oid) AS def,"
                       " ic.relname AS index_name, COALESCE(x.indisclustered, false) AS clustered,"
                       " COALESCE(x.indisreplident, false) AS replident"
                       " FROM pg_constraint k JOIN pg_namespace n ON n.oid = k.connamespace"
                       " LEFT JOIN pg_index x ON x.indexrelid = k.conindid AND k.contype IN ('p', 'u', 'x')"
                       " LEFT JOIN pg_class ic ON ic.oid = x.indexrelid"
                       f" WHERE k.connamespace {IN_SCHEMAS} AND k.conislocal AND k.conparentid = 0"
                       " AND ((k.conrelid <> 0 AND k.contype IN ('p', 'u', 'x', 'c', 'f'))"
                       "   OR (k.contypid <> 0 AND k.contype = 'c'))"
                       " ORDER BY n.nspname, k.conname, k.oid", ctx)


def get_indexes(conn, ctx):
    """Indexes not backing a constraint and not partitions of a parent index."""
    return _rows(conn, "SELECT ic.oid, ic.relname AS name, n.nspname AS schema, x.indrelid AS relid,"
                       " t.relkind AS table_kind, t.oid::regclass::text AS table_qname,"
                       " pg_get_indexdef(ic.oid) AS def, x.indisclustered AS clustered,"
                       " x.indisreplident AS replident"
                       " FROM pg_index x JOIN pg_class ic ON ic.oid = x.indexrelid"
                       " JOIN pg_class t ON t.oid = x.indrelid JOIN pg_namespace n ON n.oid = ic.relnamespace"
                       f" WHERE t.relnamespace {IN_SCHEMAS} AND t.relkind IN ('r', 'p', 'm')"
                       f" AND NOT ic.relispartition AND {_not_member('pg_class', 'ic.oid')}"
                       f" AND {_not_member('pg_class', 't.oid')}"
                       " ORDER BY n.nspname, ic.relname", ctx)


def get_statistics(conn, ctx):
    return _rows(conn, "SELECT s.oid, s.stxname AS name, n.nspname AS schema, s.stxrelid AS relid,"
                       " format('%%I.%%I', n.nspname, s.stxname) AS qname, pg_get_userbyid(s.stxowner) AS owner,"
                       " pg_get_statisticsobjdef(s.oid) AS def"
                       " FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid = s.stxnamespace"
                       f" WHERE s.stxnamespace {IN_SCHEMAS} AND {_not_member('pg_statistic_ext', 's.oid')}"
                       " ORDER BY n.nspname, s.stxname", ctx)


def get_views(conn, ctx):
    return _rows(conn, "SELECT c.oid, n.nspname AS schema, c.relname AS name, c.oid::regclass::text AS qname,"
                       " c.relkind AS kind, pg_get_userbyid(c.relowner) AS owner, c.reloptions::text[] AS options,"
                       " pg_get_viewdef(c.oid) AS def, c.relispopulated AS populated,"
                       " CASE WHEN c.relkind = 'm' AND am.amname <> 'heap' THEN am.amname END AS am"
                       " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                       " LEFT JOIN pg_am am ON am.oid = c.relam"
                       f" WHERE c.relkind IN ('v', 'm') AND c.relnamespace {IN_SCHEMAS}"
                       f" AND {_not_member('pg_class', 'c.oid')} ORDER BY n.nspname, c.relname", ctx)


def get_triggers(conn, ctx):
    """User triggers; `is_clone` marks partition clones (PG 13+), kept only for state handling."""
    clone = "g.tgparentid <> 0" if ctx.version_num >= 130000 else "false"
    return _rows(conn, "SELECT g.oid, g.tgname AS name, n.nspname AS schema, g.tgrelid AS relid,"
                       " g.tgrelid::regclass::text AS table_qname, pg_get_triggerdef(g.oid) AS def,"
                       f" g.tgenabled AS enabled, {clone} AS is_clone"
                       " FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid"
                       " JOIN pg_namespace n ON n.oid = c.relnamespace"
                       f" WHERE NOT g.tgisinternal AND c.relnamespace {IN_SCHEMAS}"
                       f" AND {_not_member('pg_class', 'c.oid')} ORDER BY n.nspname, c.relname, g.tgname", ctx)


def get_rules(conn, ctx):
    return _rows(conn, "SELECT r.oid, r.rulename AS name, n.nspname AS schema, r.ev_class AS relid,"
                       " r.ev_class::regclass::text AS table_qname, pg_get_ruledef(r.oid) AS def,"
                       " r.ev_enabled AS enabled"
                       " FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class"
                       " JOIN pg_namespace n ON n.oid = c.relnamespace"
                       f" WHERE r.rulename <> '_RETURN' AND c.relnamespace {IN_SCHEMAS}"
                       f" AND {_not_member('pg_class', 'c.oid')} ORDER BY n.nspname, c.relname, r.rulename", ctx)


def get_policies(conn, ctx):
    return _rows(conn, "SELECT p.oid, p.polname AS name, n.nspname AS schema, p.polrelid AS relid,"
                       " p.polrelid::regclass::text AS table_qname, p.polcmd AS cmd, p.polpermissive AS permissive,"
                       " ARRAY(SELECT CASE WHEN r = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(r)) END"
                       "   FROM unnest(p.polroles) r ORDER BY 1)::text[] AS roles,"
                       " pg_get_expr(p.polqual, p.polrelid) AS qual, pg_get_expr(p.polwithcheck, p.polrelid) AS withcheck"
                       " FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid"
                       " JOIN pg_namespace n ON n.oid = c.relnamespace"
                       f" WHERE c.relnamespace {IN_SCHEMAS} ORDER BY n.nspname, c.relname, p.polname", ctx)


def get_comments(conn, ctx):
    return _rows(conn, "SELECT d.classoid::regclass::text AS cat, d.objoid AS oid, d.objsubid AS subid,"
                       f" d.description FROM pg_description d WHERE d.objoid >= {USER_OID}", ctx)


_ACL_SOURCES = [
    ("pg_namespace", "n.oid", "0", "n.nspacl", "pg_namespace n", f"n.oid {IN_SCHEMAS}"),
    ("pg_class", "c.oid", "0", "c.relacl", "pg_class c",
     f"c.relnamespace {IN_SCHEMAS} AND c.relkind IN ('r', 'p', 'v', 'm', 'S')"),
    ("pg_class", "a.attrelid", "a.attnum", "a.attacl", "pg_attribute a JOIN pg_class c ON c.oid = a.attrelid",
     f"c.relnamespace {IN_SCHEMAS} AND c.relkind IN ('r', 'p', 'v', 'm') AND a.attnum > 0 AND NOT a.attisdropped"),
    ("pg_proc", "p.oid", "0", "p.proacl", "pg_proc p", f"p.pronamespace {IN_SCHEMAS}"),
    ("pg_type", "t.oid", "0", "t.typacl", "pg_type t", f"t.typnamespace {IN_SCHEMAS} AND t.typtype IN ('e', 'd', 'r', 'c')"),
]


def get_acls(conn, ctx):
    """Non-NULL ACLs exploded into grantee/privilege entries (grantee None = PUBLIC)."""
    parts = []
    for cat, oid, subid, acl, source, where in _ACL_SOURCES:
        parts.append(
            f"SELECT '{cat}' AS cat, {oid} AS oid, {subid}::int AS subid,"
            " (SELECT COALESCE(json_agg(json_build_object('grantee', CASE WHEN e.grantee = 0 THEN NULL"
            " ELSE pg_get_userbyid(e.grantee) END, 'priv', e.privilege_type, 'grantable', e.is_grantable)),"
            f" '[]'::json) FROM aclexplode({acl}) e) AS entries FROM {source} WHERE {acl} IS NOT NULL AND {where}")
    return _rows(conn, " UNION ALL ".join(parts), ctx)


def get_dependencies(conn, ctx):
    """Normal/auto dependencies between user objects, with the referenced object's schema."""
    return _rows(conn, "SELECT d.classid::regclass::text AS cat, d.objid, d.objsubid,"
                       " d.refclassid::regclass::text AS refcat, d.refobjid, d.refobjsubid, d.deptype,"
                       " ri.schema AS ref_schema, ri.identity AS ref_identity,"
                       " EXISTS (SELECT 1 FROM pg_depend e WHERE e.classid = d.refclassid"
                       "   AND e.objid = d.refobjid AND e.deptype = 'e') AS ref_in_extension"
                       " FROM pg_depend d CROSS JOIN LATERAL pg_identify_object(d.refclassid, d.refobjid, 0) ri"
                       f" WHERE d.deptype IN ('n', 'a') AND d.objid >= {USER_OID} AND d.refobjid >= {USER_OID}", ctx)


def get_aliases(conn, ctx):
    """Catalog entries that stand for another object when resolving dependencies.

    Row types and composite relations, array and multirange types, view `_RETURN` rules and
    indexes that back a constraint: `(cat, oid)` resolves to `(target_cat, target_oid)`.
    """
    multirange = (f" UNION ALL SELECT 'pg_type', r.rngmultitypid, 'pg_type', r.rngtypid FROM pg_range r"
                  f" WHERE r.rngmultitypid >= {USER_OID}") if ctx.version_num >= 140000 else ""
    return _rows(conn, "SELECT 'pg_type' AS cat, t.oid, CASE WHEN c.relkind = 'c' THEN 'pg_type' ELSE 'pg_class' END"
                       " AS target_cat, CASE WHEN c.relkind = 'c' THEN t.oid ELSE t.typrelid END AS target_oid"
                       f" FROM pg_type t JOIN pg_class c ON c.oid = t.typrelid WHERE t.oid >= {USER_OID}"
                       " UNION ALL SELECT 'pg_class', c.oid, 'pg_type', c.reltype FROM pg_class c"
                       f" WHERE c.relkind = 'c' AND c.oid >= {USER_OID}"
                       " UNION ALL SELECT 'pg_type', t.oid, 'pg_type', t.typelem FROM pg_type t"
                       f" WHERE t.oid >= {USER_OID} AND t.typelem <> 0"
                       "   AND EXISTS (SELECT 1 FROM pg_type e WHERE e.oid = t.typelem AND e.typarray = t.oid)"
                       " UNION ALL SELECT 'pg_rewrite', r.oid, 'pg_class', r.ev_class FROM pg_rewrite r"
                       f" WHERE r.rulename = '_RETURN' AND r.oid >= {USER_OID}"
                       " UNION ALL SELECT 'pg_class', k.conindid, 'pg_constraint', k.oid FROM pg_constraint k"
                       f" WHERE k.contype IN ('p', 'u', 'x') AND k.conindid <> 0 AND k.oid >= {USER_OID}"
                       + multirange, ctx)


def get_sequence_values(conn: psycopg.Connection, seqs: list[dict], skip: set | None = None) -> dict:
    """{sequence oid: (last_value, is_called)} read with plain SELECTs (no writes); oids in `skip` are not read."""
    values = {}
    for s in seqs:
        if skip and s["oid"] in skip:
            continue
        q = sql.SQL("SELECT last_value, is_called FROM {}").format(sql.Identifier(s["schema"], s["name"]))
        values[s["oid"]] = tuple(conn.execute(q).fetchone())
    return values


_UNSUPPORTED = [
    ("collations", "pg_collation", "x.oid", "x.collnamespace"),
    ("operators", "pg_operator", "x.oid", "x.oprnamespace"),
    ("operator classes", "pg_opclass", "x.oid", "x.opcnamespace"),
    ("operator families", "pg_opfamily", "x.oid", "x.opfnamespace"),
    ("text search configurations", "pg_ts_config", "x.oid", "x.cfgnamespace"),
    ("text search dictionaries", "pg_ts_dict", "x.oid", "x.dictnamespace"),
    ("text search parsers", "pg_ts_parser", "x.oid", "x.prsnamespace"),
    ("text search templates", "pg_ts_template", "x.oid", "x.tmplnamespace"),
    ("conversions", "pg_conversion", "x.oid", "x.connamespace"),
]


def scan_unsupported(conn, ctx) -> list[str]:
    """Object kinds in the selected schemas that the backup does not reproduce."""
    checks = [(label, f"SELECT count(*) FROM {cat} x WHERE {ns} {IN_SCHEMAS} AND {_not_member(cat, oid)}")
              for label, cat, oid, ns in _UNSUPPORTED]
    checks += [
        ("base or shell types", "SELECT count(*) FROM pg_type x WHERE x.typtype IN ('b', 'p')"
                                f" AND x.typnamespace {IN_SCHEMAS} AND {_not_member('pg_type', 'x.oid')}"),
        ("foreign tables", f"SELECT count(*) FROM pg_class x WHERE x.relkind = 'f' AND x.relnamespace {IN_SCHEMAS}"
                           f" AND {_not_member('pg_class', 'x.oid')}"),
        ("casts", f"SELECT count(*) FROM pg_cast x WHERE x.oid >= {USER_OID} AND {_not_member('pg_cast', 'x.oid')}"),
        ("default privileges", "SELECT count(*) FROM pg_default_acl x"
                               f" WHERE x.defaclnamespace = 0 OR x.defaclnamespace {IN_SCHEMAS}"),
        ("publications", "SELECT count(*) FROM pg_publication"),
        ("event triggers", f"SELECT count(*) FROM pg_event_trigger x WHERE {_not_member('pg_event_trigger', 'x.oid')}"),
        ("foreign servers", f"SELECT count(*) FROM pg_foreign_server x"
                            f" WHERE {_not_member('pg_foreign_server', 'x.oid')}"),
        ("large objects", "SELECT count(*) FROM pg_largeobject_metadata"),
    ]
    found = []
    for label, query in checks:
        n = conn.execute(query, {"schemas": ctx.schema_oids}).fetchone()[0]
        if n:
            found.append(f"NOT INCLUDED: {n} {label}")
    return found


def load_all(conn: psycopg.Connection, schemas: list[str] | None, version_num: int,
             on_step: Callable[[str], None] | None = None, skip_sequence_values: set | None = None) -> Catalog:
    """Read every catalog object for the selected schemas (None = all user schemas).

    `on_step(name)` runs before each reader (the backup uses it to check for cancel and to name
    the step in a lock-timeout message). Sequences in `skip_sequence_values` (oids the login
    may not read) get no value.
    """
    selected = resolve_schemas(conn, schemas)
    ctx = CatalogContext(version_num, [s["oid"] for s in selected])
    cat = Catalog(version_num=version_num)
    readers = {
        "schemas": get_schemas, "extensions": get_extensions, "types": get_types,
        "sequences": get_sequences, "functions": get_functions, "aggregates": get_aggregates,
        "tables": get_tables, "columns": get_columns, "inherits": get_inherits,
        "constraints": get_constraints, "indexes": get_indexes, "statistics": get_statistics,
        "views": get_views, "triggers": get_triggers, "rules": get_rules, "policies": get_policies,
        "comments": get_comments, "acls": get_acls, "dependencies": get_dependencies,
        "aliases": get_aliases,
    }
    for name, reader in readers.items():
        if on_step:
            on_step(name)
        setattr(cat, name, reader(conn, ctx))
    if on_step:
        on_step("sequence values")
    cat.sequence_values = get_sequence_values(conn, cat.sequences, skip_sequence_values)
    if on_step:
        on_step("unsupported objects")
    cat.unsupported = scan_unsupported(conn, ctx)
    return cat
