"""Step 8c: editable grid — which results can be edited, and apply-edits (ARCHITECTURE 14.7, decision 009)."""
import json

import pytest

from app import create_app
from conftest import PREFIX, connect, drop_database, pg_settings
from engine.console import ConsoleError, ConsoleManager
from jobs import JobManager
from store import Store

ED_DB = f"{PREFIX}ws_ed"
H = {"X-PGBS": "1"}

FIXTURE = '''
CREATE SCHEMA app;
CREATE TYPE app.mood AS ENUM ('ok', 'sad');
CREATE TABLE app.t (
    id int PRIMARY KEY,
    name text DEFAULT 'dflt',
    price numeric(10,2),
    doc jsonb,
    raw bytea,
    tags text[],
    mood app.mood,
    at timestamptz,
    total numeric GENERATED ALWAYS AS (price * 2) STORED
);
INSERT INTO app.t (id, name, price) VALUES (1, 'a', 1.50), (2, 'b', 2.00), (3, 'c', NULL);
CREATE TABLE app.pair (a int, b text, v text, PRIMARY KEY (a, b));
INSERT INTO app.pair VALUES (1, 'x', 'one'), (1, 'y', 'two');
CREATE TABLE app.nokey (v text);
INSERT INTO app.nokey VALUES ('q');
CREATE VIEW app.tv AS SELECT * FROM app.t;
CREATE TABLE app."we""ird" ("I d" int PRIMARY KEY, "va""l" text);
INSERT INTO app."we""ird" VALUES (1, 'z');
'''


def server() -> dict:
    s = pg_settings()
    return {"id": "srv1", "host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


# ---------------------------------------------------------------- no database

def test_apply_edits_refuses_bad_requests_before_connecting():
    m = ConsoleManager()
    for schema, table, changes in (("", "t", [{}]), ("app", "t", []), ("app", "t", "x"), ("app", "t", [{}] * 5001)):
        with pytest.raises(ConsoleError):
            m.apply_edits("nope", schema, table, changes)
    with pytest.raises(ConsoleError) as e:
        m.apply_edits("nope", "app", "t", [{"op": "delete", "key": {"id": "1"}}])
    assert e.value.code == "session_lost"


# ---------------------------------------------------------------- PostgreSQL

@pytest.fixture
def ed_db(pg_admin):
    drop_database(pg_admin, ED_DB)
    pg_admin.execute(f'CREATE DATABASE "{ED_DB}" TEMPLATE template0')
    with connect(ED_DB) as c:
        c.execute(FIXTURE)
    yield ED_DB
    drop_database(pg_admin, ED_DB)


@pytest.fixture
def mgr():
    m = ConsoleManager()
    yield m
    m.close_all()


@pytest.fixture
def sid(ed_db, mgr):
    return mgr.open(server(), ed_db)["session_id"]


def first(mgr, sid, sql_text):
    r = mgr.execute(sid, sql_text)["results"][0]
    assert r["error"] is None, r["error"]
    return r


def rows(sql_text):
    with connect(ED_DB) as c:
        return c.execute(sql_text).fetchall()


def test_editable_results(mgr, sid):
    r = first(mgr, sid, "SELECT name AS n, id FROM app.t ORDER BY id")
    assert r["editable"] == {"schema": "app", "table": "t", "columns": ["name", "id"], "key": [1]}
    assert r["readonly"] is None
    assert first(mgr, sid, "SELECT b, v, a FROM app.pair")["editable"]["key"] == [2, 0]
    cases = {
        "SELECT name FROM app.t": "primary key column(s) id",
        "SELECT id, name, 1 AS one FROM app.t": "straight from one table",
        "SELECT t.id, p.v FROM app.t t JOIN app.pair p ON p.a = t.id": "straight from one table",
        "SELECT * FROM app.nokey": "no primary key",
        "SELECT * FROM app.tv": "Only tables",
        "SELECT id, id FROM app.t": "appears twice",
    }
    for q, reason in cases.items():
        res = first(mgr, sid, q)
        assert res["editable"] is None and reason in res["readonly"], (q, res["readonly"])
    tv = mgr.table_data(sid, "app", "we\"ird")["result"]
    assert tv["editable"] == {"schema": "app", "table": "we\"ird", "columns": ["I d", "va\"l"], "key": [0]}


def test_update_insert_delete_with_types(mgr, sid):
    changes = [
        {"op": "delete", "key": {"id": "3"}},
        {"op": "update", "key": {"id": "1"}, "set": {"name": "it's", "price": "9.99", "doc": '{"a": [1, 2]}',
                                                    "raw": "\\x00ff", "tags": '{x,"y z"}', "mood": "sad",
                                                    "at": "2026-10-28 10:00:00+00"}},
        {"op": "update", "key": {"id": "2"}, "set": {"name": None, "price": {"default": True}}},
        {"op": "insert", "values": {"id": "10", "price": "1"}},
        {"op": "insert", "values": {"id": "11", "name": {"default": True}}},
    ]
    res = mgr.apply_edits(sid, "app", "t", changes)
    assert res["error"] is None and res["applied"] == 5 and res["tx_status"] == "idle"
    assert res["statements"][0] == 'DELETE FROM "app"."t" WHERE "id" = \'3\'::integer'
    got = rows("SELECT id, name, price::text, doc::text, encode(raw, 'hex'), tags, mood::text, total::text"
               " FROM app.t ORDER BY id")
    assert got == [(1, "it's", "9.99", '{"a": [1, 2]}', "00ff", ["x", "y z"], "sad", "19.98"),
                   (2, None, None, None, None, None, None, None),
                   (10, "dflt", "1.00", None, None, None, None, "2.00"),
                   (11, "dflt", None, None, None, None, None, None)]
    assert rows("SELECT at = '2026-10-28 10:00:00+00' FROM app.t WHERE id = 1") == [(True,)]


def test_preview_is_what_runs_and_changes_nothing(mgr, sid):
    changes = [{"op": "update", "key": {"a": "1", "b": "y"}, "set": {"v": "TWO"}}]
    prev = mgr.apply_edits(sid, "app", "pair", changes, preview=True)
    assert prev["applied"] == 0 and prev["statements"] == [
        'UPDATE "app"."pair" SET "v" = \'TWO\'::text WHERE "a" = \'1\'::integer AND "b" = \'y\'::text']
    assert rows("SELECT v FROM app.pair ORDER BY b") == [("one",), ("two",)]
    assert mgr.apply_edits(sid, "app", "pair", changes)["statements"] == prev["statements"]
    assert rows("SELECT v FROM app.pair ORDER BY b") == [("one",), ("TWO",)]


def test_conflict_undoes_the_whole_batch(mgr, sid):
    res = mgr.apply_edits(sid, "app", "t", [
        {"op": "update", "key": {"id": "1"}, "set": {"name": "changed"}},
        {"op": "delete", "key": {"id": "99"}},   # deleted meanwhile
    ])
    assert res["applied"] == 0 and res["error"]["conflict"] and res["error"]["index"] == 1
    assert "no row" in res["error"]["message"] and res["tx_status"] == "idle"
    assert rows("SELECT name FROM app.t WHERE id = 1") == [("a",)]


def test_error_undoes_the_whole_batch(mgr, sid):
    res = mgr.apply_edits(sid, "app", "t", [
        {"op": "insert", "values": {"id": "20"}},
        {"op": "update", "key": {"id": "2"}, "set": {"price": "not a number"}},
    ])
    assert res["applied"] == 0 and not res["error"]["conflict"]
    assert res["error"]["index"] == 1 and res["error"]["sqlstate"] == "22P02"
    assert rows("SELECT count(*) FROM app.t WHERE id = 20") == [(0,)]
    dup = mgr.apply_edits(sid, "app", "t", [{"op": "insert", "values": {"id": "1"}}])
    assert dup["error"]["sqlstate"] == "23505"


def test_manual_mode_keeps_the_transaction_open(mgr, sid):
    mgr.set_mode(sid, "manual")
    res = mgr.apply_edits(sid, "app", "t", [{"op": "update", "key": {"id": "1"}, "set": {"name": "tx"}}])
    assert res["error"] is None and res["tx_status"] == "in_transaction"
    assert rows("SELECT name FROM app.t WHERE id = 1") == [("a",)]   # not visible outside yet
    bad = mgr.apply_edits(sid, "app", "t", [{"op": "delete", "key": {"id": "42"}}])
    assert bad["error"]["conflict"] and bad["tx_status"] == "in_transaction"   # only the batch is undone
    assert first(mgr, sid, "SELECT name FROM app.t WHERE id = 1")["rows"] == [["tx"]]
    mgr.rollback(sid)
    assert rows("SELECT name FROM app.t WHERE id = 1") == [("a",)]


def test_names_and_keys_come_from_the_catalog(mgr, sid):
    res = mgr.apply_edits(sid, "app", 'we"ird', [{"op": "update", "key": {"I d": "1"}, "set": {'va"l': "ok'; DROP TABLE app.t; --"}}])
    assert res["error"] is None
    assert res["statements"] == ['UPDATE "app"."we""ird" SET "va""l" = \'ok\'\'; DROP TABLE app.t; --\'::text WHERE "I d" = \'1\'::integer']
    assert rows('SELECT "va""l" FROM app."we""ird"') == [("ok'; DROP TABLE app.t; --",)]
    bad = [
        ({"op": "update", "key": {"id": "1"}, "set": {"nope": "x"}}, "no column"),
        ({"op": "update", "key": {"name": "a"}, "set": {"name": "x"}}, "primary key"),
        ({"op": "update", "key": {"id": "1", "name": "a"}, "set": {"name": "x"}}, "primary key"),
        ({"op": "update", "key": {"id": "1"}, "set": {}}, "at least one column"),
        ({"op": "update", "key": {"id": "1"}, "set": {"total": "5"}}, "generated"),
        ({"op": "update", "key": {"id": "1"}, "set": {"name": 5}}, "must be text"),
        ({"op": "merge"}, "'op'"),
        ({"op": "insert", "values": {"id": "1\x00"}}, "NUL"),
    ]
    for change, msg in bad:
        with pytest.raises(ConsoleError, match=msg):
            mgr.apply_edits(sid, "app", "t", [change])
    with pytest.raises(ConsoleError, match="no primary key"):
        mgr.apply_edits(sid, "app", "nokey", [{"op": "insert", "values": {"v": "x"}}])
    with pytest.raises(ConsoleError, match="does not exist"):
        mgr.apply_edits(sid, "app", "tv", [{"op": "insert", "values": {"id": "5"}}])


def test_failed_transaction_is_refused(mgr, sid):
    # statements run in savepoints, but typed transaction control does not: a bad RELEASE fails the transaction
    res = mgr.execute(sid, "BEGIN; RELEASE SAVEPOINT nope")
    assert res["tx_status"] == "failed"
    with pytest.raises(ConsoleError, match="roll it back"):
        mgr.apply_edits(sid, "app", "t", [{"op": "delete", "key": {"id": "1"}}])


def test_api_apply_edits(ed_db, tmp_path):
    st = Store(tmp_path / "data")
    s = pg_settings()
    secret = s["password"] or "unused-secret"
    rec = st.save_server({"name": "L", "host": s["host"], "port": s["port"], "user": s["user"], "password": secret})
    consoles = ConsoleManager()
    client = create_app(st, JobManager(st), consoles).test_client()
    try:
        session = client.post(f"/api/servers/{rec['id']}/databases/{ed_db}/console", headers=H).get_json()["session_id"]
        body = {"schema": "app", "table": "t", "changes": [{"op": "update", "key": {"id": "1"}, "set": {"name": "api"}}]}
        r = client.post(f"/api/console/{session}/apply-edits?preview=1", json=body, headers=H)
        assert r.status_code == 200 and r.get_json()["applied"] == 0
        assert rows("SELECT name FROM app.t WHERE id = 1") == [("a",)]
        r = client.post(f"/api/console/{session}/apply-edits", json=body, headers=H)
        assert r.status_code == 200 and r.get_json()["applied"] == 1
        assert secret.encode() not in r.data
        json.loads(r.data)
        assert rows("SELECT name FROM app.t WHERE id = 1") == [("api",)]
        r = client.post(f"/api/console/{session}/apply-edits", json={"schema": "app", "table": "t"}, headers=H)
        assert r.status_code == 400
        r = client.post(f"/api/console/{session}/apply-edits", json={**body, "changes": [{"op": "x"}]}, headers=H)
        assert r.status_code == 400 and r.get_json()["code"] == "invalid"
    finally:
        consoles.close_all()
