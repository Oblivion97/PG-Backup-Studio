"""Step 5: the JSON API in app.py (Flask test client). ARCHITECTURE 12.5.

Tests without a database use fake engines; the rest run against real PostgreSQL. Every response body
of the suite is scanned for the passwords used here.
"""
import os
import threading
import time

import psycopg
import pytest

import app as app_mod
from app import create_app
from conftest import connect, pg_settings
from jobs import JobManager
from store import Store

P = "pgbs_test_s5_"
SECRETS = ["s5-stored-secret", "pw_s5"]
SEEN: list[str] = []


class Api:
    """Test client that sends the guard header and records every response body."""

    def __init__(self, flask_app):
        self.c = flask_app.test_client()

    def call(self, method, path, json=None, headers=None, **kw):
        h = {"X-PGBS": "1", **(headers or {})}
        r = getattr(self.c, method)(path, json=json, headers=h, **kw)
        SEEN.append(r.get_data(as_text=True))
        return r

    def get(self, path, **kw):
        return self.call("get", path, **kw)

    def post(self, path, json=None, **kw):
        return self.call("post", path, json=json, **kw)

    def delete(self, path, json=None, **kw):
        return self.call("delete", path, json=json, **kw)


def teardown_module(module):
    leaked = [s for s in SEEN if any(secret in s for secret in SECRETS)]
    assert not leaked, f"a password was returned by the API: {leaked[0][:200]}"


class Gate:
    """Fake engine that waits until released (keeps a job running)."""

    def __init__(self):
        self.go = threading.Event()

    def __call__(self, *a, progress=None, cancel=None, **k):
        while not self.go.wait(0.02):
            if cancel.is_set():
                from engine.util import Cancelled
                raise Cancelled("Cancelled by user")
        return {"file": "x.sql", "size": 1, "objects": 0, "tables": 0, "rows": 0, "warnings": [], "status": "ok"}


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def gate():
    g = Gate()
    yield g
    g.go.set()


@pytest.fixture
def api(st, gate):
    mgr = JobManager(st, backup_fn=gate, restore_fn=gate, prepare_fn=lambda job: None)
    return Api(create_app(st, mgr))


FORM = {"name": "Local", "host": "localhost", "port": 5432, "user": "postgres", "password": "s5-stored-secret"}


# ---------------------------------------------------------------- 1. guard and errors

def test_guard(api):
    assert api.c.get("/").status_code == 200                                 # the page needs no header
    r = api.c.get("/api/servers")
    assert r.status_code == 403 and r.get_json()["code"] == "forbidden"
    r = api.get("/api/servers", headers={"Host": "evil.example:8000"})
    assert r.status_code == 403
    assert api.get("/api/servers", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    r = api.call("post", "/api/settings", data="history_limit=60", headers={"Content-Type": "text/plain"})
    assert r.status_code == 400 and "JSON" in r.get_json()["error"]
    r = api.post("/api/settings", json=[1, 2])
    assert r.status_code == 400 and "object" in r.get_json()["error"]
    r = api.get("/api/nope")
    assert r.status_code == 404 and r.get_json()["code"] == "not_found"


def test_unexpected_error_is_500_without_details(api, st, monkeypatch):
    def boom():
        raise RuntimeError("secret internals at C:\\x")
    monkeypatch.setattr(st, "list_history", boom)
    r = api.get("/api/history")
    assert r.status_code == 500 and r.get_json() == {"error": "Unexpected error: RuntimeError", "code": "unexpected"}


def test_app_info_and_settings(api):
    r = api.get("/api/app")
    assert r.status_code == 200 and r.get_json()["version"] == app_mod.APP_VERSION
    assert api.get("/api/settings").get_json()["history_limit"] == 1000
    r = api.post("/api/settings", json={"history_limit": 5})
    assert r.status_code == 400 and r.get_json()["code"] == "invalid"
    assert api.post("/api/settings", json={"history_limit": 60}).get_json()["history_limit"] == 60


# ---------------------------------------------------------------- 2. servers (no database)

def test_servers_crud(api):
    r = api.post("/api/servers", json=FORM)
    assert r.status_code == 201
    rec = r.get_json()
    assert rec["has_password"] is True and "password" not in rec and rec["password_cached"] is False
    r = api.post("/api/servers", json={"id": rec["id"], "port": 5433})
    assert r.status_code == 200 and r.get_json()["port"] == 5433
    assert [s["id"] for s in api.get("/api/servers").get_json()] == [rec["id"]]
    r = api.post("/api/servers", json={**FORM, "name": "local"})
    assert r.status_code == 400 and "already exists" in r.get_json()["error"]
    assert api.post("/api/servers", json={"id": "nope", "name": "x"}).status_code == 404
    assert api.delete(f"/api/servers/{rec['id']}").get_json() == {"deleted": True}
    assert api.delete(f"/api/servers/{rec['id']}").status_code == 404


def test_missing_password_asks_for_it(api):
    sid = api.post("/api/servers", json={**FORM, "password": None, "remember_password": False}).get_json()["id"]
    for path in (f"/api/servers/{sid}/databases", f"/api/servers/{sid}/info"):
        r = api.get(path)
        assert r.status_code == 401 and r.get_json()["code"] == "password_required"
    r = api.post("/api/backup", json={"server_id": sid, "database": "x"})
    assert r.status_code == 401
    assert api.get("/api/servers/nope/databases").status_code == 404


# ---------------------------------------------------------------- jobs, history (fake engine)

def test_jobs_history_and_open_folder(api, st, gate, tmp_path, monkeypatch):
    sid = api.post("/api/servers", json=FORM).get_json()["id"]
    r = api.post("/api/backup", json={"server_id": sid, "database": "shop", "options": {"out_dir": str(tmp_path)}})
    assert r.status_code == 202
    jid = r.get_json()["job_id"]
    snap = api.get(f"/api/jobs/{jid}").get_json()
    assert snap["status"] == "running" and snap["database"] == "shop"
    assert [j["id"] for j in api.get("/api/jobs").get_json()] == [jid]
    r = api.delete(f"/api/history/{jid}")
    assert r.status_code == 409                                              # still running
    r = api.delete(f"/api/servers/{sid}/databases/shop", json={"confirm": "shop"})
    assert r.status_code == 409 and r.get_json()["code"] == "conflict"       # a job uses it
    assert api.post(f"/api/servers/{sid}/databases/shop/kill").status_code == 409
    r = api.post(f"/api/servers/{sid}/databases/shop/rename", json={"new_name": "x"})
    assert r.status_code == 409
    r = api.post("/api/backup", json={"server_id": sid, "database": "shop", "options": {"mode": "bad"}})
    assert r.status_code == 400
    assert api.post(f"/api/jobs/{jid}/cancel").get_json() == {"cancelled": True}
    end = time.monotonic() + 5
    while api.get(f"/api/jobs/{jid}").get_json()["status"] == "running":
        assert time.monotonic() < end
        time.sleep(0.02)
    assert api.get(f"/api/jobs/{jid}").get_json()["status"] == "cancelled"
    assert api.get("/api/jobs/nope").status_code == 404
    assert api.post("/api/jobs/nope/cancel").status_code == 404
    assert api.get("/api/history").get_json()[0]["status"] == "cancelled"

    f = tmp_path / "b.sql"
    f.write_text("x")
    st.add_history({"id": "aaaaaaaaaaaa", "kind": "backup", "status": "success", "file": str(f)})
    calls = []
    monkeypatch.setattr(app_mod.subprocess, "Popen", lambda args, **k: calls.append(args))
    monkeypatch.setattr(app_mod.os, "startfile", lambda p: calls.append(["startfile", p]), raising=False)
    r = api.post("/api/history/aaaaaaaaaaaa/open-folder")
    assert r.status_code == 200 and r.get_json()["opened"] == str(f) and str(f) in " ".join(calls[0])
    f.unlink()
    assert api.post("/api/history/aaaaaaaaaaaa/open-folder").get_json()["opened"] == str(tmp_path)
    assert api.post("/api/history/nope/open-folder").status_code == 404
    r = api.post("/api/restore", json={"server_id": sid, "database": "t", "history_id": jid})
    assert r.status_code == 404                                              # not a finished backup
    r = api.post("/api/restore", json={"server_id": sid, "database": "t", "file": str(tmp_path / "missing.sql")})
    assert r.status_code == 400
    r = api.delete("/api/history/aaaaaaaaaaaa?delete_file=1")
    assert r.status_code == 200 and r.get_json()["file_deleted"] is False
    assert api.delete("/api/history/aaaaaaaaaaaa").status_code == 404


def test_profile_endpoints(api, st):
    sid = api.post("/api/servers", json=FORM).get_json()["id"]
    assert api.get(f"/api/servers/{sid}/databases/shop/profile").get_json() == {"profile": None}
    st.save_profile(sid, "shop", {"saved_at": "2026-10-27T10:00:00+06:00", "options": {"mode": "full"}})
    assert api.get(f"/api/servers/{sid}/databases/shop/profile").get_json()["profile"]["options"] == {"mode": "full"}
    assert api.delete(f"/api/servers/{sid}/databases/shop/profile").get_json() == {"deleted": True}


# ---------------------------------------------------------------- real PostgreSQL

@pytest.fixture
def pg_form(pg_admin):
    s = pg_settings()
    return {"name": "pg", "host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


@pytest.fixture
def real(tmp_path, pg_form):
    st = Store(tmp_path / "rdata")
    a = Api(create_app(st, JobManager(st)))
    sid = a.post("/api/servers", json=pg_form).get_json()["id"]
    return a, sid, st


@pytest.fixture
def dbs(pg_admin):
    names = []

    def drop(n):
        assert n.startswith(P)
        pg_admin.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(psycopg.sql.Identifier(n)))

    def add(n):
        names.append(n)
        drop(n)
        return n
    yield add
    for n in names:
        drop(n)


@pytest.fixture
def s5_role(pg_admin):
    name = P + "login"
    pg_admin.execute(f'DROP ROLE IF EXISTS "{name}"')
    pg_admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD 'pw_s5'")
    yield name
    pg_admin.execute(f'DROP ROLE IF EXISTS "{name}"')


def test_test_connection(api, pg_form):
    r = api.post("/api/servers/test", json=pg_form)
    assert r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["user"] == pg_form["user"]
    r = api.post("/api/servers/test", json={**pg_form, "password": "wrong-pw"})
    assert r.status_code == 200 and r.get_json()["ok"] is False and "password" in r.get_json()["error"]
    sid = api.post("/api/servers", json=pg_form).get_json()["id"]
    r = api.post("/api/servers/test", json={"id": sid, "host": pg_form["host"], "user": pg_form["user"]})
    assert r.get_json()["ok"] is True                                        # uses the stored password
    assert api.post("/api/servers/test", json={**pg_form, "port": "x"}).status_code == 400


def test_password_prompt_flow(real, pg_form, s5_role):
    a, _, st = real
    sid = a.post("/api/servers", json={**pg_form, "name": "nopw", "user": s5_role, "password": None,
                                       "remember_password": False}).get_json()["id"]
    assert a.get(f"/api/servers/{sid}/databases").status_code == 401
    r = a.post(f"/api/servers/{sid}/connect", json={"password": "wrong"})
    assert r.status_code == 401 and r.get_json()["code"] == "password_required"
    r = a.post(f"/api/servers/{sid}/connect", json={"password": "pw_s5"})
    assert r.status_code == 200 and r.get_json()["user"] == s5_role and r.get_json()["can_create_db"] is False
    assert a.get(f"/api/servers/{sid}/databases").status_code == 200
    assert a.get("/api/servers").get_json()[-1]["password_cached"] is True
    assert "pw_s5" not in (st.dir / "servers.json").read_text(encoding="utf-8")
    a.post("/api/servers", json={"id": sid, "port": pg_form["port"]})          # editing forgets it
    assert a.get(f"/api/servers/{sid}/databases").status_code == 401
    # a stored password that the server rejects -> 401 as well
    bad = a.post("/api/servers", json={**pg_form, "name": "badpw", "user": s5_role, "password": "nope"}).get_json()
    r = a.get(f"/api/servers/{bad['id']}/databases")
    assert r.status_code == 401 and r.get_json()["code"] == "password_required"


def test_database_actions(real, pg_admin, dbs):
    a, sid, _ = real
    name, clone, renamed = dbs(P + "a"), dbs(P + "a_clone"), dbs(P + "a_new")
    base = f"/api/servers/{sid}/databases"
    r = a.post(base, json={"name": name})
    assert r.status_code == 201 and r.get_json()["name"] == name
    assert a.post(base, json={"name": name}).status_code == 409
    assert a.post(base, json={"name": P + "x" * 70}).status_code == 400
    assert a.post(base, json={"name": 5}).status_code == 400
    with connect(name) as c:
        c.execute("CREATE SCHEMA s; CREATE TABLE s.t AS SELECT 1 AS x")
    rows = {r["name"]: r for r in a.get(base).get_json()}
    assert rows[name]["busy"] == [] and rows["postgres"]["protected"] is True
    assert a.get(f"{base}/{name}/schemas").get_json() == ["public", "s"]
    assert a.get(f"{base}/{name}/contents").get_json()["tables"] == 1
    assert a.get(f"{base}/{P}missing/contents").status_code == 404
    assert a.get(f"{base}/{P}missing/schemas").status_code == 404
    check = a.post(f"{base}/{name}/check", json={"schemas": ["s"]})
    assert check.status_code == 200 and [t["name"] for t in check.get_json()["tables"]] == ["t"]
    assert a.post(f"{base}/{name}/check", json={"schemas": "s"}).status_code == 400
    assert a.post(f"{base}/{name}/check", json={"schemas": ["nope"]}).status_code == 400
    s = connect(name)
    try:
        assert a.get(f"{base}/{name}/sessions").get_json() == {"sessions": 1}
        r = a.post(base, json={"name": clone, "clone_from": name})
        assert r.status_code == 201
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001 - terminated by the clone
            pass
    assert a.post(f"{base}/{name}/kill").get_json()["terminated"] == 0
    r = a.post(f"{base}/{name}/rename", json={"new_name": renamed})
    assert r.status_code == 200 and r.get_json()["name"] == renamed
    assert a.post(f"{base}/postgres/rename", json={"new_name": P + "z"}).status_code == 403
    assert a.delete(f"{base}/{renamed}", json={"confirm": "wrong"}).status_code == 400
    assert a.delete(f"{base}/template1", json={"confirm": "template1"}).status_code == 403
    r = a.delete(f"{base}/{renamed}", json={"confirm": renamed})
    assert r.status_code == 200 and r.get_json()["name"] == renamed
    assert a.delete(f"{base}/{renamed}", json={"confirm": renamed}).status_code == 404
    assert [x["name"] for x in a.get(f"/api/servers/{sid}/roles").get_json()]
    assert a.get(f"/api/servers/{sid}/templates").get_json()[0] == "template0"


def test_backup_then_restore_from_history(real, pg_admin, dbs, tmp_path):
    a, sid, st = real
    src, target = dbs(P + "src"), dbs(P + "dst")
    pg_admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(src)))
    with connect(src) as c:
        c.execute("CREATE TABLE items AS SELECT g AS id FROM generate_series(1, 50) g")

    def finish(jid):
        end = time.monotonic() + 60
        while (snap := a.get(f"/api/jobs/{jid}").get_json())["status"] == "running":
            assert time.monotonic() < end
            time.sleep(0.05)
        return snap
    r = a.post("/api/backup", json={"server_id": sid, "database": src, "options": {"out_dir": str(tmp_path / "bk")}})
    assert r.status_code == 202
    bk = finish(r.get_json()["job_id"])
    assert bk["status"] == "success" and os.path.isfile(bk["file"])
    assert a.get(f"/api/servers/{sid}/databases/{src}/profile").get_json()["profile"]["options"]["out_dir"]
    hist = a.get("/api/history").get_json()[0]
    assert hist["id"] == bk["id"] and hist["status"] == "success"
    r = a.post("/api/restore", json={"server_id": sid, "database": target, "history_id": hist["id"],
                                     "target": {"mode": "create"}})
    assert r.status_code == 202
    rs = finish(r.get_json()["job_id"])
    assert rs["status"] == "success", rs["error"]
    with connect(target) as c:
        assert c.execute("SELECT count(*) FROM items").fetchone()[0] == 50
    r = a.post("/api/restore", json={"server_id": sid, "database": target, "history_id": hist["id"],
                                     "target": {"mode": "recreate"}})
    assert r.status_code == 400 and "confirm" in r.get_json()["error"]
