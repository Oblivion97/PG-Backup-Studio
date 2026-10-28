"""Step 8b: explorer tree, object detail, DDL and the table viewer (ARCHITECTURE 14, decision 009)."""
import json

import pytest

from app import create_app
from conftest import PREFIX, connect, drop_database, pg_settings
from engine import explore
from engine.console import ConsoleError, ConsoleManager, _strip_literals
from engine.util import EngineError
from jobs import JobManager
from store import Store

EX_DB = f"{PREFIX}ws_ex"
COPY_DB = f"{PREFIX}ws_ex_copy"
H = {"X-PGBS": "1"}
WEIRD = 'we"ird T'
WEIRD_Q = '"we""ird T"'

FIXTURE = f'''
CREATE SCHEMA app;
CREATE TYPE app.mood AS ENUM ('ok', 'sad');
CREATE DOMAIN app.posint AS int CHECK (VALUE > 0);
CREATE TABLE app.orders (
    id serial PRIMARY KEY,
    customer text NOT NULL DEFAULT 'anon',
    qty app.posint,
    mood app.mood,
    note text
);
COMMENT ON TABLE app.orders IS 'All orders';
COMMENT ON COLUMN app.orders.note IS 'free text';
CREATE INDEX orders_customer ON app.orders (customer);
ALTER TABLE app.orders ADD CONSTRAINT qty_small CHECK (qty < 1000);
CREATE FUNCTION app.touch() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$;
CREATE TRIGGER orders_touch BEFORE UPDATE ON app.orders FOR EACH ROW EXECUTE FUNCTION app.touch();
CREATE FUNCTION app.twice(x int) RETURNS int LANGUAGE sql AS 'SELECT x * 2';
CREATE FUNCTION app.twice(x text) RETURNS text LANGUAGE sql AS 'SELECT x || x';
CREATE PROCEDURE app.noop() LANGUAGE sql AS 'SELECT 1';
CREATE SEQUENCE app.free_seq START 5;
CREATE VIEW app.big AS SELECT * FROM app.orders WHERE qty > 10;
CREATE MATERIALIZED VIEW app.totals AS SELECT customer, sum(qty) AS qty FROM app.orders GROUP BY customer;
CREATE TABLE app.parted (id int, d date) PARTITION BY RANGE (d);
CREATE TABLE app.parted_2026 PARTITION OF app.parted FOR VALUES FROM ('2026-01-01') TO ('2027-01-01');
CREATE TABLE app.{WEIRD_Q} ("Col ""x""" int PRIMARY KEY, "order" text);
INSERT INTO app.orders (customer, qty, mood) SELECT 'c' || (g % 7), g, 'ok' FROM generate_series(1, 250) g;
INSERT INTO app.{WEIRD_Q} VALUES (1, 'a'), (2, 'b;c'), (3, NULL);
'''


def server() -> dict:
    s = pg_settings()
    return {"id": "srv1", "host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


# ---------------------------------------------------------------- no database

def test_filter_semicolons_inside_literals_are_allowed():
    assert ";" not in _strip_literals("name = 'a;b' AND \"x;y\" = 1 -- c;\n")
    assert ";" not in _strip_literals("body = $q$a;b$q$ /* ; */")
    assert ";" in _strip_literals("1 = 1; DROP TABLE x")


def test_table_data_refuses_bad_parameters():
    m = ConsoleManager()
    for kw in ({"schema": ""}, {"where": "1=1; select 1"}, {"limit": 0}, {"limit": 10001}, {"offset": -1},
               {"order_by": [{"desc": True}]}, {"order_by": "id"}):
        args = {"schema": "public", "table": "t", **kw}
        with pytest.raises(ConsoleError):
            m.table_data("nope", args.pop("schema"), args.pop("table"), **args)


def test_explorer_script_is_served(tmp_path):
    st = Store(tmp_path / "data")
    client = create_app(st, JobManager(st), ConsoleManager()).test_client()
    r = client.get("/ws/explorer.js")
    assert r.status_code == 200 and b"function wxPanel" in r.data


def test_ddl_refuses_unknown_kinds_and_missing_oid():
    with pytest.raises(EngineError, match="Unknown object kind"):
        explore._target(None, "public", "index", "x", None)
    with pytest.raises(EngineError, match="'oid' is required"):
        explore._target(None, "public", "function", "x", None)


# ---------------------------------------------------------------- PostgreSQL

@pytest.fixture
def ex_db(pg_admin):
    drop_database(pg_admin, EX_DB)
    pg_admin.execute(f'CREATE DATABASE "{EX_DB}" TEMPLATE template0')
    with connect(EX_DB) as c:
        c.execute(FIXTURE)
    yield EX_DB
    drop_database(pg_admin, EX_DB)
    drop_database(pg_admin, COPY_DB)


@pytest.fixture
def conn(ex_db):
    with connect(ex_db) as c:
        yield c


@pytest.fixture
def mgr():
    m = ConsoleManager()
    yield m
    m.close_all()


def test_schemas_list_user_and_system(conn):
    rows = explore.schemas(conn)
    names = [r["name"] for r in rows]
    assert {"app", "public", "pg_catalog", "information_schema"} <= set(names)
    assert not any(n.startswith("pg_toast") for n in names)
    assert [r["system"] for r in rows] == sorted(r["system"] for r in rows)   # user schemas first


def test_objects_are_grouped(conn):
    o = explore.objects(conn, "app")
    assert [t["name"] for t in o["tables"]] == sorted(["orders", "parted", WEIRD])   # partitions left out
    assert next(t for t in o["tables"] if t["name"] == "parted")["partitioned"]
    orders = next(t for t in o["tables"] if t["name"] == "orders")
    assert orders["comment"] == "All orders" and orders["size"] > 0
    assert [v["name"] for v in o["views"]] == ["big"]
    assert [v["name"] for v in o["matviews"]] == ["totals"]
    assert {s["name"] for s in o["sequences"]} == {"free_seq", "orders_id_seq"}
    assert sorted(f["args"] for f in o["functions"] if f["name"] == "twice") == ["x integer", "x text"]
    assert [p["name"] for p in o["procedures"]] == ["noop"]
    assert {(t["name"], t["kind"]) for t in o["types"]} == {("mood", "enum"), ("posint", "domain")}
    assert o["truncated"] == []
    with pytest.raises(EngineError, match="does not exist"):
        explore.objects(conn, "nope")


def test_object_detail(conn):
    d = explore.object_detail(conn, "app", "orders")
    assert d["kind"] == "table" and d["comment"] == "All orders"
    cols = {c["name"]: c for c in d["columns"]}
    assert list(cols) == ["id", "customer", "qty", "mood", "note"]
    assert cols["id"]["pk"] and not cols["id"]["nullable"] and "nextval" in cols["id"]["default"]
    assert cols["customer"]["default"] == "'anon'::text" and cols["note"]["comment"] == "free text"
    assert cols["qty"]["type"] == "app.posint"
    assert [(k["name"], k["kind"]) for k in d["constraints"]] == [("orders_pkey", "primary key"), ("qty_small", "check")]
    assert [i["name"] for i in d["indexes"]] == ["orders_pkey", "orders_customer"] and d["indexes"][0]["primary"]
    assert [t["name"] for t in d["triggers"]] == ["orders_touch"]
    w = explore.object_detail(conn, "app", WEIRD)
    assert [c["name"] for c in w["columns"]] == ['Col "x"', "order"] and w["columns"][0]["pk"]
    assert explore.object_detail(conn, "app", "big")["kind"] == "view"


def test_table_ddl_has_everything_and_round_trips(conn, pg_admin):
    text = explore.ddl(conn, "app", "table", "orders")["ddl"]
    for piece in ("CREATE SEQUENCE app.orders_id_seq", "CREATE TABLE app.orders", "DEFAULT 'anon'::text",
                  "COMMENT ON TABLE app.orders IS 'All orders'", 'COMMENT ON COLUMN app.orders."note"',
                  'ADD CONSTRAINT "orders_pkey" PRIMARY KEY (id)', 'ADD CONSTRAINT "qty_small" CHECK',
                  "CREATE INDEX orders_customer", "CREATE TRIGGER orders_touch", "OWNED BY app.orders.id",
                  "OWNER TO"):
        assert piece in text, piece
    assert "free_seq" not in text and "CREATE VIEW" not in text
    assert text.index("CREATE SEQUENCE") < text.index("CREATE TABLE") < text.index("OWNED BY")
    # it runs in a database that has the schema, types and trigger function
    drop_database(pg_admin, COPY_DB)
    pg_admin.execute(f'CREATE DATABASE "{COPY_DB}" TEMPLATE template0')
    with connect(COPY_DB) as c:
        c.execute("CREATE SCHEMA app; CREATE TYPE app.mood AS ENUM ('ok', 'sad');"
                  "CREATE DOMAIN app.posint AS int CHECK (VALUE > 0);"
                  "CREATE FUNCTION app.touch() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$;")
        c.execute(text)
        copy = explore.object_detail(c, "app", "orders")
    orig = explore.object_detail(conn, "app", "orders")
    for key in ("columns", "constraints", "triggers"):
        assert copy[key] == orig[key], key
    assert [i["def"] for i in copy["indexes"]] == [i["def"] for i in orig["indexes"]]


def test_ddl_of_other_kinds(conn):
    oid = next(f["oid"] for f in explore.objects(conn, "app")["functions"] if f["args"] == "x text")
    f = explore.ddl(conn, "app", "function", oid=oid)
    assert f["name"] == "app.twice(text)" and "x || x" in f["ddl"] and "x * 2" not in f["ddl"]
    assert "CREATE VIEW app.big AS" in explore.ddl(conn, "app", "view", "big")["ddl"]
    assert "CREATE MATERIALIZED VIEW app.totals" in explore.ddl(conn, "app", "matview", "totals")["ddl"]
    seq = explore.ddl(conn, "app", "sequence", "free_seq")["ddl"]
    assert "CREATE SEQUENCE app.free_seq" in seq and "START WITH 5" in seq
    assert "CREATE TYPE app.mood AS ENUM ('ok', 'sad')" in explore.ddl(conn, "app", "type", "mood")["ddl"]
    dom = explore.ddl(conn, "app", "type", "posint")["ddl"]
    assert "CREATE DOMAIN app.posint AS integer" in dom and "CHECK" in dom
    weird = explore.ddl(conn, "app", "table", WEIRD)["ddl"]
    assert 'CREATE TABLE app."we""ird T"' in weird and '"Col ""x""" integer' in weird
    # system schema: falls back to the server's own definition
    sysview = explore.ddl(conn, "pg_catalog", "view", "pg_tables")["ddl"]
    assert sysview.startswith("CREATE VIEW pg_tables AS") or sysview.startswith("CREATE VIEW pg_catalog.pg_tables AS")
    assert "No DDL" in explore.ddl(conn, "pg_catalog", "table", "pg_class")["ddl"]
    with pytest.raises(EngineError, match="does not exist"):
        explore.ddl(conn, "app", "table", "nope")


def test_table_data_pages_filters_and_sorts(ex_db, mgr):
    sid = mgr.open(server(), ex_db)["session_id"]
    page = mgr.table_data(sid, "app", "orders", limit=100)
    assert page["result"]["row_count"] == 100 and page["has_more"] and page["count"] is None
    assert page["result"]["columns"][0]["name"] == "id"
    last = mgr.table_data(sid, "app", "orders", limit=100, offset=200, order_by=[{"column": "id"}])
    assert last["result"]["row_count"] == 50 and not last["has_more"]
    assert last["result"]["rows"][0][0] == "201"
    desc = mgr.table_data(sid, "app", "orders", limit=3, order_by=[{"column": "customer", "desc": True}, {"column": "id"}])
    assert [r[1] for r in desc["result"]["rows"]] == ["c6", "c6", "c6"] and desc["result"]["rows"][0][0] == "6"
    f = mgr.table_data(sid, "app", "orders", where="qty <= 5 -- trailing comment", count=True, limit=2)
    assert f["count"] == 5 and f["result"]["row_count"] == 2 and f["has_more"]
    assert f["sql"] == 'SELECT * FROM "app"."orders"\nWHERE (\nqty <= 5 -- trailing comment\n)\nLIMIT 3 OFFSET 0'


def test_table_data_quotes_hostile_names(ex_db, mgr):
    sid = mgr.open(server(), ex_db)["session_id"]
    r = mgr.table_data(sid, "app", WEIRD, order_by=[{"column": 'Col "x"', "desc": True}], where="\"order\" = 'b;c'", count=True)
    assert r["result"]["error"] is None, r["result"]["error"]
    assert r["result"]["rows"] == [["2", "b;c"]] and r["count"] == 1
    bad = mgr.table_data(sid, "app", "orders", where="no_such_column = 1")
    assert bad["result"]["error"]["sqlstate"] == "42703" and bad["count"] is None
    missing = mgr.table_data(sid, "app", "nope")
    assert missing["result"]["error"]["sqlstate"] == "42P01"
    injected = mgr.table_data(sid, "app", "orders", order_by=[{"column": 'id"; DROP TABLE app.orders; --'}])
    assert injected["result"]["error"]["sqlstate"] == "42703"
    assert mgr.table_data(sid, "app", "orders", limit=1)["result"]["row_count"] == 1   # still there


def test_table_data_sees_the_open_transaction(ex_db, mgr):
    sid = mgr.open(server(), ex_db)["session_id"]
    mgr.set_mode(sid, "manual")
    mgr.execute(sid, "DELETE FROM app.orders WHERE id > 10")
    r = mgr.table_data(sid, "app", "orders", count=True)
    assert r["count"] == 10 and r["tx_status"] == "in_transaction"
    mgr.rollback(sid)
    assert mgr.table_data(sid, "app", "orders", count=True)["count"] == 250


def test_api_explorer_object_ddl_table_data(ex_db, tmp_path):
    st = Store(tmp_path / "data")
    s = pg_settings()
    secret = s["password"] or "unused-secret"
    rec = st.save_server({"name": "L", "host": s["host"], "port": s["port"], "user": s["user"], "password": secret})
    consoles = ConsoleManager()
    client = create_app(st, JobManager(st), consoles).test_client()
    base = f"/api/servers/{rec['id']}/databases/{ex_db}"
    bodies = []
    try:
        r = client.get(f"{base}/explorer", headers=H)
        bodies.append(r.data)
        assert any(x["name"] == "app" for x in r.get_json()["schemas"])
        r = client.get(f"{base}/explorer?schema=app", headers=H)
        bodies.append(r.data)
        fn = next(f for f in r.get_json()["functions"] if f["args"] == "x integer")
        r = client.get(f"{base}/object", query_string={"schema": "app", "name": WEIRD}, headers=H)
        bodies.append(r.data)
        assert r.get_json()["columns"][1]["name"] == "order"
        r = client.get(f"{base}/ddl?schema=app&kind=function&oid={fn['oid']}", headers=H)
        bodies.append(r.data)
        assert "x * 2" in r.get_json()["ddl"]
        assert client.get(f"{base}/ddl?schema=app&kind=function&oid=abc", headers=H).status_code == 400
        assert client.get(f"{base}/ddl?schema=app&kind=table&name=nope", headers=H).status_code == 400
        assert client.get(f"{base}/object?schema=app", headers=H).status_code == 400
        sid = client.post(f"{base}/console", headers=H).get_json()["session_id"]
        r = client.post(f"/api/console/{sid}/table-data", headers=H, json={
            "schema": "app", "table": "orders", "where": "qty < 4", "order_by": [{"column": "id", "desc": True}],
            "limit": 2, "offset": 0, "count": True})
        bodies.append(r.data)
        body = r.get_json()
        assert r.status_code == 200 and body["count"] == 3 and body["has_more"]
        assert [row[0] for row in body["result"]["rows"]] == ["3", "2"]
        r = client.post(f"/api/console/{sid}/table-data", headers=H, json={"schema": "app", "table": "orders", "limit": 0})
        assert r.status_code == 400
        r = client.post("/api/console/nope/table-data", headers=H, json={"schema": "app", "table": "orders"})
        assert r.status_code == 410
    finally:
        consoles.close_all()
    for b in bodies:
        assert secret.encode() not in b
        json.loads(b)
