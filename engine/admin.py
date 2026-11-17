"""Server administration (decision 011): sessions (cancel / terminate) and roles & users (create / alter / drop,
passwords, memberships).

Everything runs on the maintenance connection of the saved login and is limited by what that login may do; the
`capabilities` call tells the UI what to offer. Role changes run in one transaction and are built with psycopg's
`sql` module only. Passwords are turned into a SCRAM-SHA-256 verifier here, so the plain password never reaches
the server (or its statement log); they are never logged or stored by the app.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
import stringprep
import unicodedata

import psycopg
from psycopg import errors, sql
from psycopg.rows import dict_row

from .db import APPLICATION_NAME, DbActionError, _maintenance, _primary, check_name
from .util import EngineError

log = logging.getLogger(__name__)

SCRAM_ITERATIONS = 4096   # PostgreSQL's default (scram_iterations, PG 16+)
ROLE_FLAGS = ("superuser", "createdb", "createrole", "replication", "bypassrls", "inherit", "login")
_FLAG_SQL = {"superuser": "SUPERUSER", "createdb": "CREATEDB", "createrole": "CREATEROLE", "replication": "REPLICATION",
             "bypassrls": "BYPASSRLS", "inherit": "INHERIT", "login": "LOGIN"}
MAX_PIDS = 500


# ---------------------------------------------------------------- capabilities

def _caps(conn: psycopg.Connection) -> dict:
    row = conn.execute(
        "SELECT current_user, r.rolsuper, r.rolcreaterole,"
        " pg_catalog.pg_has_role(current_user, 'pg_signal_backend', 'USAGE'),"
        " pg_catalog.pg_has_role(current_user, 'pg_read_all_stats', 'USAGE'),"
        " current_setting('server_version_num')::int"
        " FROM pg_catalog.pg_roles r WHERE r.rolname = current_user").fetchone()
    user, sup, createrole, signal, stats, version = row
    return {
        "user": user, "superuser": sup, "createrole": createrole, "signal_backend": sup or signal,
        "read_all_stats": sup or stats, "version_num": version,
        "can_manage_roles": sup or createrole,
        "can_end_others": sup or signal,
    }


def capabilities(server: dict) -> dict:
    """What the saved login may do here: see / end sessions, manage roles."""
    with _maintenance(server, "check the login's rights") as conn:
        return _caps(conn)


# ---------------------------------------------------------------- sessions

_SESSIONS_SQL = """
SELECT a.pid, a.usename AS "user", a.datname AS database, a.application_name AS app,
       host(a.client_addr) AS client_addr, a.client_hostname AS client_host, a.backend_type, a.state,
       a.backend_start, a.xact_start, a.query_start, a.state_change, a.wait_event_type, a.wait_event, a.query,
       EXTRACT(EPOCH FROM now() - a.state_change)::float8 AS state_secs,
       EXTRACT(EPOCH FROM now() - a.query_start)::float8 AS query_secs,
       EXTRACT(EPOCH FROM now() - a.xact_start)::float8 AS xact_secs,
       COALESCE(r.rolsuper, false) AS superuser_session,
       (a.backend_type = 'client backend' AND a.usesysid IS NOT NULL AND (
            %(sup)s
            OR (NOT COALESCE(r.rolsuper, false) AND (%(signal)s OR pg_catalog.pg_has_role(a.usesysid, 'MEMBER')))
       )) AS can_signal
FROM pg_catalog.pg_stat_activity a
LEFT JOIN pg_catalog.pg_roles r ON r.oid = a.usesysid
WHERE a.pid <> pg_catalog.pg_backend_pid()
ORDER BY (a.backend_type = 'client backend') DESC, a.datname NULLS LAST, a.usename, a.pid
"""


def list_sessions(server: dict) -> dict:
    """Every session the login can see except the one asking; `can_signal` tells which it may cancel / end."""
    with _maintenance(server, "list the sessions") as conn, conn.cursor(row_factory=dict_row) as cur:
        caps = _caps(conn)
        rows = cur.execute(_SESSIONS_SQL, {"sup": caps["superuser"], "signal": caps["signal_backend"]}).fetchall()
    for r in rows:
        r["client"] = r["backend_type"] == "client backend"
        r["this_app"] = bool(r["app"]) and r["app"].startswith(APPLICATION_NAME)
        for k in ("backend_start", "xact_start", "query_start", "state_change"):
            if r[k] is not None:
                r[k] = r[k].isoformat()
    return {"capabilities": caps, "sessions": rows, "now": dt.datetime.now(dt.timezone.utc).isoformat()}


def signal_sessions(server: dict, pids, action: str) -> dict:
    """`action` cancel (stop the running query) or terminate (disconnect) for each pid; per-pid outcome."""
    if action not in ("cancel", "terminate"):
        raise DbActionError("Unknown action; use cancel or terminate.")
    if not isinstance(pids, list) or not pids or len(pids) > MAX_PIDS \
            or not all(isinstance(p, int) and not isinstance(p, bool) and p > 0 for p in pids):
        raise DbActionError(f"Choose between 1 and {MAX_PIDS} sessions.")
    fn = "pg_cancel_backend" if action == "cancel" else "pg_terminate_backend"
    done, failed = [], []
    with _maintenance(server, f"{action} the sessions") as conn:
        me = conn.execute("SELECT pg_catalog.pg_backend_pid()").fetchone()[0]
        info = {r[0]: r[1] for r in conn.execute(
            "SELECT pid, backend_type FROM pg_catalog.pg_stat_activity WHERE pid = ANY(%s)", (list(set(pids)),))}
        for pid in dict.fromkeys(pids):
            if pid == me:
                failed.append({"pid": pid, "reason": "This is the app's own connection."})
            elif pid not in info:
                failed.append({"pid": pid, "reason": "The session has already ended."})
            elif info[pid] != "client backend":
                failed.append({"pid": pid, "reason": f"A {info[pid]} process is not ended from here."})
            else:
                try:
                    ok = conn.execute(sql.SQL("SELECT pg_catalog.{}(%s)").format(sql.Identifier(fn)), (pid,)).fetchone()[0]
                except errors.InsufficientPrivilege as e:
                    failed.append({"pid": pid, "reason": _primary(e)})
                    continue
                (done if ok else failed).append(pid if ok else {"pid": pid, "reason": "The session had already ended."})
    log.info("%s sessions on %s:%s: %d done, %d not", action, server.get("host"), server.get("port"), len(done), len(failed))
    return {"action": action, "done": done, "failed": failed}


# ---------------------------------------------------------------- roles

_ROLES_SQL = """
SELECT r.oid, r.rolname AS name, r.rolsuper AS superuser, r.rolinherit AS inherit, r.rolcreaterole AS createrole,
       r.rolcreatedb AS createdb, r.rolcanlogin AS login, r.rolreplication AS replication, r.rolbypassrls AS bypassrls,
       r.rolconnlimit AS connlimit, r.rolvaliduntil AS valid_until, r.rolname ~ '^pg_' AS builtin,
       pg_catalog.shobj_description(r.oid, 'pg_authid') AS comment,
       COALESCE((SELECT json_agg(json_build_object('name', b.rolname, 'admin', m.admin_option) ORDER BY b.rolname)
                 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles b ON b.oid = m.roleid
                 WHERE m.member = r.oid), '[]') AS member_of,
       COALESCE((SELECT json_agg(u.rolname ORDER BY u.rolname)
                 FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles u ON u.oid = m.member
                 WHERE m.roleid = r.oid), '[]') AS members,
       (SELECT count(*) FROM pg_catalog.pg_stat_activity a WHERE a.usesysid = r.oid)::int AS sessions,
       r.rolname = current_user AS is_me,
       (%(sup)s OR (%(createrole)s AND NOT r.rolsuper AND r.rolname !~ '^pg_'
                     AND (%(v16)s IS FALSE OR pg_catalog.pg_has_role(r.oid, 'MEMBER WITH ADMIN OPTION')))) AS can_manage
FROM pg_catalog.pg_roles r
ORDER BY r.rolname ~ '^pg_', r.rolname
"""


def list_roles(server: dict) -> dict:
    """All roles with flags, memberships and whether the login may change them (`can_manage`)."""
    with _maintenance(server, "list the roles") as conn, conn.cursor(row_factory=dict_row) as cur:
        caps = _caps(conn)
        rows = cur.execute(_ROLES_SQL, {"sup": caps["superuser"], "createrole": caps["createrole"],
                                        "v16": caps["version_num"] >= 160000}).fetchall()
    for r in rows:
        r.pop("oid")
        vu = r["valid_until"]
        r["valid_until"] = None if vu is None or vu.year >= 9999 else vu.isoformat()
    return {"capabilities": caps, "roles": rows}


def _role_name(name) -> str:
    name = check_name(name, "role name")
    if name.strip() != name or not name.strip():
        raise DbActionError("The role name cannot start or end with spaces.")
    if name.lower().startswith("pg_"):
        raise DbActionError('Role names starting with "pg_" are reserved by PostgreSQL.')
    if name.lower() == "public" or name.lower() in ("none", "current_user", "session_user", "current_role"):
        raise DbActionError(f'"{name}" cannot be used as a role name.')
    return name


def _valid_until(value):
    """None → no expiry; 'YYYY-MM-DD' or an ISO date-time → that moment (local server time zone if none given)."""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise DbActionError("'Password valid until' must be a date.")
    try:
        when = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        raise DbActionError(f"'Password valid until' is not a date: {value!r}.") from None
    return when.isoformat(sep=" ")


def _connlimit(value) -> int:
    if value in (None, ""):
        return -1
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < -1:
        raise DbActionError("The connection limit must be a whole number (-1 for no limit).")
    return value


def _memberships(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DbActionError("'Member of' must be a list of roles.")
    out, seen = [], set()
    for item in value:
        if isinstance(item, str):
            item = {"name": item, "admin": False}
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise DbActionError("Each 'member of' entry needs a role name.")
        if item["name"] in seen:
            continue
        seen.add(item["name"])
        out.append({"name": check_name(item["name"], "role name"), "admin": bool(item.get("admin"))})
    return out


def _options(spec: dict, *, creating: bool) -> list[sql.Composable]:
    """WITH options for CREATE / ALTER ROLE from a spec (only keys present when altering)."""
    parts: list[sql.Composable] = []
    for flag in ROLE_FLAGS:
        if flag in spec or (creating and flag in ("login", "inherit")):
            on = spec.get(flag, flag == "inherit")
            if not isinstance(on, bool):
                raise DbActionError(f"'{flag}' must be true or false.")
            parts.append(sql.SQL(_FLAG_SQL[flag] if on else "NO" + _FLAG_SQL[flag]))
    if "connlimit" in spec:
        parts.append(sql.SQL("CONNECTION LIMIT {}").format(sql.Literal(_connlimit(spec["connlimit"]))))
    if "valid_until" in spec:
        when = _valid_until(spec["valid_until"])
        parts.append(sql.SQL("VALID UNTIL {}").format(sql.Literal(when or "infinity")))
    return parts


def _password_part(spec: dict, mask: bool) -> sql.Composable | None:
    if spec.get("clear_password"):
        return sql.SQL("PASSWORD NULL")
    pw = spec.get("password")
    if pw in (None, ""):
        return None
    if not isinstance(pw, str):
        raise DbActionError("The password must be text.")
    if "\x00" in pw:
        raise DbActionError("The password cannot contain a NUL character.")
    return sql.SQL("PASSWORD {}").format(sql.SQL("'***'") if mask else sql.Literal(scram_verifier(pw)))


def build_create(spec: dict, *, mask: bool = False) -> list[sql.Composable]:
    """Statements for a new role: CREATE ROLE … then one GRANT per membership."""
    if not isinstance(spec, dict):
        raise DbActionError("Role details must be an object.")
    name = _role_name(spec.get("name"))
    opts = _options(spec, creating=True)
    pw = _password_part(spec, mask)
    if pw is not None:
        opts.append(pw)
    stmts = [sql.SQL("CREATE ROLE {} WITH {}").format(sql.Identifier(name), sql.SQL(" ").join(opts))]
    for m in _memberships(spec.get("member_of")):
        stmts.append(_grant(m["name"], name, m["admin"]))
    if isinstance(spec.get("comment"), str) and spec["comment"].strip():
        stmts.append(sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(name), sql.Literal(spec["comment"].strip())))
    return stmts


def _grant(role: str, member: str, admin: bool) -> sql.Composable:
    return sql.SQL("GRANT {} TO {}{}").format(sql.Identifier(role), sql.Identifier(member),
                                             sql.SQL(" WITH ADMIN OPTION") if admin else sql.SQL(""))


def build_alter(name: str, spec: dict, current: dict | None, *, mask: bool = False) -> list[sql.Composable]:
    """Statements to change role `name` to `spec` (only the keys given); memberships diffed against `current`."""
    if not isinstance(spec, dict):
        raise DbActionError("Role details must be an object.")
    name = check_name(name, "role name")
    stmts: list[sql.Composable] = []
    opts = _options(spec, creating=False)
    pw = _password_part(spec, mask)
    if pw is not None:
        opts.append(pw)
    if opts:
        stmts.append(sql.SQL("ALTER ROLE {} WITH {}").format(sql.Identifier(name), sql.SQL(" ").join(opts)))
    if "member_of" in spec:
        want = {m["name"]: m["admin"] for m in _memberships(spec["member_of"])}
        have = {m["name"]: m["admin"] for m in (current or {}).get("member_of", [])}
        for role in sorted(set(have) - set(want)):
            stmts.append(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(role), sql.Identifier(name)))
        for role in sorted(want):
            if role not in have or (want[role] and not have[role]):
                stmts.append(_grant(role, name, want[role]))
            elif have[role] and not want[role]:
                stmts.append(sql.SQL("REVOKE ADMIN OPTION FOR {} FROM {}").format(sql.Identifier(role), sql.Identifier(name)))
    if "comment" in spec and (current or {}).get("comment") != (spec["comment"] or None):
        text = spec["comment"].strip() if isinstance(spec["comment"], str) else ""
        stmts.append(sql.SQL("COMMENT ON ROLE {} IS {}").format(sql.Identifier(name), sql.Literal(text) if text else sql.NULL))
    new_name = spec.get("new_name")
    if new_name not in (None, "", name):
        stmts.append(sql.SQL("ALTER ROLE {} RENAME TO {}").format(sql.Identifier(name), sql.Identifier(_role_name(new_name))))
    return stmts


def _current_role(conn: psycopg.Connection, name: str) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT r.rolname AS name, pg_catalog.shobj_description(r.oid, 'pg_authid') AS comment,"
            " COALESCE((SELECT json_agg(json_build_object('name', b.rolname, 'admin', m.admin_option))"
            "  FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles b ON b.oid = m.roleid"
            "  WHERE m.member = r.oid), '[]') AS member_of"
            " FROM pg_catalog.pg_roles r WHERE r.rolname = %s", (name,)).fetchone()
    if row is None:
        raise DbActionError(f"Role {name} does not exist.", "not_found")
    return row


def preview(server: dict, spec: dict, name: str | None = None) -> list[str]:
    """The SQL a create (no `name`) or alter would run, password shown as '***'."""
    with _maintenance(server, "prepare the SQL") as conn:
        stmts = build_create(spec, mask=True) if name is None else build_alter(name, spec, _current_role(conn, name), mask=True)
        return [s.as_string(conn) + ";" for s in stmts]


def _run(conn: psycopg.Connection, stmts: list[sql.Composable], action: str) -> None:
    try:
        with conn.transaction():
            for s in stmts:
                conn.execute(s)
    except errors.InsufficientPrivilege as e:
        raise DbActionError(f"Could not {action}: {_primary(e)}", "permission") from e
    except errors.DuplicateObject as e:
        raise DbActionError(f"Could not {action}: {_primary(e)}", "exists") from e
    except errors.UndefinedObject as e:
        raise DbActionError(f"Could not {action}: {_primary(e)}", "not_found") from e
    except errors.DependentObjectsStillExist as e:
        detail = (e.diag.message_detail or "").strip()
        raise DbActionError(f"Could not {action}: {_primary(e)}." + (f" {detail}" if detail else "") +
                            " Reassign or drop those objects first (REASSIGN OWNED / DROP OWNED in each database).",
                            "in_use") from e
    except psycopg.Error as e:
        raise DbActionError(f"Could not {action}: {_primary(e)}") from e


def create_role(server: dict, spec: dict) -> dict:
    stmts = build_create(spec)
    with _maintenance(server, "create the role") as conn:
        _run(conn, stmts, f"create role {spec['name']}")
    log.info("created role %s on %s:%s (%d statements)", spec["name"], server.get("host"), server.get("port"), len(stmts))
    return {"name": spec["name"], "statements": len(stmts)}


def alter_role(server: dict, name: str, spec: dict) -> dict:
    with _maintenance(server, "change the role") as conn:
        stmts = build_alter(name, spec, _current_role(conn, name))
        if not stmts:
            return {"name": name, "statements": 0}
        _run(conn, stmts, f"change role {name}")
    final = spec.get("new_name") or name
    log.info("changed role %s on %s:%s (%d statements)", final, server.get("host"), server.get("port"), len(stmts))
    return {"name": final, "statements": len(stmts)}


def drop_role(server: dict, name: str, *, confirm: str | None) -> dict:
    name = check_name(name, "role name")
    if confirm != name:
        raise DbActionError(f"Type the role name ({name}) exactly to confirm.")
    if name.startswith("pg_"):
        raise DbActionError("Built-in pg_ roles cannot be dropped.", "protected")
    with _maintenance(server, "drop the role") as conn:
        if conn.execute("SELECT current_user = %s", (name,)).fetchone()[0]:
            raise DbActionError("This is the login the app uses for this server; it cannot drop itself.", "protected")
        _current_role(conn, name)
        _run(conn, [sql.SQL("DROP ROLE {}").format(sql.Identifier(name))], f"drop role {name}")
    log.info("dropped role %s on %s:%s", name, server.get("host"), server.get("port"))
    return {"name": name, "dropped": True}


# ---------------------------------------------------------------- SCRAM-SHA-256 (RFC 5802 / 7677, as PostgreSQL)

def _saslprep(password: str) -> str:
    """RFC 4013 SASLprep as PostgreSQL applies it; when the result is not valid, PostgreSQL uses the raw password."""
    mapped = "".join(" " if stringprep.in_table_c12(c) else "" if stringprep.in_table_b1(c) else c for c in password)
    norm = unicodedata.normalize("NFKC", mapped)
    if not norm:
        return password
    prohibited = (stringprep.in_table_c12, stringprep.in_table_c21_c22, stringprep.in_table_c3, stringprep.in_table_c4,
                  stringprep.in_table_c5, stringprep.in_table_c6, stringprep.in_table_c7, stringprep.in_table_c8,
                  stringprep.in_table_c9, stringprep.in_table_a1)
    if any(t(c) for c in norm for t in prohibited):
        return password
    if any(stringprep.in_table_d1(c) for c in norm):
        if not (stringprep.in_table_d1(norm[0]) and stringprep.in_table_d1(norm[-1])) \
                or any(stringprep.in_table_d2(c) for c in norm):
            return password
    return norm


def scram_verifier(password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """The value PostgreSQL stores for a password: SCRAM-SHA-256$<iter>:<salt>$<StoredKey>:<ServerKey>."""
    salt = salt or os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", _saslprep(password).encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")   # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


__all__ = ["capabilities", "list_sessions", "signal_sessions", "list_roles", "create_role", "alter_role", "drop_role",
           "preview", "build_create", "build_alter", "scram_verifier", "EngineError"]
