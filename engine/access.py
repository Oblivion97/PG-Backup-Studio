"""Access rights of one user or group inside one database (decision 015).

`read_access` reads what a role may do: database (connect, create schemas, temporary tables), each schema (create
objects, run functions, default rights on tables created later) and each table / view (SELECT, INSERT, UPDATE,
DELETE, TRUNCATE, REFERENCES, TRIGGER), as granted directly and as it works in effect (groups, PUBLIC, ownership).

`build_access` turns the wanted state the UI sends into GRANT / REVOKE / ALTER DEFAULT PRIVILEGES statements, diffed
against what is there now, and adds what makes them work: USAGE on the schema, CONNECT on the database and USAGE on
the sequences behind serial columns when rows may be inserted. Everything is built with `psycopg.sql` and runs in one
transaction; PostgreSQL decides whether the saved login may grant it.
"""

from __future__ import annotations

import logging

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from . import admin
from .db import DbActionError, check_name, connect, require_supported, _missing_database, _primary
from .util import EngineError

log = logging.getLogger(__name__)

TABLE_PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
DB_PRIVS = ("CONNECT", "CREATE", "TEMPORARY")
MAX_TABLES = 5000
MAX_CHANGES = 20000

_SCHEMA_FILTER = """n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
  AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend e WHERE e.classid = 'pg_catalog.pg_namespace'::regclass
                  AND e.objid = n.oid AND e.deptype = 'e')"""

_ROLE_SQL = """
SELECT r.oid, r.rolname AS name, r.rolcanlogin AS login, r.rolsuper AS superuser, r.rolinherit AS inherit,
       COALESCE((SELECT json_agg(b.rolname ORDER BY b.rolname) FROM pg_catalog.pg_auth_members m
                 JOIN pg_catalog.pg_roles b ON b.oid = m.roleid WHERE m.member = r.oid), '[]') AS member_of
FROM pg_catalog.pg_roles r WHERE r.rolname = %s
"""

_DB_SQL = """
SELECT d.datname AS name, pg_catalog.pg_get_userbyid(d.datdba) AS owner,
       pg_catalog.pg_has_role(%(rid)s, d.datdba, 'USAGE') AS is_owner,
       COALESCE((SELECT array_agg(a.privilege_type::text) FROM pg_catalog.aclexplode(COALESCE(d.datacl,
                 pg_catalog.acldefault('d', d.datdba))) a WHERE a.grantee = %(rid)s), '{}') AS direct,
       COALESCE((SELECT array_agg(a.privilege_type::text) FROM pg_catalog.aclexplode(COALESCE(d.datacl,
                 pg_catalog.acldefault('d', d.datdba))) a WHERE a.grantee = 0), '{}') AS public,
       ARRAY(SELECT p FROM unnest(ARRAY['CONNECT', 'CREATE', 'TEMPORARY']) p
             WHERE pg_catalog.has_database_privilege(%(rid)s, d.oid, p)) AS effective
FROM pg_catalog.pg_database d WHERE d.datname = pg_catalog.current_database()
"""

_SCHEMAS_SQL = f"""
SELECT n.nspname AS name, pg_catalog.pg_get_userbyid(n.nspowner) AS owner,
       pg_catalog.pg_has_role(%(rid)s, n.nspowner, 'USAGE') AS is_owner,
       COALESCE((SELECT array_agg(a.privilege_type::text) FROM pg_catalog.aclexplode(COALESCE(n.nspacl,
                 pg_catalog.acldefault('n', n.nspowner))) a WHERE a.grantee = %(rid)s), '{{}}') AS direct,
       COALESCE((SELECT array_agg(a.privilege_type::text) FROM pg_catalog.aclexplode(COALESCE(n.nspacl,
                 pg_catalog.acldefault('n', n.nspowner))) a WHERE a.grantee = 0), '{{}}') AS public,
       pg_catalog.has_schema_privilege(%(rid)s, n.oid, 'USAGE') AS eff_usage,
       pg_catalog.has_schema_privilege(%(rid)s, n.oid, 'CREATE') AS eff_create,
       (SELECT count(*) FROM pg_catalog.pg_proc p WHERE p.pronamespace = n.oid AND p.prokind IN ('f', 'p'))::int AS routines,
       (SELECT count(*) FROM pg_catalog.pg_proc p WHERE p.pronamespace = n.oid AND p.prokind IN ('f', 'p')
          AND EXISTS (SELECT 1 FROM pg_catalog.aclexplode(COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner))) a
                      WHERE a.grantee = %(rid)s AND a.privilege_type = 'EXECUTE'))::int AS routines_direct,
       (SELECT count(*) FROM pg_catalog.pg_proc p WHERE p.pronamespace = n.oid AND p.prokind IN ('f', 'p')
          AND pg_catalog.has_function_privilege(%(rid)s, p.oid, 'EXECUTE'))::int AS routines_effective,
       (SELECT count(*) FROM pg_catalog.pg_class c WHERE c.relnamespace = n.oid AND c.relkind = 'S')::int AS sequences,
       COALESCE((SELECT array_agg(DISTINCT a.privilege_type::text) FROM pg_catalog.pg_default_acl da,
                 pg_catalog.aclexplode(da.defaclacl) a WHERE da.defaclnamespace = n.oid AND da.defaclrole = n.nspowner
                 AND da.defaclobjtype = 'r' AND a.grantee = %(rid)s), '{{}}') AS future
FROM pg_catalog.pg_namespace n
WHERE {_SCHEMA_FILTER}
ORDER BY n.nspname
"""

_TABLES_SQL = f"""
SELECT n.nspname AS schema, c.relname AS name, c.relkind::text AS kind,
       pg_catalog.pg_has_role(%(rid)s, c.relowner, 'USAGE') AS is_owner,
       COALESCE((SELECT array_agg(a.privilege_type::text) FROM pg_catalog.aclexplode(COALESCE(c.relacl,
                 pg_catalog.acldefault('r', c.relowner))) a WHERE a.grantee = %(rid)s), '{{}}') AS direct,
       ARRAY(SELECT p FROM unnest(ARRAY['SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER']) p
             WHERE pg_catalog.has_table_privilege(%(rid)s, c.oid, p)) AS effective,
       COALESCE((SELECT json_agg(json_build_array(sn.nspname, s.relname)) FROM pg_catalog.pg_depend d
                 JOIN pg_catalog.pg_class s ON s.oid = d.objid AND s.relkind = 'S'
                 JOIN pg_catalog.pg_namespace sn ON sn.oid = s.relnamespace
                 WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.refclassid = 'pg_catalog.pg_class'::regclass
                   AND d.refobjid = c.oid AND d.deptype = 'a'), '[]') AS sequences
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition AND {_SCHEMA_FILTER}
ORDER BY n.nspname, c.relname
LIMIT {MAX_TABLES + 1}
"""


def _order(privs, allowed=TABLE_PRIVS) -> list[str]:
    s = set(privs)
    return [p for p in allowed if p in s]


def _open(server: dict, dbname: str) -> psycopg.Connection:
    dbname = check_name(dbname)
    try:
        conn = connect(server, dbname, autocommit=True)
    except EngineError as e:
        if _missing_database(e):
            raise DbActionError(f"Database {dbname} does not exist.", "not_found") from e
        raise
    try:
        require_supported(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _read(conn: psycopg.Connection, role: str) -> dict:
    role = check_name(role, "role name")
    with conn.cursor(row_factory=dict_row) as cur:
        r = cur.execute(_ROLE_SQL, (role,)).fetchone()
        if r is None:
            raise DbActionError(f"Role {role} does not exist.", "not_found")
        rid = r.pop("oid")
        p = {"rid": rid}
        database = cur.execute(_DB_SQL, p).fetchone()
        schemas = cur.execute(_SCHEMAS_SQL, p).fetchall()
        tables = cur.execute(_TABLES_SQL, p).fetchall()
    database["direct"] = _order(database["direct"], DB_PRIVS)
    database["public"] = _order(database["public"], DB_PRIVS)
    database["effective"] = _order(database["effective"], DB_PRIVS)
    for s in schemas:
        s["direct"] = _order(s["direct"], ("USAGE", "CREATE"))
        s["public"] = _order(s["public"], ("USAGE", "CREATE"))
        s["future"] = _order(s["future"])
    for t in tables:
        t["direct"] = _order(t["direct"])
        t["effective"] = _order(t["effective"])
    truncated = len(tables) > MAX_TABLES
    return {"role": r, "database": database, "schemas": schemas, "tables": tables[:MAX_TABLES], "truncated": truncated}


def read_access(server: dict, dbname: str, role: str) -> dict:
    """What `role` may do in database `dbname`: granted directly and in effect."""
    conn = _open(server, dbname)
    try:
        caps = admin._caps(conn)
        out = _read(conn, role)
    except EngineError:
        raise
    except psycopg.Error as e:
        raise EngineError(f"Could not read the access rights: {_primary(e)}") from e
    finally:
        conn.close()
    out["capabilities"] = caps
    return out


# ---------------------------------------------------------------- statements

def _privs(value, allowed=TABLE_PRIVS, what="privileges") -> list[str]:
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise DbActionError(f"'{what}' must be a list of privilege names.")
    bad = [p for p in value if p.upper() not in allowed]
    if bad:
        raise DbActionError(f"Unknown privilege {bad[0]!r}; use {', '.join(allowed)}.")
    return _order({p.upper() for p in value}, allowed)


def _plist(privs) -> sql.Composable:
    return sql.SQL(", ").join(sql.SQL(p) for p in privs)   # names checked against a fixed list


def _qn(schema: str, name: str) -> sql.Composable:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(name))


def build_access(state: dict, changes) -> list[sql.Composable]:
    """GRANT / REVOKE statements that take `state` (from `_read`) to the wanted `changes`.

    Change kinds:
      {"kind": "database", "privs": [CONNECT, CREATE, TEMPORARY]}      direct rights on the database
      {"kind": "all_tables", "schema": s, "privs": [...]}               every table / view in the schema
      {"kind": "table", "schema": s, "name": t, "privs": [...]}         one table / view (after all_tables)
      {"kind": "functions", "schema": s, "execute": bool}               every function / procedure in the schema
      {"kind": "schema_create", "schema": s, "create": bool}            may create objects in the schema
      {"kind": "future_tables", "schema": s, "privs": [...]}            rights on tables the schema owner creates later
    """
    if not isinstance(changes, list):
        raise DbActionError("'changes' must be a list.")
    if len(changes) > MAX_CHANGES:
        raise DbActionError(f"Too many changes at once (at most {MAX_CHANGES}).")
    role = sql.Identifier(state["role"]["name"])
    db = state["database"]
    schemas = {s["name"]: s for s in state["schemas"]}
    tables = {(t["schema"], t["name"]): dict(t, direct=list(t["direct"])) for t in state["tables"]}
    by_schema: dict[str, list[dict]] = {}
    for t in tables.values():
        by_schema.setdefault(t["schema"], []).append(t)

    def schema_of(c) -> dict:
        name = c.get("schema")
        if not isinstance(name, str) or name not in schemas:
            raise DbActionError(f"Schema {name!r} does not exist in {db['name']} (any more).", "not_found")
        return schemas[name]

    kinds = {"database": [], "all_tables": [], "table": [], "functions": [], "schema_create": [], "future_tables": []}
    for c in changes:
        if not isinstance(c, dict) or c.get("kind") not in kinds:
            raise DbActionError(f"Unknown change: {c!r}.")
        kinds[c["kind"]].append(c)

    out_db: list[sql.Composable] = []
    out_schema: list[sql.Composable] = []
    out_tables: list[sql.Composable] = []
    out_seq: list[sql.Composable] = []
    out_func: list[sql.Composable] = []
    out_future: list[sql.Composable] = []
    touched: set[str] = set()
    granted_any = False
    exec_after: dict[str, bool] = {}
    create_after: dict[str, bool] = {}

    for c in kinds["database"]:
        want = set(_privs(c.get("privs"), DB_PRIVS))
        have = set(db["direct"])
        dbid = sql.Identifier(db["name"])
        if have - want:
            out_db.append(sql.SQL("REVOKE {} ON DATABASE {} FROM {}").format(_plist(_order(have - want, DB_PRIVS)), dbid, role))
        if want - have:
            out_db.append(sql.SQL("GRANT {} ON DATABASE {} TO {}").format(_plist(_order(want - have, DB_PRIVS)), dbid, role))
            granted_any = True

    for c in kinds["all_tables"]:
        s = schema_of(c)
        want = _privs(c.get("privs"))
        rows = by_schema.get(s["name"], [])
        if not rows:
            continue
        touched.add(s["name"])
        if any(t["is_owner"] for t in rows):
            # tables the role owns are left alone: an owner must not revoke its own rights by accident
            for t in rows:
                if not t["is_owner"]:
                    _table_diff(t, want, role, out_tables, out_seq)
                    t["direct"] = list(want)
            granted_any = granted_any or bool(want)
            continue
        have_any = set().union(*(set(t["direct"]) for t in rows))
        have_all = set.intersection(*(set(t["direct"]) for t in rows))
        revoke, grant = _order(have_any - set(want)), _order(set(want) - have_all)
        sid = sql.Identifier(s["name"])
        if revoke:
            out_tables.append(sql.SQL("REVOKE {} ON ALL TABLES IN SCHEMA {} FROM {}").format(_plist(revoke), sid, role))
        if grant:
            out_tables.append(sql.SQL("GRANT {} ON ALL TABLES IN SCHEMA {} TO {}").format(_plist(grant), sid, role))
            granted_any = True
        if s["sequences"]:
            if "INSERT" in grant:
                out_seq.append(sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(sid, role))
            elif not want and have_any:
                out_seq.append(sql.SQL("REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(sid, role))
        for t in rows:
            t["direct"] = list(want)

    for c in kinds["table"]:
        s = schema_of(c)
        name = c.get("name")
        t = tables.get((s["name"], name)) if isinstance(name, str) else None
        if t is None:
            raise DbActionError(f"Table {s['name']}.{name} does not exist (any more).", "not_found")
        if t["is_owner"]:
            continue   # the owner has every right on it already
        touched.add(s["name"])
        want = _privs(c.get("privs"))
        if _table_diff(t, want, role, out_tables, out_seq):
            granted_any = granted_any or bool(set(want) - set(t["direct"]))
        t["direct"] = list(want)

    for c in kinds["functions"]:
        s = schema_of(c)
        on = c.get("execute")
        if not isinstance(on, bool):
            raise DbActionError("'execute' must be true or false.")
        if not s["routines"]:
            continue
        touched.add(s["name"])
        exec_after[s["name"]] = on
        sid = sql.Identifier(s["name"])
        if on and s["routines_direct"] < s["routines"]:
            out_func.append(sql.SQL("GRANT EXECUTE ON ALL ROUTINES IN SCHEMA {} TO {}").format(sid, role))
            granted_any = True
        elif not on and s["routines_direct"]:
            out_func.append(sql.SQL("REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA {} FROM {}").format(sid, role))

    for c in kinds["schema_create"]:
        s = schema_of(c)
        on = c.get("create")
        if not isinstance(on, bool):
            raise DbActionError("'create' must be true or false.")
        if s["is_owner"]:
            continue
        touched.add(s["name"])
        create_after[s["name"]] = on
        has = "CREATE" in s["direct"]
        sid = sql.Identifier(s["name"])
        if on and not has:
            out_schema.append(sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(sid, role))
            granted_any = True
        elif not on and has:
            out_schema.append(sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(sid, role))

    future_after: dict[str, list[str]] = {}
    for c in kinds["future_tables"]:
        s = schema_of(c)
        want = _privs(c.get("privs"))
        have = set(s["future"])
        future_after[s["name"]] = want
        touched.add(s["name"])
        head = sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} ").format(sql.Identifier(s["owner"]), sql.Identifier(s["name"]))
        if have - set(want):
            out_future.append(head + sql.SQL("REVOKE {} ON TABLES FROM {}").format(_plist(_order(have - set(want))), role))
        if set(want) - have:
            out_future.append(head + sql.SQL("GRANT {} ON TABLES TO {}").format(_plist(_order(set(want) - have)), role))
            granted_any = True

    # the schema itself: USAGE while anything inside is reachable, taken back when nothing is left
    usage: list[sql.Composable] = []
    for name in sorted(touched):
        s = schemas[name]
        if s["is_owner"]:
            continue
        rows = by_schema.get(name, [])
        anything = (any(t["direct"] for t in rows if not t["is_owner"])
                    or exec_after.get(name, s["routines_direct"] > 0)
                    or create_after.get(name, "CREATE" in s["direct"])
                    or bool(future_after.get(name, s["future"])))
        sid = sql.Identifier(name)
        if anything and not s["eff_usage"]:
            usage.append(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sid, role))
        elif not anything and "USAGE" in s["direct"]:
            usage.append(sql.SQL("REVOKE USAGE ON SCHEMA {} FROM {}").format(sid, role))

    connect_stmt: list[sql.Composable] = []
    db_wanted = set(db["direct"]) if not kinds["database"] else set(_privs(kinds["database"][-1].get("privs"), DB_PRIVS))
    if granted_any and "CONNECT" not in db["effective"] and "CONNECT" not in db_wanted:
        connect_stmt.append(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db["name"]), role))

    return out_db + connect_stmt + usage + out_schema + out_tables + out_seq + out_func + out_future


def _table_diff(t: dict, want: list[str], role, out: list, out_seq: list) -> bool:
    have = set(t["direct"])
    revoke, grant = _order(have - set(want)), _order(set(want) - have)
    name = _qn(t["schema"], t["name"])
    if revoke:
        out.append(sql.SQL("REVOKE {} ON TABLE {} FROM {}").format(_plist(revoke), name, role))
    if grant:
        out.append(sql.SQL("GRANT {} ON TABLE {} TO {}").format(_plist(grant), name, role))
    if "INSERT" in grant:
        for sch, seq in t.get("sequences") or []:
            out_seq.append(sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {} TO {}").format(_qn(sch, seq), role))
    return bool(revoke or grant)


def preview_access(server: dict, dbname: str, role: str, changes) -> list[str]:
    conn = _open(server, dbname)
    try:
        stmts = build_access(_read(conn, role), changes)
        return [s.as_string(conn) + ";" for s in stmts]
    except EngineError:
        raise
    except psycopg.Error as e:
        raise EngineError(f"Could not prepare the SQL: {_primary(e)}") from e
    finally:
        conn.close()


def apply_access(server: dict, dbname: str, role: str, changes) -> dict:
    conn = _open(server, dbname)
    try:
        stmts = build_access(_read(conn, role), changes)
        if stmts:
            admin._run(conn, stmts, f"change the access of {role} in {dbname}")
    except EngineError:
        raise
    except psycopg.Error as e:
        raise EngineError(f"Could not change the access: {_primary(e)}") from e
    finally:
        conn.close()
    log.info("changed access of %s in %s on %s:%s (%d statements)", role, dbname, server.get("host"), server.get("port"), len(stmts))
    return {"role": role, "database": dbname, "statements": len(stmts)}


__all__ = ["read_access", "preview_access", "apply_access", "build_access", "TABLE_PRIVS", "DB_PRIVS"]
