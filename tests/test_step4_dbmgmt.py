"""Step 4: database management in engine/db.py and restore targets in jobs.py (real PostgreSQL).

ARCHITECTURE 11.8. Every database and role created here is named pgbs_test_s4_*.
"""
import threading
import time

import psycopg
import pytest

from conftest import connect, pg_settings
from engine import db
from engine.backup import run_backup
from engine.db import DbActionError
from jobs import JobError, JobManager
from store import Store

P = "pgbs_test_s4_"
ODD = P + 'odd "name" ü 🙂'


@pytest.fixture
def server(pg_admin):
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


def _drop(admin, name):
    assert name.startswith(P)
    admin.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(psycopg.sql.Identifier(name)))


@pytest.fixture
def dbs(pg_admin):
    """Tracks database names to drop before and after the test."""
    names = []

    def add(name):
        names.append(name)
        _drop(pg_admin, name)
        return name
    yield add
    for n in names:
        _drop(pg_admin, n)


@pytest.fixture
def roles(pg_admin):
    made = []

    def make(name, extra=""):
        full = P + name
        pg_admin.execute(f'DROP ROLE IF EXISTS "{full}"')
        pg_admin.execute(f"CREATE ROLE \"{full}\" LOGIN PASSWORD 'pw_s4' {extra}")
        made.append(full)
        return full
    yield make
    for r in made:
        for row in pg_admin.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (P + "%",)).fetchall():
            _drop(pg_admin, row[0])
        pg_admin.execute(f'DROP ROLE IF EXISTS "{r}"')


def session(name):
    """An open idle session on `name` (closed by the caller or killed by the test)."""
    return connect(name)


def create(pg_admin, name, extra=""):
    pg_admin.execute(psycopg.sql.SQL("CREATE DATABASE {} " + extra).format(psycopg.sql.Identifier(name)))


def code_of(call):
    with pytest.raises(DbActionError) as e:
        call()
    return e.value.code, str(e.value)


# ---------------------------------------------------------------- 1. list

def test_list_databases(server, pg_admin, dbs, roles):
    name = dbs(P + "list")
    create(pg_admin, name)
    s1 = session(name)
    try:
        rows = {r["name"]: r for r in db.list_databases(server)}
    finally:
        s1.close()
    assert "template0" not in rows and "template1" not in rows
    r = rows[name]
    assert r["connections"] == 1 and r["size"] > 0 and r["protected"] is False and r["allow_connections"] is True
    assert r["encoding"] == "UTF8" and r["owner"] == server["user"]
    assert rows["postgres"]["protected"] is True
    # a login without CONNECT on the DB sees it with size None
    nc = roles("noconnect")
    pg_admin.execute(f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC')
    seen = {r["name"]: r for r in db.list_databases(dict(server, user=nc, password="pw_s4"))}
    assert seen[name]["size"] is None and seen["postgres"]["size"] is not None


def test_roles_templates_sessions(server, pg_admin, dbs, roles):
    r = roles("owner")
    names = [x["name"] for x in db.list_roles(server)]
    assert r in names and not any(n.startswith("pg_") for n in names)
    assert db.list_templates(server)[0] == "template0" and "template1" in db.list_templates(server)
    name = dbs(P + "sess")
    create(pg_admin, name)
    a, b = session(name), session(name)
    try:
        assert db.database_sessions(server, name) == 2
    finally:
        a.close()
        b.close()


def test_maintenance_fallback(server):
    conn = db.connect_maintenance(dict(server, maintenance_db=P + "missing"))
    try:
        assert conn.info.dbname == "postgres" and conn.autocommit
    finally:
        conn.close()
    rows = db.list_databases(dict(server, maintenance_db=P + "missing"))
    assert {r["name"]: r for r in rows}["postgres"]["protected"] is True


# ---------------------------------------------------------------- 2. create

def test_create_with_owner_template_encoding(server, dbs, roles):
    owner = roles("own")
    row = db.create_database(server, dbs(P + "c1"), owner=owner, template="template0", encoding="SQL_ASCII")
    assert (row["owner"], row["encoding"]) == (owner, "SQL_ASCII")
    assert db.create_database(server, dbs(P + "c2"))["encoding"] == "UTF8"
    code, msg = code_of(lambda: db.create_database(server, dbs(P + "c3"), encoding="SQL_ASCII"))
    assert code == "invalid" and "template0" in msg
    assert code_of(lambda: db.create_database(server, dbs(P + "c4"), encoding="NOPE"))[0] == "invalid"


def test_create_names(server, dbs):
    long_ok = dbs(P + "x" * (63 - len(P)))
    assert db.create_database(server, long_ok)["name"] == long_ok
    code, msg = code_of(lambda: db.create_database(server, P + "x" * (64 - len(P))))
    assert code == "invalid" and "63 bytes" in msg
    emoji_too_long = P + "🙂" * 13                  # 13 + 4*13 = 65 bytes
    assert code_of(lambda: db.create_database(server, emoji_too_long))[0] == "invalid"
    assert db.create_database(server, dbs(ODD))["name"] == ODD
    for bad in ("", None, "a\x00b"):
        assert code_of(lambda: db.create_database(server, bad))[0] == "invalid"
    assert code_of(lambda: db.create_database(server, ODD))[0] == "exists"


def test_create_errors(server, dbs, roles):
    code, msg = code_of(lambda: db.create_database(server, dbs(P + "e1"), owner=P + "norole"))
    assert code == "invalid" and "does not exist" in msg
    plain = roles("plain")
    code, msg = code_of(lambda: db.create_database(dict(server, user=plain, password="pw_s4"), dbs(P + "e2")))
    assert code == "permission" and plain in msg
    assert code_of(lambda: db.create_database(server, dbs(P + "e3"), template=P + "notemplate"))[0] == "not_found"


# ---------------------------------------------------------------- 3. drop

def test_drop(server, pg_admin, dbs):
    name = dbs(P + "drop")
    create(pg_admin, name)
    assert code_of(lambda: db.drop_database(server, name, confirm="wrong"))[0] == "invalid"
    assert code_of(lambda: db.drop_database(server, name, confirm=None))[0] == "invalid"
    a, b = session(name), session(name)
    try:
        assert db.drop_database(server, name, confirm=name) == {"name": name, "terminated": 2}
    finally:
        for c in (a, b):
            try:
                c.close()
            except Exception:  # noqa: BLE001 - already terminated
                pass
    assert name not in [r["name"] for r in db.list_databases(server)]
    assert code_of(lambda: db.drop_database(server, name, confirm=name))[0] == "not_found"
    for protected in ("template0", "template1", "postgres"):
        assert code_of(lambda: db.drop_database(server, protected, confirm=protected))[0] == "protected"
    other = dbs(P + "maint")
    create(pg_admin, other)
    code, msg = code_of(lambda: db.drop_database(dict(server, maintenance_db=other), other, confirm=other))
    assert code == "protected" and "connects through it" in msg


def test_drop_on_pg12_uses_no_force(server, pg_admin, dbs, monkeypatch):
    name = dbs(P + "pg12")
    create(pg_admin, name)
    seen = []
    real = psycopg.Connection.execute

    def spy(self, query, *a, **k):
        if isinstance(query, psycopg.sql.Composable):
            seen.append(query.as_string(self))
        return real(self, query, *a, **k)
    monkeypatch.setattr(psycopg.Connection, "execute", spy)
    monkeypatch.setattr(psycopg.ConnectionInfo, "server_version", property(lambda self: 120022))
    db.drop_database(server, name, confirm=name)
    drops = [q for q in seen if q.startswith("DROP DATABASE")]
    assert drops and "FORCE" not in drops[0]


# ---------------------------------------------------------------- 4. rename

def test_rename(server, pg_admin, dbs):
    old, new, taken = dbs(P + "ren_a"), dbs(P + "ren_b"), dbs(P + "ren_taken")
    create(pg_admin, old)
    create(pg_admin, taken)
    s = session(old)
    try:
        row = db.rename_database(server, old, new)
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    assert row["name"] == new
    assert code_of(lambda: db.rename_database(server, new, taken))[0] == "exists"
    assert code_of(lambda: db.rename_database(server, P + "nope", P + "x"))[0] == "not_found"
    assert code_of(lambda: db.rename_database(server, "template1", P + "x"))[0] == "protected"
    assert code_of(lambda: db.rename_database(server, new, P + "y" * 70))[0] == "invalid"


def test_rename_with_a_session_that_stays_gives_in_use_after_3_tries(server, pg_admin, dbs, monkeypatch):
    """A session that is back before every statement (a pool reconnecting) is simulated deterministically:
    terminating does nothing, so the session stays; the rename is tried RETRIES times, then `in_use`."""
    name = dbs(P + "busy")
    create(pg_admin, name)
    monkeypatch.setattr(db, "RETRY_DELAY_S", 0.01)
    tries = []
    monkeypatch.setattr(db, "_terminate", lambda conn, n: tries.append(n) or {"terminated": 0, "failed": 0})
    s = session(name)
    try:
        code, msg = code_of(lambda: db.rename_database(server, name, P + "busy2"))
    finally:
        s.close()
    assert code == "in_use" and "1 other session" in msg
    assert tries == [name] * db.RETRIES
    assert name in [r["name"] for r in db.list_databases(server)]


# ---------------------------------------------------------------- 5. clone

def test_clone(server, pg_admin, dbs):
    src, dst = dbs(P + "clone_src"), dbs(P + "clone_dst")
    create(pg_admin, src)
    with connect(src) as c:
        c.execute("CREATE TABLE t AS SELECT g FROM generate_series(1, 500) g")
    s = session(src)
    try:
        row = db.clone_database(server, src, dst)
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    assert row["name"] == dst
    with connect(dst) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 500
    assert code_of(lambda: db.clone_database(server, src, dst))[0] == "exists"
    assert code_of(lambda: db.clone_database(server, P + "nope", P + "x"))[0] == "not_found"


# ---------------------------------------------------------------- 6. kill

def test_kill_connections(server, pg_admin, dbs, roles):
    name = dbs(P + "kill")
    create(pg_admin, name)
    a, b = session(name), session(name)
    assert db.kill_connections(server, name) == {"name": name, "terminated": 2, "failed": 0}
    for c in (a, b):
        with pytest.raises(psycopg.OperationalError):
            c.execute("SELECT 1")
    # a role that may not end the superuser's session: counted, no exception
    plain = roles("killer")
    pg_admin.execute(f'GRANT CONNECT ON DATABASE "{name}" TO "{plain}"')
    keep = session(name)
    try:
        out = db.kill_connections(dict(server, user=plain, password="pw_s4"), name)
        assert out["terminated"] == 0 and out["failed"] == 1
        assert keep.execute("SELECT 1").fetchone()[0] == 1
    finally:
        keep.close()
    assert code_of(lambda: db.kill_connections(server, P + "nope"))[0] == "not_found"


# ---------------------------------------------------------------- 7. restore targets through jobs

@pytest.fixture
def mgr_sid(tmp_path, server):
    st = Store(tmp_path / "data")
    sid = st.save_server({"name": "pg", **server})["id"]
    return JobManager(st), sid, st


@pytest.fixture
def small_backup(server, pg_admin, dbs, tmp_path):
    src = dbs(P + "rsrc")
    create(pg_admin, src)
    with connect(src) as c:
        c.execute("CREATE TABLE items (id int PRIMARY KEY, v text)")
        c.execute("INSERT INTO items SELECT g, 'v' || g FROM generate_series(1, 100) g")
    return run_backup(server, src, {"out_dir": str(tmp_path / "bk")})["file"]


def wait(mgr, jid, timeout=60):
    end = time.monotonic() + timeout
    while mgr.get_job(jid)["status"] == "running":
        assert time.monotonic() < end
        time.sleep(0.05)
    return mgr.get_job(jid)


def count_items(name):
    with connect(name) as c:
        return c.execute("SELECT count(*) FROM items").fetchone()[0]


def test_restore_into_new_database(mgr_sid, small_backup, dbs, roles):
    mgr, sid, st = mgr_sid
    owner = roles("rowner")
    target = dbs(P + "rnew")
    snap = wait(mgr, mgr.start_restore(sid, target, small_backup, target={"mode": "create", "owner": owner}))
    assert snap["status"] == "success", snap["error"]
    assert count_items(target) == 100
    assert any("Created database" in x["msg"] for x in snap["log"])
    assert st.get_history(snap["id"])["target_mode"] == "create"
    row = {r["name"]: r for r in db.list_databases(dict(st.get_server(sid, with_password=True)))}[target]
    assert row["owner"] == owner and row["encoding"] == "UTF8"
    snap = wait(mgr, mgr.start_restore(sid, target, small_backup, target={"mode": "create"}))
    assert snap["status"] == "failed" and "already exists" in snap["error"]


def test_restore_recreate_replaces_a_busy_non_empty_database(mgr_sid, small_backup, pg_admin, dbs):
    mgr, sid, _ = mgr_sid
    target = dbs(P + "rrec")
    create(pg_admin, target)
    with connect(target) as c:
        c.execute("CREATE TABLE old_stuff (x int)")
    s = session(target)
    try:
        for bad in ({"mode": "recreate"}, {"mode": "recreate", "confirm": "wrong"}):
            with pytest.raises(JobError) as e:
                mgr.start_restore(sid, target, small_backup, target=bad)
            assert e.value.code == "invalid"
        snap = wait(mgr, mgr.start_restore(sid, target, small_backup, target={"mode": "recreate", "confirm": target}))
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    assert snap["status"] == "success", snap["error"]
    assert count_items(target) == 100
    with connect(target) as c:
        assert c.execute("SELECT to_regclass('old_stuff')").fetchone()[0] is None
    assert any("Dropped database" in x["msg"] for x in snap["log"])
    # recreate of a database that does not exist yet just creates it
    fresh = dbs(P + "rrec_new")
    assert wait(mgr, mgr.start_restore(sid, fresh, small_backup,
                                       target={"mode": "recreate", "confirm": fresh}))["status"] == "success"


def test_restore_into_existing_non_empty_is_refused_unless_allowed(mgr_sid, small_backup, pg_admin, dbs):
    mgr, sid, _ = mgr_sid
    target = dbs(P + "rexist")
    create(pg_admin, target, "TEMPLATE template0")
    with connect(target) as c:
        c.execute("CREATE TABLE other (x int)")
    snap = wait(mgr, mgr.start_restore(sid, target, small_backup))
    assert snap["status"] == "failed" and "not empty" in snap["error"] and "drop and recreate" in snap["error"]
    with connect(target) as c:
        assert c.execute("SELECT to_regclass('items')").fetchone()[0] is None   # nothing was run
    snap = wait(mgr, mgr.start_restore(sid, target, small_backup, target={"allow_non_empty": True}))
    assert snap["status"] == "success" and count_items(target) == 100
    missing = wait(mgr, mgr.start_restore(sid, P + "rmissing", small_backup))
    assert missing["status"] == "failed" and "does not exist" in missing["error"]


def test_restore_target_validation_and_protection(mgr_sid, small_backup):
    mgr, sid, _ = mgr_sid
    cases = [{"mode": "weird"}, "text", {"mode": "create", "owner": 5}, {"allow_non_empty": "yes"},
             {"mode": "recreate", "confirm": "template1"}, {"mode": "recreate", "confirm": "postgres"}]
    names = [P + "v", P + "v", P + "v", P + "v", "template1", "postgres"]
    for name, target in zip(names, cases):
        with pytest.raises(JobError) as e:
            mgr.start_restore(sid, name, small_backup, target=target)
        assert e.value.code == "invalid", target
    with pytest.raises(JobError):
        mgr.start_restore(sid, P + "z" * 70, small_backup, target={"mode": "create"})


def test_jobs_using_and_recreate_conflict(tmp_path, server, small_backup, dbs):
    st = Store(tmp_path / "data2")
    sid = st.save_server({"name": "pg", **server})["id"]
    gate = threading.Event()

    def slow_restore(*a, progress=None, cancel=None, **k):
        gate.wait(10)
        return {"status": "ok"}
    mgr = JobManager(st, restore_fn=slow_restore)
    target = dbs(P + "ruse")
    jid = mgr.start_restore(sid, target, small_backup, target={"mode": "create"})
    try:
        assert mgr.jobs_using(sid, target) == [jid] and mgr.jobs_using(sid, P + "other") == []
        with pytest.raises(JobError) as e:
            mgr.start_restore(sid, target, small_backup, target={"mode": "recreate", "confirm": target})
        assert e.value.code == "conflict"
    finally:
        gate.set()
    wait(mgr, jid)
    assert mgr.jobs_using(sid, target) == []


def test_cancel_during_preparing_stops_before_the_drop(mgr_sid, small_backup, pg_admin, dbs, monkeypatch):
    mgr, sid, _ = mgr_sid
    target = dbs(P + "rcancel")
    create(pg_admin, target)
    with connect(target) as c:
        c.execute("CREATE TABLE keep_me (x int)")
    started = threading.Event()
    real_check = __import__("jobs").check_cancel

    def slow_check(ev):
        started.set()
        ev.wait(5)
        real_check(ev)
    monkeypatch.setattr("jobs.check_cancel", slow_check)
    jid = mgr.start_restore(sid, target, small_backup, target={"mode": "recreate", "confirm": target})
    assert started.wait(5)
    assert mgr.cancel_job(jid)
    assert wait(mgr, jid)["status"] == "cancelled"
    with connect(target) as c:
        assert c.execute("SELECT to_regclass('keep_me')").fetchone()[0] is not None
