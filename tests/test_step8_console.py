"""Step 8a: SQL console sessions, completions and their API (ARCHITECTURE 14, decision 009)."""
import json
import threading
import time

import pytest

from app import create_app
from conftest import PREFIX, connect, drop_database, pg_settings
from engine.console import ConsoleError, ConsoleManager, first_word
from jobs import JobManager
from store import Store

WS_DB = f"{PREFIX}ws"
H = {"X-PGBS": "1"}


def server() -> dict:
    s = pg_settings()
    return {"id": "srv1", "host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


# ---------------------------------------------------------------- no database

def test_first_word():
    assert first_word("  select 1") == "SELECT"
    assert first_word("(SELECT 1) UNION SELECT 2") == "SELECT"
    assert first_word("with x as (select 1) select * from x") == "WITH"
    assert first_word("") == ""


def test_workspace_scripts_are_served_from_an_allowlist(tmp_path):
    st = Store(tmp_path / "data")
    client = create_app(st, JobManager(st), ConsoleManager()).test_client()
    r = client.get("/ws/console.js")
    assert r.status_code == 200 and "javascript" in r.content_type and b"function viewQuery" in r.data
    assert client.get("/ws/grid.js").status_code == 200
    for bad in ("app.py", "index.html", "..%2Fapp.py", "secret.js"):
        assert client.get(f"/ws/{bad}").status_code == 404


def test_unknown_session_is_reported_as_lost(tmp_path):
    st = Store(tmp_path / "data")
    client = create_app(st, JobManager(st), ConsoleManager()).test_client()
    r = client.post("/api/console/nope/execute", json={"sql": "select 1"}, headers=H)
    assert r.status_code == 410 and r.get_json()["code"] == "session_lost"
    assert client.delete("/api/console/nope", headers=H).get_json() == {"closed": False}


def test_bad_parameters_are_refused_before_running():
    m = ConsoleManager()
    with pytest.raises(ConsoleError):
        m.execute("x", "", 10)
    with pytest.raises(ConsoleError):
        m.execute("x", "select 1", 0)
    with pytest.raises(ConsoleError):
        m.execute("x", "select 1", 10, timeout_s=-1)
    with pytest.raises(ConsoleError, match="only comments"):
        m.execute("x", "-- nothing\n", 10)


# ---------------------------------------------------------------- PostgreSQL

@pytest.fixture
def ws_db(pg_admin):
    drop_database(pg_admin, WS_DB)
    pg_admin.execute(f'CREATE DATABASE "{WS_DB}" TEMPLATE template0')
    with connect(WS_DB) as c:
        c.execute("CREATE TABLE public.items (id int PRIMARY KEY, name text, price numeric(10,2));"
                  "INSERT INTO public.items VALUES (1, 'a', 1.50), (2, 'b', NULL);"
                  "CREATE VIEW public.cheap AS SELECT * FROM public.items WHERE price < 2;"
                  "CREATE FUNCTION public.twice(x int) RETURNS int LANGUAGE sql AS 'SELECT x * 2';")
    yield WS_DB
    drop_database(pg_admin, WS_DB)


@pytest.fixture
def mgr():
    m = ConsoleManager()
    yield m
    m.close_all()


def run(m, sid, sql, **kw):
    return m.execute(sid, sql, kw.pop("max_rows", 1000), **kw)


def test_values_come_back_as_postgres_text(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    res = run(mgr, sid, "SELECT 1.50::numeric(10,2) AS n, '2026-10-28 10:00:00+00'::timestamptz AT TIME ZONE 'UTC' AS t,"
                        " '\\x01ff'::bytea AS b, '{\"a\": 1}'::jsonb AS j, ARRAY[1,2] AS arr, NULL::text AS nothing, true AS ok")
    r = res["results"][0]
    assert r["error"] is None and r["has_rows"]
    assert [c["name"] for c in r["columns"]] == ["n", "t", "b", "j", "arr", "nothing", "ok"]
    assert [c["type"] for c in r["columns"]] == ["numeric(10,2)", "timestamp without time zone", "bytea", "jsonb",
                                                  "integer[]", "text", "boolean"]
    assert r["rows"] == [["1.50", "2026-10-28 10:00:00", "\\x01ff", '{"a": 1}', "{1,2}", None, "t"]]
    assert r["status"] == "SELECT 1"
    assert res["tx_status"] == "idle" and res["mode"] == "auto"


def test_table_columns_carry_their_table(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    r = run(mgr, sid, "SELECT id, name, 1 AS one FROM public.items ORDER BY id")["results"][0]
    assert r["columns"][0]["table_oid"] and r["columns"][0]["table_col"] == 1
    assert r["columns"][2]["table_oid"] is None
    assert r["rows"] == [["1", "a", "1"], ["2", "b", "1"]]


def test_row_limit_and_huge_results_stay_small(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    r = run(mgr, sid, "SELECT g FROM generate_series(1, 50) g", max_rows=10)["results"][0]
    assert r["row_count"] == 10 and r["truncated"] and r["status"] == "SELECT 10+"
    t0 = time.monotonic()
    r = run(mgr, sid, "SELECT g, repeat('x', 100) FROM generate_series(1, 20000000) g", max_rows=5)["results"][0]
    assert r["row_count"] == 5 and r["truncated"]
    assert time.monotonic() - t0 < 5, "a server-side cursor must not read the whole result"


def test_statements_run_in_order_and_stop_at_the_first_error(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    res = run(mgr, sid, "SELECT 1;\n\nSELECT\n  nope_column\nFROM public.items;\nSELECT 3;")["results"]
    assert len(res) == 2
    assert res[0]["rows"] == [["1"]] and res[0]["line"] == 1
    e = res[1]["error"]
    assert e["sqlstate"] == "42703" and "nope_column" in e["message"]
    assert res[1]["line"] == 3 and e["line"] == 4


def test_auto_commit_saves_each_statement(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    res = run(mgr, sid, "INSERT INTO public.items VALUES (3, 'c', 3);\nUPDATE public.items SET name = 'z' WHERE id = 3;")
    assert [r["status"] for r in res["results"]] == ["INSERT 0 1", "UPDATE 1"]
    with connect(ws_db) as c:
        assert c.execute("SELECT name FROM public.items WHERE id = 3").fetchone() == ("z",)


def test_statements_the_cursor_refuses_run_normally_once(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    r = run(mgr, sid, "WITH d AS (DELETE FROM public.items WHERE id = 2 RETURNING *) SELECT count(*) FROM d")["results"][0]
    assert r["error"] is None and r["rows"] == [["1"]]
    r = run(mgr, sid, "SELECT * INTO public.copy_of_items FROM public.items")["results"][0]
    assert r["error"] is None
    with connect(ws_db) as c:
        assert c.execute("SELECT count(*) FROM public.items").fetchone() == (1,)
        assert c.execute("SELECT count(*) FROM public.copy_of_items").fetchone() == (1,)


def test_notices_are_captured(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    r = run(mgr, sid, "DO $$ BEGIN RAISE NOTICE 'hello %', 42; END $$")["results"][0]
    assert r["error"] is None and {"severity": "NOTICE", "message": "hello 42"} in r["notices"]


def test_statement_timeout(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    r = run(mgr, sid, "SELECT pg_sleep(5)", timeout_s=0.5)["results"][0]
    assert r["error"]["sqlstate"] == "57014"
    assert run(mgr, sid, "SELECT 1", timeout_s=0)["results"][0]["error"] is None


def test_cancel_stops_a_long_statement(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    out = {}
    t = threading.Thread(target=lambda: out.update(run(mgr, sid, "SELECT pg_sleep(30); SELECT 2")))
    t0 = time.monotonic()
    t.start()
    for _ in range(50):
        if mgr.get(sid).running:
            break
        time.sleep(0.05)
    time.sleep(0.3)
    with pytest.raises(ConsoleError) as busy:
        run(mgr, sid, "SELECT 1")
    assert busy.value.code == "busy"
    assert mgr.cancel(sid) is True
    t.join(10)
    assert time.monotonic() - t0 < 6
    assert len(out["results"]) == 1 and out["results"][0]["error"]["message"] == "Cancelled."
    assert run(mgr, sid, "SELECT 2")["results"][0]["rows"] == [["2"]]


def test_manual_mode_commit_rollback_and_errors_keep_the_transaction(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    assert mgr.set_mode(sid, "manual")["mode"] == "manual"
    res = run(mgr, sid, "INSERT INTO public.items VALUES (10, 'tx', 1)")
    assert res["tx_status"] == "in_transaction"
    with connect(ws_db) as c:
        assert c.execute("SELECT count(*) FROM public.items WHERE id = 10").fetchone() == (0,)
    with pytest.raises(ConsoleError):
        mgr.set_mode(sid, "auto")
    assert mgr.rollback(sid)["tx_status"] == "idle"
    with connect(ws_db) as c:
        assert c.execute("SELECT count(*) FROM public.items WHERE id = 10").fetchone() == (0,)
    # an error undoes only that statement; the transaction goes on
    res = run(mgr, sid, "INSERT INTO public.items VALUES (11, 'x', 1)")
    res = run(mgr, sid, "INSERT INTO public.items VALUES (11, 'dup', 1)")
    assert res["results"][0]["error"]["sqlstate"] == "23505" and res["tx_status"] == "in_transaction"
    r = run(mgr, sid, "SELECT count(*) FROM public.items WHERE id = 11")["results"][0]
    assert r["rows"] == [["1"]]
    run(mgr, sid, "INSERT INTO public.items VALUES (12, 'y', 1)")
    assert mgr.commit(sid)["tx_status"] == "idle"
    with connect(ws_db) as c:
        assert c.execute("SELECT count(*) FROM public.items WHERE id IN (11, 12)").fetchone() == (2,)
    assert mgr.set_mode(sid, "auto")["mode"] == "auto"


def test_explicit_begin_and_commit_typed_by_the_user(ws_db, mgr):
    sid = mgr.open(server(), ws_db)["session_id"]
    res = run(mgr, sid, "BEGIN; INSERT INTO public.items VALUES (20, 'b', 1); SELECT count(*) FROM public.items;")
    assert res["tx_status"] == "in_transaction" and res["results"][2]["error"] is None
    res = run(mgr, sid, "ROLLBACK")
    assert res["tx_status"] == "idle"
    with connect(ws_db) as c:
        assert c.execute("SELECT count(*) FROM public.items WHERE id = 20").fetchone() == (0,)


def test_idle_sessions_are_closed_and_the_limit_holds(ws_db):
    m = ConsoleManager(idle_timeout_s=0, max_sessions=1)
    try:
        sid = m.open(server(), ws_db)["session_id"]
        time.sleep(0.01)
        assert m.reap() == 1 and m.count() == 0
        with pytest.raises(ConsoleError) as e:
            m.execute(sid, "select 1", 10)
        assert e.value.code == "session_lost"
        m2 = ConsoleManager(max_sessions=1)
        m2.open(server(), ws_db)
        with pytest.raises(ConsoleError) as e:
            m2.open(server(), ws_db)
        assert e.value.code == "limit"
        m2.close_all()
    finally:
        m.close_all()


def test_api_round_trip_completions_and_no_password(ws_db, tmp_path):
    st = Store(tmp_path / "data")
    s = pg_settings()
    secret = s["password"] or "unused-secret"
    rec = st.save_server({"name": "L", "host": s["host"], "port": s["port"], "user": s["user"], "password": secret})
    consoles = ConsoleManager()
    client = create_app(st, JobManager(st), consoles).test_client()
    bodies = []
    try:
        r = client.post(f"/api/servers/{rec['id']}/databases/{ws_db}/console", headers=H)
        assert r.status_code == 200
        sid = r.get_json()["session_id"]
        bodies.append(r.data)
        r = client.post(f"/api/console/{sid}/execute", json={"sql": "SELECT name FROM items ORDER BY id", "max_rows": 100}, headers=H)
        bodies.append(r.data)
        assert r.get_json()["results"][0]["rows"] == [["a"], ["b"]]
        r = client.post(f"/api/console/{sid}/mode", json={"mode": "manual"}, headers=H)
        assert r.get_json()["mode"] == "manual"
        assert client.post(f"/api/console/{sid}/commit", headers=H).status_code == 200
        assert client.post(f"/api/console/{sid}/rollback", headers=H).status_code == 200
        assert client.post(f"/api/console/{sid}/cancel", headers=H).get_json() == {"cancelled": False}
        r = client.get(f"/api/servers/{rec['id']}/databases/{ws_db}/completions", headers=H)
        bodies.append(r.data)
        comp = r.get_json()
        assert {"schema": "public", "name": "items", "kind": "table", "system": False} in comp["relations"]
        assert any(x["name"] == "cheap" and x["kind"] == "view" for x in comp["relations"])
        assert any(f["name"] == "twice" and f["args"] == "x integer" for f in comp["functions"])
        assert "public" in comp["search_path"] and "now" in comp["builtin_functions"]
        r = client.get(f"/api/servers/{rec['id']}/databases/{ws_db}/columns?schema=public&table=items", headers=H)
        cols = r.get_json()
        assert [(c["name"], c["type"], c["pk"]) for c in cols] == [("id", "integer", True), ("name", "text", False),
                                                                   ("price", "numeric(10,2)", False)]
        r = client.post(f"/api/servers/{rec['id']}/databases/nope_{PREFIX}x/console", headers=H)
        assert r.status_code == 404
        assert client.delete(f"/api/console/{sid}", headers=H).get_json() == {"closed": True}
        for b in bodies:
            assert secret.encode() not in b and b"password\"" not in b
        json.loads(bodies[0])
    finally:
        consoles.close_all()


def test_dropping_a_database_closes_its_console_sessions(ws_db, mgr):
    mgr.open(server(), ws_db)
    assert mgr.close_for("srv1", ws_db) == 1 and mgr.count() == 0
