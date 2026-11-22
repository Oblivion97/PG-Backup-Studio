"""Access rights (decision 015) and group members. Unit tests run anywhere; the rest need PostgreSQL."""
import secrets

import pytest

from engine import access, admin
from engine.db import DbActionError

from conftest import PREFIX, connect, drop_database, pg_settings

H = {"X-PGBS": "1"}
DB = PREFIX + "access"
GROUP = PREFIX + "acc_readers"
USER = PREFIX + "acc_user"
OWNER = PREFIX + "acc_owner"


def as_text(stmts):
    return [s.as_string(None) for s in stmts]


def state(**over):
    """A small fake `_read` result: schema app with t1 (role has SELECT) and t2 (nothing), 2 functions, sequences."""
    st = {
        "role": {"name": "r", "login": False, "superuser": False, "member_of": []},
        "database": {"name": "d", "owner": "o", "is_owner": False, "direct": [], "public": ["CONNECT", "TEMPORARY"],
                     "effective": ["CONNECT", "TEMPORARY"]},
        "schemas": [{"name": "app", "owner": "o", "is_owner": False, "direct": ["USAGE"], "public": [], "eff_usage": True,
                     "eff_create": False, "routines": 2, "routines_direct": 0, "routines_effective": 2, "sequences": 1,
                     "future": []},
                    {"name": "s2", "owner": "o", "is_owner": False, "direct": [], "public": [], "eff_usage": False,
                     "eff_create": False, "routines": 0, "routines_direct": 0, "routines_effective": 0, "sequences": 0,
                     "future": []}],
        "tables": [{"schema": "app", "name": "t1", "kind": "r", "is_owner": False, "direct": ["SELECT"], "effective": ["SELECT"],
                    "sequences": [["app", "t1_id_seq"]]},
                   {"schema": "app", "name": "t2", "kind": "r", "is_owner": False, "direct": [], "effective": [], "sequences": []},
                   {"schema": "s2", "name": "x", "kind": "v", "is_owner": False, "direct": [], "effective": [], "sequences": []}],
        "truncated": False,
    }
    st.update(over)
    return st


# ---------------------------------------------------------------- unit

def test_all_tables_diff_grants_missing_and_revokes_extra():
    sql = as_text(access.build_access(state(), [{"kind": "all_tables", "schema": "app", "privs": ["SELECT", "INSERT", "UPDATE"]}]))
    assert 'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA "app" TO "r"' in sql      # t2 lacked SELECT too
    assert not any("REVOKE" in s for s in sql)
    assert 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "app" TO "r"' in sql             # serial columns keep working
    assert not any("USAGE ON SCHEMA" in s for s in sql)                                     # already has usage


def test_all_tables_union_and_intersection():
    st = state()
    sql = as_text(access.build_access(st, [{"kind": "all_tables", "schema": "app", "privs": ["SELECT"]}]))
    assert sql == ['GRANT SELECT ON ALL TABLES IN SCHEMA "app" TO "r"']   # t2 lacked it; nothing to revoke
    sql = as_text(access.build_access(st, [{"kind": "all_tables", "schema": "app", "privs": []}]))
    assert 'REVOKE SELECT ON ALL TABLES IN SCHEMA "app" FROM "r"' in sql
    assert 'REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "app" FROM "r"' in sql
    assert 'REVOKE USAGE ON SCHEMA "app" FROM "r"' in sql                  # nothing left inside


def test_single_table_after_bulk_and_prerequisites():
    sql = as_text(access.build_access(state(), [
        {"kind": "all_tables", "schema": "app", "privs": ["SELECT"]},
        {"kind": "table", "schema": "app", "name": "t1", "privs": ["SELECT", "INSERT", "DELETE"]},
        {"kind": "table", "schema": "s2", "name": "x", "privs": ["SELECT"]}]))
    assert 'GRANT INSERT, DELETE ON TABLE "app"."t1" TO "r"' in sql
    assert 'GRANT USAGE, SELECT ON SEQUENCE "app"."t1_id_seq" TO "r"' in sql
    assert 'GRANT USAGE ON SCHEMA "s2" TO "r"' in sql                      # added so the view can be reached
    assert sql.index('GRANT USAGE ON SCHEMA "s2" TO "r"') < sql.index('GRANT SELECT ON TABLE "s2"."x" TO "r"')


def test_connect_added_when_missing_and_database_rights():
    st = state()
    st["database"]["effective"] = []
    sql = as_text(access.build_access(st, [{"kind": "table", "schema": "app", "name": "t2", "privs": ["SELECT"]}]))
    assert sql[0] == 'GRANT CONNECT ON DATABASE "d" TO "r"'
    st = state()
    st["database"]["direct"] = ["CONNECT", "CREATE"]
    sql = as_text(access.build_access(st, [{"kind": "database", "privs": ["CONNECT", "temporary"]}]))
    assert sql == ['REVOKE CREATE ON DATABASE "d" FROM "r"', 'GRANT TEMPORARY ON DATABASE "d" TO "r"']


def test_functions_create_and_future():
    sql = as_text(access.build_access(state(), [
        {"kind": "functions", "schema": "app", "execute": True},
        {"kind": "schema_create", "schema": "app", "create": True},
        {"kind": "future_tables", "schema": "app", "privs": ["SELECT"]}]))
    assert 'GRANT EXECUTE ON ALL ROUTINES IN SCHEMA "app" TO "r"' in sql
    assert 'GRANT CREATE ON SCHEMA "app" TO "r"' in sql
    assert 'ALTER DEFAULT PRIVILEGES FOR ROLE "o" IN SCHEMA "app" GRANT SELECT ON TABLES TO "r"' in sql
    assert access.build_access(state(), [{"kind": "functions", "schema": "app", "execute": False}]) == []   # none granted directly


def test_owned_tables_are_not_revoked_from_their_owner():
    st = state()
    st["tables"][0]["is_owner"] = True
    sql = as_text(access.build_access(st, [{"kind": "all_tables", "schema": "app", "privs": []}]))
    assert not any("ALL TABLES" in s for s in sql) and not any('"t1"' in s for s in sql)
    assert access.build_access(st, [{"kind": "table", "schema": "app", "name": "t1", "privs": []}]) == []


@pytest.mark.parametrize("change, msg", [
    ({"kind": "boom"}, "Unknown change"),
    ({"kind": "all_tables", "schema": "nope", "privs": []}, "does not exist"),
    ({"kind": "table", "schema": "app", "name": "nope", "privs": []}, "does not exist"),
    ({"kind": "all_tables", "schema": "app", "privs": ["DROP"]}, "Unknown privilege"),
    ({"kind": "all_tables", "schema": "app", "privs": "SELECT"}, "list"),
    ({"kind": "functions", "schema": "app", "execute": "yes"}, "true or false"),
])
def test_build_access_rejects(change, msg):
    with pytest.raises(DbActionError, match=msg):
        access.build_access(state(), [change])


def test_group_members_create_and_diff():
    sql = as_text(admin.build_create({"name": "g", "login": False, "members": ["a", "b", "a"]}))
    assert sql[1:] == ['GRANT "g" TO "a"', 'GRANT "g" TO "b"']
    sql = as_text(admin.build_alter("g", {"members": ["b", "c"]}, {"name": "g", "member_of": [], "members": ["a", "b"]}))
    assert sql == ['REVOKE "g" FROM "a"', 'GRANT "g" TO "c"']
    with pytest.raises(DbActionError, match="Members"):
        admin.build_create({"name": "g", "members": "a"})


# ---------------------------------------------------------------- PostgreSQL

def server() -> dict:
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"], "sslmode": "prefer",
            "maintenance_db": "postgres"}


def _drop_roles(conn):
    for r in (USER, GROUP, OWNER):
        conn.execute(f'DROP ROLE IF EXISTS "{r}"')


@pytest.fixture
def acc_db(pg_admin):
    drop_database(pg_admin, DB)
    _drop_roles(pg_admin)
    pg_admin.execute(f'CREATE ROLE "{OWNER}" NOLOGIN')
    pg_admin.execute(f'CREATE DATABASE "{DB}"')
    c = connect(DB)
    try:
        c.execute('CREATE SCHEMA app; CREATE SCHEMA "odd schema";'
                  'CREATE TABLE app.items (id serial PRIMARY KEY, name text);'
                  'CREATE TABLE app.notes (id int, t text);'
                  'CREATE VIEW app.item_names AS SELECT name FROM app.items;'
                  'CREATE TABLE "odd schema"."Odd ""T""" (x int);'
                  'CREATE FUNCTION app.answer() RETURNS int LANGUAGE sql AS $$SELECT 42$$;'
                  f'ALTER SCHEMA "odd schema" OWNER TO "{OWNER}";')
    finally:
        c.close()
    yield
    drop_database(pg_admin, DB)
    _drop_roles(pg_admin)


def test_read_apply_and_effective_through_group(pg_admin, acc_db):
    srv = server()
    pw = "T-" + secrets.token_urlsafe(18)
    admin.create_role(srv, {"name": GROUP, "login": False})
    admin.create_role(srv, {"name": USER, "login": True, "password": pw, "member_of": [GROUP]})
    d = access.read_access(srv, DB, GROUP)
    assert d["role"]["name"] == GROUP and {s["name"] for s in d["schemas"]} >= {"app", "odd schema"}
    items = next(t for t in d["tables"] if t["name"] == "items")
    assert items["direct"] == [] and items["sequences"] == [["app", "items_id_seq"]]

    changes = [{"kind": "all_tables", "schema": "app", "privs": ["SELECT", "INSERT", "UPDATE"]},
               {"kind": "table", "schema": "app", "name": "notes", "privs": ["SELECT"]},
               {"kind": "functions", "schema": "app", "execute": True},
               {"kind": "future_tables", "schema": "odd schema", "privs": ["SELECT"]},
               {"kind": "table", "schema": "odd schema", "name": 'Odd "T"', "privs": ["SELECT", "DELETE"]}]
    preview = access.preview_access(srv, DB, GROUP, changes)
    assert any("GRANT USAGE ON SCHEMA" in s and "odd schema" in s for s in preview)
    assert access.apply_access(srv, DB, GROUP, changes)["statements"] == len(preview)

    d = access.read_access(srv, DB, GROUP)
    by = {(t["schema"], t["name"]): t for t in d["tables"]}
    assert by[("app", "items")]["direct"] == ["SELECT", "INSERT", "UPDATE"]
    assert by[("app", "notes")]["direct"] == ["SELECT"]
    assert by[("odd schema", 'Odd "T"')]["direct"] == ["SELECT", "DELETE"]
    odd = next(s for s in d["schemas"] if s["name"] == "odd schema")
    assert odd["future"] == ["SELECT"] and odd["direct"] == ["USAGE"]

    # the user gets it through the group: it can really insert (serial sequence included) and read
    u = access.read_access(srv, DB, USER)
    assert next(t for t in u["tables"] if t["name"] == "items")["effective"][:3] == ["SELECT", "INSERT", "UPDATE"]
    s = pg_settings()
    import psycopg
    with psycopg.connect(host=s["host"], port=s["port"], user=USER, password=pw, dbname=DB, autocommit=True) as c:
        c.execute("INSERT INTO app.items (name) VALUES ('x')")
        assert c.execute("SELECT count(*) FROM app.items").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("DELETE FROM app.items")

    # applying the same again changes nothing; taking everything away cleans up schema usage too
    assert access.preview_access(srv, DB, GROUP, [{"kind": "all_tables", "schema": "app", "privs": ["SELECT", "INSERT", "UPDATE"]},
                                                 {"kind": "table", "schema": "app", "name": "notes", "privs": ["SELECT"]}]) == []
    access.apply_access(srv, DB, GROUP, [{"kind": "all_tables", "schema": "app", "privs": []},
                                         {"kind": "functions", "schema": "app", "execute": False}])
    d = access.read_access(srv, DB, GROUP)
    assert all(not t["direct"] for t in d["tables"] if t["schema"] == "app")
    assert next(s for s in d["schemas"] if s["name"] == "app")["direct"] == []


def test_missing_role_and_database(pg_admin, acc_db):
    with pytest.raises(DbActionError, match="does not exist"):
        access.read_access(server(), DB, PREFIX + "nobody")
    with pytest.raises(DbActionError, match="does not exist"):
        access.read_access(server(), PREFIX + "no_such_db", OWNER)


def test_group_members_on_server(pg_admin, acc_db):
    srv = server()
    admin.create_role(srv, {"name": USER, "login": True})
    admin.create_role(srv, {"name": GROUP, "login": False, "members": [USER]})
    g = next(r for r in admin.list_roles(srv)["roles"] if r["name"] == GROUP)
    assert g["members"] == [USER]
    admin.alter_role(srv, GROUP, {"members": []})
    g = next(r for r in admin.list_roles(srv)["roles"] if r["name"] == GROUP)
    assert g["members"] == []


def test_access_api_routes(pg_admin, acc_db, tmp_path):
    from app import create_app
    from engine.console import ConsoleManager
    from jobs import JobManager
    from store import Store
    st = Store(tmp_path / "data")
    s = pg_settings()
    sid = st.save_server({"name": "local", "host": s["host"], "port": s["port"], "user": s["user"],
                          "password": s["password"], "maintenance_db": "postgres"})["id"]
    c = create_app(st, JobManager(st), ConsoleManager()).test_client()
    base = f"/api/servers/{sid}/admin/access"
    assert c.get(base, headers=H).status_code == 400
    r = c.get(f"{base}?db={DB}&role={OWNER}", headers=H)
    assert r.status_code == 200 and r.get_json()["role"]["name"] == OWNER
    body = {"db": DB, "role": OWNER, "changes": [{"kind": "table", "schema": "app", "name": "notes", "privs": ["SELECT"]}]}
    r = c.post(base + "/preview", json=body, headers=H)
    assert r.status_code == 200 and any("GRANT SELECT" in x for x in r.get_json()["sql"])
    assert c.post(base, json=body, headers=H).get_json()["statements"] >= 1
    assert c.post(base, json={"db": DB, "role": OWNER, "changes": [{"kind": "bad"}]}, headers=H).status_code == 400
    assert c.get(f"{base}?db={DB}&role={PREFIX}nobody", headers=H).status_code == 404
