"""Step 8d: EXPLAIN / EXPLAIN ANALYZE in the SQL workspace (ARCHITECTURE 14.8, decision 009)."""
import json

import pytest

from app import create_app
from conftest import PREFIX, connect, drop_database, pg_settings
from engine.console import ConsoleError, ConsoleManager
from jobs import JobManager
from store import Store

EXP_DB = f"{PREFIX}ws_exp"
H = {"X-PGBS": "1"}


def server() -> dict:
    s = pg_settings()
    return {"id": "srv1", "host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


# ---------------------------------------------------------------- no database

def test_explain_refuses_bad_input_before_running():
    m = ConsoleManager()
    for text in ("", "select 1; select 2", "explain select 1", "-- only a comment\n"):
        with pytest.raises(ConsoleError):
            m.explain("nope", text)
    with pytest.raises(ConsoleError):
        m.explain("nope", "select 1", timeout_s=-1)
    with pytest.raises(ConsoleError) as e:
        m.explain("nope", "select 1")
    assert e.value.code == "session_lost"


# ---------------------------------------------------------------- PostgreSQL

@pytest.fixture
def exp_db(pg_admin):
    drop_database(pg_admin, EXP_DB)
    pg_admin.execute(f'CREATE DATABASE "{EXP_DB}" TEMPLATE template0')
    with connect(EXP_DB) as c:
        c.execute("CREATE TABLE t (id int PRIMARY KEY, v text);"
                  "INSERT INTO t SELECT g, 'v' || g FROM generate_series(1, 1000) g; ANALYZE t;")
    yield EXP_DB
    drop_database(pg_admin, EXP_DB)


@pytest.fixture
def mgr():
    m = ConsoleManager()
    yield m
    m.close_all()


def count():
    with connect(EXP_DB) as c:
        return c.execute("SELECT count(*) FROM t").fetchone()[0]


def test_explain_plan_does_not_run(exp_db, mgr):
    sid = mgr.open(server(), exp_db)["session_id"]
    r = mgr.explain(sid, "DELETE FROM t WHERE id > 10;")
    assert r["error"] is None and not r["analyze"] and not r["rolled_back"]
    assert r["plan"]["Plan"]["Node Type"] == "ModifyTable"
    assert "Actual Rows" not in r["plan"]["Plan"]
    assert count() == 1000
    r = mgr.explain(sid, "SELECT * FROM t WHERE id = 5")
    assert r["plan"]["Plan"]["Node Type"] in ("Index Scan", "Index Only Scan", "Bitmap Heap Scan")
    assert r["statement"] == "SELECT * FROM t WHERE id = 5"


def test_explain_analyze_runs_and_rolls_back(exp_db, mgr):
    sid = mgr.open(server(), exp_db)["session_id"]
    r = mgr.explain(sid, "DELETE FROM t WHERE id > 10", analyze=True)
    assert r["error"] is None and r["analyze"] and r["rolled_back"] and r["tx_status"] == "idle"
    plan = r["plan"]
    assert "Execution Time" in plan and "Planning Time" in plan
    assert plan["Plan"]["Plans"][0]["Actual Rows"] == 990
    assert count() == 1000, "EXPLAIN ANALYZE must not keep the delete"
    sel = mgr.explain(sid, "SELECT count(*) FROM t", analyze=True)["plan"]["Plan"]
    assert sel["Actual Rows"] == 1 and "Shared Hit Blocks" in sel


def test_explain_analyze_in_manual_mode_keeps_the_transaction(exp_db, mgr):
    sid = mgr.open(server(), exp_db)["session_id"]
    mgr.set_mode(sid, "manual")
    mgr.execute(sid, "INSERT INTO t VALUES (5000, 'mine')")
    r = mgr.explain(sid, "DELETE FROM t", analyze=True)
    assert r["error"] is None and r["tx_status"] == "in_transaction"
    rows = mgr.execute(sid, "SELECT count(*) FROM t")["results"][0]["rows"]
    assert rows == [["1001"]], "only the explained statement is undone, not the open transaction"
    mgr.rollback(sid)
    assert count() == 1000


def test_explain_errors_are_reported(exp_db, mgr):
    sid = mgr.open(server(), exp_db)["session_id"]
    r = mgr.explain(sid, "SELECT nope FROM t", analyze=True)
    assert r["plan"] is None and r["error"]["sqlstate"] == "42703" and r["tx_status"] == "idle"
    r = mgr.explain(sid, "SELECT 1\nFROM t\nWHERE nope = 1")
    assert r["error"]["line"] == 3 and r["error"]["position"] == len("SELECT 1\nFROM t\nWHERE ") + 1   # prefix taken off
    r = mgr.explain(sid, "CREATE INDEX i ON t (v)")   # EXPLAIN does not support it
    assert r["error"]["sqlstate"] == "42601"
    assert mgr.execute(sid, "SELECT 1")["results"][0]["rows"] == [["1"]]   # session still fine


def test_explain_analyze_respects_timeout_and_cancel_state(exp_db, mgr):
    sid = mgr.open(server(), exp_db)["session_id"]
    r = mgr.explain(sid, "SELECT pg_sleep(5)", analyze=True, timeout_s=0.5)
    assert r["error"]["sqlstate"] == "57014" and r["tx_status"] == "idle"


def test_api_explain(exp_db, tmp_path):
    st = Store(tmp_path / "data")
    s = pg_settings()
    secret = s["password"] or "unused-secret"
    rec = st.save_server({"name": "L", "host": s["host"], "port": s["port"], "user": s["user"], "password": secret})
    consoles = ConsoleManager()
    client = create_app(st, JobManager(st), consoles).test_client()
    try:
        session = client.post(f"/api/servers/{rec['id']}/databases/{exp_db}/console", headers=H).get_json()["session_id"]
        r = client.post(f"/api/console/{session}/explain", json={"sql": "SELECT * FROM t", "analyze": True}, headers=H)
        assert r.status_code == 200 and r.get_json()["plan"]["Plan"]["Actual Rows"] == 1000
        assert secret.encode() not in r.data
        json.loads(r.data)
        r = client.post(f"/api/console/{session}/explain", json={"sql": "select 1; select 2"}, headers=H)
        assert r.status_code == 400
        r = client.post(f"/api/console/{session}/explain", json={"sql": "select 1", "analyze": "yes"}, headers=H)
        assert r.status_code == 400
    finally:
        consoles.close_all()
