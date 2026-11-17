"""Admin panel (decision 011): sessions and roles & users. Unit tests run anywhere; the rest need PostgreSQL."""
import base64
import hashlib
import hmac
import re
import secrets
import threading
import time

import pytest

from engine import admin
from engine.db import DbActionError

from conftest import PREFIX, connect, pg_settings

H = {"X-PGBS": "1"}


def _pw() -> str:
    """A fresh random password per run for the throwaway pgbs_test_ roles (no fixed secrets in the code)."""
    return "T-" + secrets.token_urlsafe(18)


PW_USER, PW_NEW, PW_SESS, PW_PLAIN, PW_API, PW_UNIT = (_pw() for _ in range(6))


# ---------------------------------------------------------------- unit: SCRAM, SQL building

def test_scram_verifier_format_and_keys():
    salt = b"0123456789abcdef"
    v = admin.scram_verifier("pencil", salt=salt)
    m = re.fullmatch(r"SCRAM-SHA-256\$4096:([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+):([A-Za-z0-9+/=]+)", v)
    assert m and base64.b64decode(m.group(1)) == salt
    salted = hashlib.pbkdf2_hmac("sha256", b"pencil", salt, 4096)
    stored = hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    assert base64.b64decode(m.group(2)) == stored
    assert admin.scram_verifier("pencil") != admin.scram_verifier("pencil")   # random salt
    assert admin._saslprep("café") == "café" and admin._saslprep("a b") == "a b"   # NFKC / space mapping


def as_text(stmts):
    # render without a connection: good enough to check shape (psycopg needs a conn for exact quoting)
    out = []
    for s in stmts:
        out.append(re.sub(r"\s+", " ", repr(s)))
    return out


def test_build_create_user_with_memberships_masks_password():
    stmts = admin.build_create({"name": "alice", "login": True, "password": PW_UNIT, "createdb": True, "connlimit": 5,
                                "valid_until": "2030-01-31", "member_of": ["readers", {"name": "writers", "admin": True}]},
                               mask=True)
    text = " ".join(as_text(stmts))
    assert len(stmts) == 3
    assert "CREATE ROLE" in text and "'alice'" in text and "LOGIN" in text and "CREATEDB" in text and "INHERIT" in text
    assert "CONNECTION LIMIT" in text and "2030-01-31" in text and "***" in text and "SCRAM" not in text
    assert "WITH ADMIN OPTION" in text and "'writers'" in text and "'readers'" in text
    real = " ".join(as_text(admin.build_create({"name": "alice", "login": True, "password": PW_UNIT})))
    assert "SCRAM-SHA-256$4096:" in real and PW_UNIT not in real


def test_build_alter_diffs_memberships_and_renames_last():
    current = {"name": "bob", "comment": None, "member_of": [{"name": "a", "admin": False}, {"name": "b", "admin": True},
                                                              {"name": "c", "admin": False}]}
    stmts = as_text(admin.build_alter("bob", {"member_of": [{"name": "b", "admin": False}, {"name": "c", "admin": True},
                                                            "d"], "new_name": "robert", "login": False}, current))
    joined = "\n".join(stmts)
    assert "NOLOGIN" in stmts[0]
    assert "REVOKE" in joined and "'a'" in joined                       # a removed
    assert "REVOKE ADMIN OPTION FOR" in joined                          # b: admin taken away
    assert joined.count("WITH ADMIN OPTION") == 1                       # c: admin given
    assert "RENAME TO" in stmts[-1] and "'robert'" in stmts[-1]
    assert admin.build_alter("bob", {}, current) == []


@pytest.mark.parametrize("bad, msg", [
    ({"name": "pg_mine"}, "reserved"), ({"name": "public"}, "cannot be used"), ({"name": " x"}, "spaces"),
    ({"name": "x" * 64}, "too long"), ({"name": "u", "connlimit": -5}, "connection limit"),
    ({"name": "u", "valid_until": "next week"}, "not a date"), ({"name": "u", "login": "yes"}, "true or false"),
    ({"name": "u", "member_of": "readers"}, "list"),
])
def test_build_create_rejects(bad, msg):
    with pytest.raises(DbActionError, match=msg):
        admin.build_create(bad)


def test_signal_rejects_bad_input_before_connecting():
    with pytest.raises(DbActionError, match="Unknown action"):
        admin.signal_sessions({}, [1], "kill")
    for pids in ([], "1", [0], [True], list(range(1, 600))):
        with pytest.raises(DbActionError, match="Choose between"):
            admin.signal_sessions({}, pids, "cancel")


# ---------------------------------------------------------------- PostgreSQL

ROLE = PREFIX + "admin_user"
GROUP = PREFIX + "admin_group"
GROUP2 = PREFIX + "admin_group2"


def server() -> dict:
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"], "sslmode": "prefer",
            "maintenance_db": "postgres"}


def drop_test_roles(conn):
    for r in (ROLE, ROLE + "_renamed", GROUP, GROUP2):
        conn.execute(f'DROP ROLE IF EXISTS "{r}"')


@pytest.fixture
def clean_roles(pg_admin):
    drop_test_roles(pg_admin)
    yield
    drop_test_roles(pg_admin)


def test_capabilities_of_superuser(pg_admin):
    caps = admin.capabilities(server())
    assert caps["superuser"] and caps["can_manage_roles"] and caps["can_end_others"]
    assert caps["user"] == pg_settings()["user"] and caps["version_num"] >= 120000


def test_create_user_login_alter_and_drop(pg_admin, clean_roles):
    srv = server()
    admin.create_role(srv, {"name": GROUP, "login": False})
    admin.create_role(srv, {"name": GROUP2, "login": False})
    admin.create_role(srv, {"name": ROLE, "login": True, "password": PW_USER, "connlimit": 3,
                            "member_of": [{"name": GROUP, "admin": True}], "comment": "made by the test"})
    roles = {r["name"]: r for r in admin.list_roles(srv)["roles"]}
    me = roles[ROLE]
    assert me["login"] and me["connlimit"] == 3 and me["can_manage"] and me["comment"] == "made by the test"
    assert me["member_of"] == [{"name": GROUP, "admin": True}] and roles[GROUP]["members"] == [ROLE]

    # the SCRAM verifier the app computed works for a real login; a wrong password does not
    s = pg_settings()
    import psycopg
    with psycopg.connect(host=s["host"], port=s["port"], user=ROLE, password=PW_USER, dbname="postgres") as c:
        assert c.execute("SELECT current_user").fetchone()[0] == ROLE
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(host=s["host"], port=s["port"], user=ROLE, password="wrong", dbname="postgres", connect_timeout=5)
    stored = pg_admin.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s", (ROLE,)).fetchone()[0]
    assert stored.startswith("SCRAM-SHA-256$4096:")

    preview = admin.preview(srv, {"password": PW_UNIT, "member_of": [GROUP2]}, ROLE)
    assert any("PASSWORD '***'" in p for p in preview) and any("REVOKE" in p for p in preview)

    admin.alter_role(srv, ROLE, {"password": PW_NEW, "member_of": [{"name": GROUP2, "admin": False}],
                                 "valid_until": "2099-12-31", "new_name": ROLE + "_renamed", "comment": ""})
    roles = {r["name"]: r for r in admin.list_roles(srv)["roles"]}
    assert ROLE not in roles
    r2 = roles[ROLE + "_renamed"]
    assert r2["member_of"] == [{"name": GROUP2, "admin": False}] and r2["valid_until"].startswith("2099-12-31")
    assert r2["comment"] is None
    with psycopg.connect(host=s["host"], port=s["port"], user=ROLE + "_renamed", password=PW_NEW, dbname="postgres"):
        pass

    with pytest.raises(DbActionError, match="exactly"):
        admin.drop_role(srv, ROLE + "_renamed", confirm="nope")
    admin.drop_role(srv, ROLE + "_renamed", confirm=ROLE + "_renamed")
    assert ROLE + "_renamed" not in {r["name"] for r in admin.list_roles(srv)["roles"]}


def test_errors_are_coded(pg_admin, clean_roles):
    srv = server()
    admin.create_role(srv, {"name": GROUP})
    with pytest.raises(DbActionError) as e:
        admin.create_role(srv, {"name": GROUP})
    assert e.value.code == "exists"
    with pytest.raises(DbActionError) as e:
        admin.create_role(srv, {"name": ROLE, "member_of": ["pgbs_test_no_such_role"]})
    assert e.value.code == "not_found"
    assert ROLE not in {r["name"] for r in admin.list_roles(srv)["roles"]}   # the whole create was rolled back
    with pytest.raises(DbActionError) as e:
        admin.drop_role(srv, pg_settings()["user"], confirm=pg_settings()["user"])
    assert e.value.code == "protected"
    with pytest.raises(DbActionError) as e:
        admin.alter_role(srv, "pgbs_test_missing", {"login": True})
    assert e.value.code == "not_found"


def test_drop_role_that_owns_objects_explains(pg_admin, clean_roles):
    srv = server()
    admin.create_role(srv, {"name": ROLE, "login": True})
    pg_admin.execute(f'GRANT CONNECT ON DATABASE postgres TO "{ROLE}"')
    try:
        with pytest.raises(DbActionError) as e:
            admin.drop_role(srv, ROLE, confirm=ROLE)
        assert e.value.code == "in_use" and "REASSIGN OWNED" in str(e.value)
    finally:
        pg_admin.execute(f'REVOKE CONNECT ON DATABASE postgres FROM "{ROLE}"')


def test_sessions_list_cancel_and_terminate(pg_admin, clean_roles):
    srv = server()
    admin.create_role(srv, {"name": ROLE, "login": True, "password": PW_SESS})
    s = pg_settings()
    import psycopg
    victim = psycopg.connect(host=s["host"], port=s["port"], user=ROLE, password=PW_SESS, dbname="postgres",
                             autocommit=True, application_name="pgbs-test-victim")
    pid = victim.info.backend_pid
    try:
        # a long query to cancel
        errs = []

        def sleeper():
            try:
                victim.execute("SELECT pg_sleep(30)")
            except psycopg.Error as e:
                errs.append(type(e).__name__)

        t = threading.Thread(target=sleeper)
        t.start()
        time.sleep(0.7)
        listed = admin.list_sessions(srv)
        row = next(r for r in listed["sessions"] if r["pid"] == pid)
        assert row["user"] == ROLE and row["state"] == "active" and row["client"] and row["can_signal"]
        assert row["app"] == "pgbs-test-victim" and not row["this_app"]
        assert all(r["pid"] != row.get("self_pid") for r in listed["sessions"])

        out = admin.signal_sessions(srv, [pid], "cancel")
        assert out["done"] == [pid]
        t.join(10)
        assert errs == ["QueryCanceled"] and not victim.closed   # query stopped, connection kept

        out = admin.signal_sessions(srv, [pid, 999999], "terminate")
        assert out["done"] == [pid] and out["failed"][0]["pid"] == 999999
        time.sleep(0.3)
        with pytest.raises(psycopg.Error):
            victim.execute("SELECT 1")
        workers = [r for r in admin.list_sessions(srv)["sessions"] if not r["client"]]
        if workers:
            out = admin.signal_sessions(srv, [workers[0]["pid"]], "terminate")
            assert out["done"] == [] and "not ended from here" in out["failed"][0]["reason"]
    finally:
        victim.close()


def test_non_admin_login_is_refused(pg_admin, clean_roles):
    srv = server()
    admin.create_role(srv, {"name": ROLE, "login": True, "password": PW_PLAIN})
    plain = dict(srv, user=ROLE, password=PW_PLAIN)
    caps = admin.capabilities(plain)
    assert not caps["can_manage_roles"] and not caps["can_end_others"]
    assert all(not r["can_manage"] for r in admin.list_roles(plain)["roles"])
    with pytest.raises(DbActionError) as e:
        admin.create_role(plain, {"name": GROUP})
    assert e.value.code == "permission"
    own = connect()   # a superuser session the plain login must not end
    try:
        pid = own.info.backend_pid
        row = next(r for r in admin.list_sessions(plain)["sessions"] if r["pid"] == pid)
        assert not row["can_signal"]
        out = admin.signal_sessions(plain, [pid], "terminate")
        assert out["done"] == [] and out["failed"]
    finally:
        own.close()


def test_api_routes(pg_admin, clean_roles, tmp_path):
    from app import create_app
    from engine.console import ConsoleManager
    from jobs import JobManager
    from store import Store
    st = Store(tmp_path / "data")
    s = pg_settings()
    sid = st.save_server({"name": "local", "host": s["host"], "port": s["port"], "user": s["user"],
                          "password": s["password"], "maintenance_db": "postgres"})["id"]
    c = create_app(st, JobManager(st), ConsoleManager()).test_client()
    base = f"/api/servers/{sid}/admin"
    assert c.get(base + "/capabilities", headers=H).get_json()["superuser"] is True
    assert c.get(base + "/sessions", headers=H).status_code == 200
    r = c.post(base + "/roles/preview", json={"spec": {"name": ROLE, "login": True, "password": PW_UNIT}}, headers=H)
    assert r.status_code == 200 and "PASSWORD '***'" in r.get_json()["sql"][0]
    assert c.post(base + "/roles", json={"name": ROLE, "login": True, "password": PW_API}, headers=H).status_code == 201
    assert c.post(base + "/roles", json={"name": ROLE}, headers=H).status_code == 409
    assert c.patch(f"{base}/roles/{ROLE}", json={"createdb": True}, headers=H).get_json()["statements"] == 1
    assert c.delete(f"{base}/roles/{ROLE}", json={"confirm": "x"}, headers=H).status_code == 400
    assert c.delete(f"{base}/roles/{ROLE}", json={"confirm": ROLE}, headers=H).get_json()["dropped"] is True
    assert c.post(base + "/sessions/explode", json={"pids": [1]}, headers=H).status_code == 404
