"""Developer self-tests for Step 2: pre-check, permissions, locks, cancel, partial backups, speed limit,
GRANT ALL and the profile helpers (ARCHITECTURE 9.11).

Scratch databases and roles are named pgbs_test_dev_* and dropped afterwards.
"""
import os
import re
import secrets
import subprocess
import threading
import time

import pytest

from conftest import connect, drop_database, pg_settings, tool
from engine import catalog
from engine.backup import BackupOptions, PartialInfo, build_plan, run_backup
from engine.precheck import (TableOption, Thresholds, order_index_check, reconcile_table_options, run_precheck)
from engine.restore import run_restore
from engine.util import BackupBlockedError, BackupError, Cancelled, LockTimeoutError

ORDERS_LATEST = {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 1000}


@pytest.fixture(scope="module")
def server():
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


@pytest.fixture(scope="module")
def analyzed(src_db):
    with connect(src_db) as c:
        c.execute("ANALYZE")
    return src_db


@pytest.fixture
def roles(pg_admin):
    """Factory: roles('ro') -> (name, password) for a LOGIN role pgbs_test_dev_ro; dropped afterwards."""
    made = []

    def make(suffix, extra=""):
        name = f"pgbs_test_dev_{suffix}"
        pw = secrets.token_hex(8)
        pg_admin.execute(f'DROP ROLE IF EXISTS "{name}"')
        pg_admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{pw}' {extra}")
        made.append(name)
        return name, pw

    yield make
    for n in made:
        pg_admin.execute(f'DROP ROLE IF EXISTS "{n}"')


@pytest.fixture
def fresh_db(pg_admin):
    made = []

    def make(suffix, script=None):
        name = f"pgbs_test_dev_{suffix}"
        drop_database(pg_admin, name)
        pg_admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
        made.append(name)
        if script:
            with connect(name) as c:
                c.execute(script)
        return name

    yield make
    for n in made:
        drop_database(pg_admin, n)


def out_files(path):
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def schema_lines(db):
    s = pg_settings()
    r = subprocess.run([tool("pg_dump"), "-s", "-h", s["host"], "-p", str(s["port"]), "-U", s["user"], db],
                       capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
    assert r.returncode == 0, r.stderr
    return sorted(ln for ln in r.stdout.splitlines() if ln.strip() and not ln.startswith("--")
                  and not ln.startswith("\\restrict ") and not ln.startswith("\\unrestrict "))


def hold_lock(db, table, mode="ACCESS EXCLUSIVE"):
    holder = connect(db, autocommit=False)
    holder.execute(f"LOCK TABLE {table} IN {mode} MODE")
    return holder


def run_in_thread(fn):
    out = {}

    def go():
        t0 = time.monotonic()
        try:
            out["result"] = fn()
        except BaseException as e:  # noqa: BLE001
            out["error"] = e
        out["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=go)
    th.start()
    return th, out


def app_backends(db):
    with connect("postgres") as c:
        return c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND application_name = "
                         "'PG Backup Studio'", (db,)).fetchone()[0]


# ---------------------------------------------------------------- read-only login and permissions

def test_read_only_login_blocks_then_partial_restores(server, analyzed, roles, fresh_db, tmp_path):
    ro, pw = roles("ro", "IN ROLE pg_read_all_data")
    ro_server = dict(server, user=ro, password=pw)
    pre = run_precheck(ro_server, analyzed)
    assert pre["blocking"] and [(p["kind"], p["qname"]) for p in pre["problems"]] == [("rls", "app.secrets")]
    assert pre["problems"][0]["fixes"] == ["exclude_schema", "schema_only"]
    assert pre["server"]["read_all_data"] and not pre["server"]["bypassrls"]
    with pytest.raises(BackupBlockedError) as ei:
        run_backup(ro_server, analyzed, {"out_dir": str(tmp_path)})
    assert "app.secrets" in str(ei.value) and ei.value.problems[0]["kind"] == "rls"
    assert out_files(tmp_path) == []
    opts = {"out_dir": str(tmp_path), "tables": [{"schema": "app", "table": "secrets", "data": "schema"}]}
    assert not run_precheck(ro_server, analyzed, tables=opts["tables"])["blocking"]
    res = run_backup(ro_server, analyzed, opts)
    assert res["partial"] and res["partial_tables"] == [{"table": "app.secrets", "data": "schema", "order_by": None,
                                                        "limit": None}]
    assert "_partial" in os.path.basename(res["file"]) and res["not_valid_fks"] == []
    db = fresh_db("ro_restore")
    rr = run_restore(server, db, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM app.secrets").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM app.customers").fetchone()[0] == 50


LIMITED = """
CREATE SCHEMA a; CREATE SCHEMA b;
CREATE TABLE a.ok (id int, v text);
CREATE TABLE a.cols (id int, v text);
CREATE TABLE a.nosel (id serial PRIMARY KEY, v text);
CREATE TABLE a.rls (id int, owner_name text);
ALTER TABLE a.rls ENABLE ROW LEVEL SECURITY;
CREATE POLICY p ON a.rls USING (owner_name = current_user);
CREATE SEQUENCE a.free_seq;
CREATE TABLE b.t (id int);
"""


def test_limited_role_problems(server, roles, fresh_db, tmp_path):
    role, pw = roles("limited")
    db = fresh_db("limited", LIMITED + f"""
        GRANT USAGE ON SCHEMA a TO {role};
        GRANT SELECT ON a.ok, a.rls TO {role};
        GRANT SELECT (id, v) ON a.cols TO {role};
    """)
    srv = dict(server, user=role, password=pw)
    got = [(p["kind"], p["qname"]) for p in run_precheck(srv, db)["problems"]]
    assert got == [("schema_usage", "b"), ("table_select", "a.nosel"), ("table_select", "b.t"), ("rls", "a.rls"),
                   ("sequence_select", "a.free_seq"), ("sequence_select", "a.nosel_id_seq")]
    assert run_precheck(srv, db, mode="schema")["problems"] == []
    # a.nosel schema only: its table and its owned sequence leave the read set
    opts = [{"schema": "a", "table": "nosel", "data": "schema"}]
    got = [(p["kind"], p["qname"]) for p in run_precheck(srv, db, schemas=["a"], tables=opts)["problems"]]
    assert got == [("rls", "a.rls"), ("sequence_select", "a.free_seq")]
    with pytest.raises(BackupBlockedError) as ei:
        run_backup(srv, db, {"out_dir": str(tmp_path), "schemas": ["a"], "tables": opts})
    assert len(ei.value.problems) == 2 and out_files(tmp_path) == []
    res = run_backup(srv, db, {"out_dir": str(tmp_path), "mode": "schema"})
    assert not res["partial"] and "_partial" not in res["file"]


def test_skipped_sequence_value_warns(server, roles, fresh_db, tmp_path):
    role, pw = roles("seqskip")
    db = fresh_db("seqskip", "CREATE SCHEMA a; CREATE TABLE a.nosel (id serial PRIMARY KEY);"
                             "CREATE TABLE a.ok (id int);" + f"GRANT USAGE ON SCHEMA a TO {role};"
                             f"GRANT SELECT ON a.ok TO {role};")
    srv = dict(server, user=role, password=pw)
    nosel = [{"schema": "a", "table": "nosel", "data": "schema"}]
    res = run_backup(srv, db, {"out_dir": str(tmp_path), "tables": nosel})
    assert any("a.nosel_id_seq" in w and "not saved" in w for w in res["warnings"])
    text = open(res["file"], encoding="utf-8").read()
    assert "setval('a.nosel_id_seq'" not in text and "a.nosel_id_seq" in text


def test_many_sequences_and_composite_type(server, fresh_db, tmp_path):
    """Regression: with a long sequence list the planner ran has_sequence_privilege on every pg_class row."""
    seqs = "".join(f"CREATE SEQUENCE s{i};" for i in range(120))
    db = fresh_db("manyseq", "CREATE TYPE addr AS (street text);" + seqs)
    assert run_precheck(server, db)["problems"] == []
    assert run_backup(server, db, {"out_dir": str(tmp_path)})["warnings"] == []


# ---------------------------------------------------------------- no writes on the source

def _db_counters(db):
    with connect(db) as c:
        c.execute("SELECT pg_stat_clear_snapshot()")
        return c.execute("SELECT tup_inserted, tup_updated, tup_deleted, temp_files FROM pg_stat_database"
                         " WHERE datname = current_database()").fetchone()


def test_no_writes_on_source(server, analyzed, tmp_path):
    time.sleep(1.2)
    before = _db_counters(analyzed)
    run_precheck(server, analyzed, tables=[ORDERS_LATEST])
    run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": [ORDERS_LATEST]})
    holder = hold_lock(analyzed, "app.edge")
    try:
        with pytest.raises(LockTimeoutError):
            run_backup(server, analyzed, {"out_dir": str(tmp_path / "x"), "lock_timeout_s": 1})
    finally:
        holder.rollback()
        holder.close()
    time.sleep(1.2)
    assert _db_counters(analyzed) == before


# ---------------------------------------------------------------- locks

@pytest.mark.parametrize("schema_only", [False, True])
def test_lock_timeout_names_table_and_leaves_nothing(server, analyzed, tmp_path, schema_only):
    holder = hold_lock(analyzed, '"sales data".orders_2024q1')
    try:
        tables = [{"schema": "sales data", "table": "orders", "data": "schema"}] if schema_only else []
        t0 = time.monotonic()
        with pytest.raises(LockTimeoutError) as ei:
            run_backup(server, analyzed, {"out_dir": str(tmp_path), "lock_timeout_s": 2, "tables": tables})
        elapsed = time.monotonic() - t0
        assert 2 <= elapsed < 4, elapsed
        assert ei.value.relation == '"sales data".orders_2024q1' and "orders_2024q1" in str(ei.value)
        assert out_files(tmp_path) == []
        t0 = time.monotonic()
        pre = run_precheck(server, analyzed)
        assert time.monotonic() - t0 < 5
        orders = next(t for t in pre["tables"] if t["name"] == "orders")
        assert orders["size_exact"] is False and orders["size"] > 0
        assert [(lk["qname"], lk["granted"]) for lk in pre["locks"]] == [('"sales data".orders_2024q1', True)]
    finally:
        holder.rollback()
        holder.close()


def test_truncate_during_backup_times_out(server, fresh_db, tmp_path):
    db = fresh_db("trunc", "CREATE TABLE t (id int); INSERT INTO t SELECT generate_series(1, 1000);"
                           "CREATE TABLE u (id int); INSERT INTO u SELECT generate_series(1, 10);")
    seen = {}

    def progress(event, data):
        if event == "table_start" and "err" not in seen:
            with connect(db) as other:
                other.execute("SET lock_timeout = '1s'")
                try:
                    other.execute("TRUNCATE t")
                    seen["err"] = None
                except Exception as e:  # noqa: BLE001
                    seen["err"] = getattr(e, "sqlstate", repr(e))

    res = run_backup(server, db, {"out_dir": str(tmp_path)}, progress)
    assert seen["err"] == "55P03" and res["rows"] == 1010
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 1000


# ---------------------------------------------------------------- cancel (OBS-1)

def test_cancel_during_lock_wait(server, analyzed, tmp_path):
    holder = hold_lock(analyzed, "app.edge")
    ev = threading.Event()
    try:
        th, out = run_in_thread(lambda: run_backup(server, analyzed, {"out_dir": str(tmp_path), "lock_timeout_s": 0},
                                                   None, ev))
        time.sleep(1.0)
        t0 = time.monotonic()
        ev.set()
        th.join(5)
        stopped = time.monotonic() - t0
    finally:
        holder.rollback()
        holder.close()
        th.join(30)
    assert isinstance(out.get("error"), Cancelled), out
    assert stopped < 2, stopped
    assert out_files(tmp_path) == []
    time.sleep(0.3)
    assert app_backends(analyzed) == 0


def test_cancel_precheck_during_lock_wait(server, analyzed):
    holder = hold_lock(analyzed, "app.edge")
    ev = threading.Event()
    try:
        th, out = run_in_thread(lambda: run_precheck(server, analyzed, lock_wait_s=0, cancel=ev))
        time.sleep(0.8)
        ev.set()
        th.join(5)
    finally:
        holder.rollback()
        holder.close()
        th.join(30)
    # the pre-check never waits on app.edge (estimates for locked tables), so it may finish first
    assert "result" in out or isinstance(out.get("error"), Cancelled), out


def test_restore_cancel_during_long_statement(server, fresh_db, tmp_path):
    db = fresh_db("sleep")
    path = tmp_path / "sleep.sql"
    path.write_bytes(b"SELECT 1;\nSELECT pg_sleep(30);\nCREATE TABLE after_sleep (id int);\n")
    ev = threading.Event()
    th, out = run_in_thread(lambda: run_restore(server, db, str(path), None, None, ev))
    time.sleep(1.0)
    t0 = time.monotonic()
    ev.set()
    th.join(5)
    assert time.monotonic() - t0 < 2
    assert out["result"]["status"] == "cancelled" and out["result"]["error_count"] == 0
    with connect(db) as c:
        assert c.execute("SELECT to_regclass('after_sleep')").fetchone()[0] is None


@pytest.fixture(scope="module")
def big5(pg_admin):
    name = "pgbs_test_dev_big5"
    drop_database(pg_admin, name)
    pg_admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
    with connect(name) as c:
        c.execute("CREATE TABLE big (id int PRIMARY KEY, payload text);"
                  "INSERT INTO big SELECT i, md5(i::text) || md5((i + 1)::text) FROM generate_series(1, 70000) i;")
    yield name
    drop_database(pg_admin, name)


def test_speed_limit(server, big5, tmp_path):
    t0 = time.monotonic()
    fast = run_backup(server, big5, {"out_dir": str(tmp_path), "filename": "fast.sql"})
    fast_s = time.monotonic() - t0
    mib = os.path.getsize(fast["file"]) / 2 ** 20
    assert 4.5 < mib < 6, mib
    t0 = time.monotonic()
    slow = run_backup(server, big5, {"out_dir": str(tmp_path), "filename": "slow.sql", "speed_limit_mb_s": 1})
    slow_s = time.monotonic() - t0
    assert slow_s >= mib - 1.0 and slow_s < mib + 3, (slow_s, mib)
    assert fast_s < 2.5, fast_s
    assert slow["rows"] == fast["rows"] == 70000 and slow["speed_limit_mb_s"] == 1


def test_cancel_during_throttled_copy(server, big5, tmp_path):
    ev = threading.Event()
    th, out = run_in_thread(lambda: run_backup(server, big5, {"out_dir": str(tmp_path), "speed_limit_mb_s": 0.1},
                                               None, ev))
    time.sleep(1.5)
    t0 = time.monotonic()
    ev.set()
    th.join(5)
    assert time.monotonic() - t0 < 1
    assert isinstance(out.get("error"), Cancelled) and out_files(tmp_path) == []


# ---------------------------------------------------------------- partial backups

def test_partial_backup_spec(server, analyzed, fresh_db, tmp_path):
    totals = {}
    opts = {"out_dir": str(tmp_path), "tables": [ORDERS_LATEST, {"schema": "app", "table": "notes", "data": "schema"}]}
    res = run_backup(server, analyzed, opts, lambda e, d: totals.update(d) if e == "totals" else None)
    assert re.fullmatch(r"pgbs_test_src_\d{8}_\d{6}_partial\.sql", os.path.basename(res["file"]))
    assert res["partial"] is True and res["not_valid_fks"] == ['"order lines_order_id_ordered_at_fkey" ON '
                                                               '"sales data"."order lines"']
    assert [t["table"] for t in res["partial_tables"]] == ["app.notes", '"sales data".orders']
    header = open(res["file"], encoding="utf-8").read().split("SET statement_timeout", 1)[0]
    assert "-- PARTIAL BACKUP: some tables do not contain all rows.\n" in header
    assert '--   "sales data".orders: latest 1000 rows by ordered_at (newest first)\n' in header
    assert "--   app.notes: schema only (no data)\n" in header
    with connect(analyzed) as c:
        full_rows = c.execute("SELECT sum(GREATEST(reltuples, 0))::bigint FROM pg_class c JOIN pg_namespace n"
                              " ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname IN ('app', 'audit',"
                              " 'sales data', 'public') AND NOT c.relispartition AND c.oid <> 'app.notes'::regclass"
                              ).fetchone()[0]
        src_ids = sorted(r[0] for r in c.execute('SELECT id FROM "sales data".orders ORDER BY ordered_at DESC'
                                                 ' LIMIT 1000'))
    assert totals["est_rows"] == 1000 + full_rows
    db = fresh_db("partial")
    rr = run_restore(server, db, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    with connect(db) as c:
        assert sorted(r[0] for r in c.execute('SELECT id FROM "sales data".orders')) == src_ids
        assert c.execute("SELECT count(*) FROM app.notes").fetchone()[0] == 0
        fks = dict(c.execute("SELECT conname, convalidated FROM pg_constraint WHERE contype = 'f'"
                             " AND conparentid = 0").fetchall())
    assert fks == {"order lines_order_id_ordered_at_fkey": False, "orders_customer_id_fkey": True,
                   "customers_referrer_id_fkey": True}


def test_latest_on_inheritance_parent_and_query_forms(server, analyzed, fresh_db, tmp_path):
    vehicle = {"schema": "app", "table": "vehicle", "data": "latest", "order_by": "id", "limit": 5}
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": [vehicle], "schemas": ["app", "audit"]})
    db = fresh_db("vehicle")
    rr = run_restore(server, db, res["file"])
    assert rr["error_count"] == 0, rr["errors"]
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM ONLY app.vehicle").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM app.car").fetchone()[0] == 2


def test_nulls_last_only_for_nullable(server, fresh_db, tmp_path):
    db = fresh_db("nulls", "CREATE TABLE n (id int NOT NULL, ts timestamptz);"
                           "INSERT INTO n SELECT i, CASE WHEN i % 4 = 0 THEN NULL ELSE"
                           " timestamptz '2024-01-01' + i * interval '1 hour' END FROM generate_series(1, 40) i;")
    conn = connect(db, autocommit=False)
    try:
        conn.execute("SET search_path = pg_catalog")
        cat = catalog.load_all(conn, None, conn.info.server_version)
        for col, nulls in (("ts", True), ("id", False)):
            opts = {"tables": [{"schema": "public", "table": "n", "data": "latest", "order_by": col, "limit": 35}]}
            spec = next(i.copy for i in build_plan(cat, opts) if i.copy)
            text = spec.source.as_string(conn)
            assert text == (f'COPY (SELECT "id", "ts" FROM ONLY "public"."n" ORDER BY "{col}" DESC'
                            f'{" NULLS LAST" if nulls else ""} LIMIT 35) TO STDOUT')
    finally:
        conn.close()
    res = run_backup(server, db, {"out_dir": str(tmp_path), "tables": [
        {"schema": "public", "table": "n", "data": "latest", "order_by": "ts", "limit": 35}]})
    text = open(res["file"], encoding="utf-8").read()
    data = text.split('COPY public.n ("id", "ts") FROM stdin;\n')[1].split("\\.")[0]
    lines = data.splitlines()
    assert len(lines) == 35 and all(not ln.endswith("\\N") for ln in lines[:30]) and lines[30].endswith("\\N")


def test_dropped_order_column_refused(server, analyzed, tmp_path):
    bad = dict(ORDERS_LATEST, order_by="no_such_col")
    with pytest.raises(BackupError, match="no_such_col"):
        run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": [bad]})
    assert out_files(tmp_path) == []
    pre = run_precheck(server, analyzed, tables=[bad])
    orders = next(t for t in pre["tables"] if t["name"] == "orders")
    assert "no_such_col" in orders["option_error"]


def test_schema_mode_ignores_options_and_data_mode_warns(server, analyzed, tmp_path):
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "mode": "schema", "tables": [ORDERS_LATEST]})
    assert not res["partial"] and "_partial" not in res["file"] and res["warnings"] == []
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "mode": "data", "tables": [ORDERS_LATEST]})
    assert res["partial"] and any("data-only backup is partial" in w for w in res["warnings"])
    text = open(res["file"], encoding="utf-8").read()
    assert 'COPY "sales data".orders ("id", "customer_id", "order", "amount", "ordered_at") FROM stdin;' in text
    assert "orders_2024q1" not in text.split("SET row_security = off;", 1)[1]


def test_unknown_or_partition_option_warns(server, analyzed, tmp_path):
    opts = [{"schema": "sales data", "table": "orders_2024q1", "data": "schema"},
            {"schema": "app", "table": "gone", "data": "schema"}]
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": opts})
    assert not res["partial"] and len([w for w in res["warnings"] if "ignored" in w]) == 2


# ---------------------------------------------------------------- pre-check details

def test_precheck_tables_thresholds_impact(server, analyzed):
    pre = run_precheck(server, analyzed, thresholds={"large_table_mb": 500, "large_table_rows": 1000})
    names = [t["qname"] for t in pre["tables"]]
    assert '"sales data".orders' in names and not any("orders_2024" in n or "orders_default" in n for n in names)
    orders = next(t for t in pre["tables"] if t["name"] == "orders")
    assert orders["partitions"] == 3 and orders["kind"] == "partitioned" and orders["est_rows"] == 5000
    assert orders["large"] and orders["large_reason"] == ["rows"] and orders["default_order_column"] == "ordered_at"
    assert [c["name"] for c in orders["order_columns"]] == ["id", "ordered_at"]
    customers = next(t for t in pre["tables"] if t["qname"] == "app.customers")
    assert not customers["large"] and customers["default_order_column"] == "id"
    assert pre["impact"]["data_bytes"] == pre["impact"]["full_data_bytes"]
    latest = run_precheck(server, analyzed, tables=[ORDERS_LATEST])["impact"]
    assert latest["est_rows"] == pre["impact"]["full_est_rows"] - 4000
    assert latest["data_bytes"] < pre["impact"]["full_data_bytes"]
    assert run_precheck(server, analyzed, mode="schema")["impact"]["data_bytes"] == 0
    assert pre["notes"] and pre["ignored_options"] == []


def test_order_index_check(server, fresh_db, analyzed):
    db = fresh_db("idx", """
        CREATE TABLE a (c timestamptz NOT NULL); CREATE INDEX a_c ON a (c);
        CREATE TABLE b (c timestamptz); CREATE INDEX b_c ON b (c);
        CREATE TABLE d (c timestamptz); CREATE INDEX d_c ON d (c DESC NULLS LAST);
        CREATE TABLE e (c int); INSERT INTO e VALUES (NULL); CREATE INDEX e_c ON e (c);
        ALTER TABLE e ADD CONSTRAINT e_nn NOT NULL c NOT VALID;
        CREATE TABLE f (c int NOT NULL); CREATE INDEX f_c ON f (c DESC NULLS LAST);
        CREATE TABLE g (c int); CREATE INDEX g_c ON g (c) WHERE c > 0;
    """)
    with connect(db) as c:
        check = {t: order_index_check(c, c.execute(f"SELECT '{t}'::regclass::oid").fetchone()[0], "c")
                 for t in "abdefg"}
        assert order_index_check(c, c.execute("SELECT 'a'::regclass::oid").fetchone()[0], "zz")["exists"] is False
    assert check["a"]["usable"] and check["a"]["null_safe"]
    assert check["b"]["indexed"] and not check["b"]["usable"] and "DESC NULLS LAST" in check["b"]["message"]
    assert check["d"]["usable"] and not check["d"]["null_safe"]
    assert not check["e"]["null_safe"] and not check["e"]["usable"]
    assert check["f"]["indexed"] and not check["f"]["usable"]
    assert not check["g"]["indexed"]
    with connect(analyzed) as c:
        oid = c.execute("""SELECT '"sales data".orders'::regclass::oid""").fetchone()[0]
        r = order_index_check(c, oid, "ordered_at")
    assert not r["indexed"] and r["message"].startswith("No index starts with ordered_at")


# ---------------------------------------------------------------- options and profile helpers

@pytest.mark.parametrize("bad", [
    {"tables": [{"schema": "a", "table": "t", "data": "some"}]},
    {"tables": [{"schema": "a", "table": "t", "data": "latest", "order_by": "c", "limit": 0}]},
    {"tables": [{"schema": "a", "table": "t", "data": "latest", "order_by": "c", "limit": True}]},
    {"tables": [{"schema": "a", "table": "t", "data": "latest"}]},
    {"tables": [{"schema": "a", "table": "t", "data": "schema"}, {"schema": "a", "table": "t", "data": "full"}]},
    {"tables": ["x"]},
    {"speed_limit_mb_s": 0.05},
    {"lock_timeout_s": -1},
    {"lock_timeout_s": 3601},
    {"lock_timeout_s": float("nan")},
])
def test_invalid_options(bad):
    with pytest.raises(BackupError):
        BackupOptions.from_value(bad)


def test_options_round_trip_and_profile():
    opts = BackupOptions.from_value({"mode": "full", "schemas": ["app"], "lock_timeout_s": 5, "speed_limit_mb_s": 2.5,
                                     "filename": "x.sql", "tables": [ORDERS_LATEST,
                                                                     {"schema": "app", "table": "t", "data": "full"},
                                                                     {"schema": "app", "table": "secrets",
                                                                      "data": "schema", "limit": 7}]})
    d = opts.to_dict()
    assert d["tables"] == [ORDERS_LATEST, {"schema": "app", "table": "secrets", "data": "schema"}]
    assert BackupOptions.from_value(d).to_dict() == d
    prof = opts.to_profile()
    assert "filename" not in prof["options"] and re.match(r"\d{4}-\d\d-\d\dT", prof["saved_at"])
    assert prof["options"]["lock_timeout_s"] == 5 and prof["options"]["speed_limit_mb_s"] == 2.5
    assert TableOption.from_dict(ORDERS_LATEST).to_dict() == ORDERS_LATEST
    t = Thresholds.from_settings({"large_table_mb": 10, "large_table_rows": 5, "x": 1})
    assert (t.large_mb, t.large_rows) == (10, 5)
    assert Thresholds.from_settings({"large_table_mb": "bad"}) == Thresholds()


def test_reconcile_table_options():
    rels = [
        {"oid": 1, "schema": "s", "name": "p", "qname": "s.p", "kind": "p", "root_oid": 1},
        {"oid": 2, "schema": "s", "name": "p1", "qname": "s.p1", "kind": "r", "root_oid": 1},
        {"oid": 3, "schema": "s", "name": "t", "qname": "s.t", "kind": "r", "root_oid": 3},
        {"oid": 4, "schema": "s", "name": "q", "qname": "s.q", "kind": "S", "root_oid": 4},
    ]
    saved = [{"schema": "s", "table": "p", "data": "latest", "order_by": "id", "limit": 3},
             {"schema": "s", "table": "p1", "data": "schema"},
             {"schema": "s", "table": "gone", "data": "schema"},
             {"schema": "s", "table": "t", "data": "full"},
             {"schema": "s", "table": "p", "data": "schema"},
             {"schema": "s", "table": "t", "data": "bogus"}]
    kept, ignored = reconcile_table_options(rels, saved)
    assert [o.to_dict() for o in kept] == [saved[0]]
    assert ignored == ["s.p1", "s.gone", "s.p", "s.t"]


# ---------------------------------------------------------------- FK rule (real catalog, version patched)

FK_SCHEMA = """
CREATE TABLE p (id int PRIMARY KEY); INSERT INTO p SELECT generate_series(1, 20);
CREATE TABLE c1 (id int PRIMARY KEY, p_id int REFERENCES p); INSERT INTO c1 SELECT i, i FROM generate_series(1, 20) i;
CREATE TABLE c2 (id int, p_id int); ALTER TABLE c2 ADD CONSTRAINT c2_nv FOREIGN KEY (p_id) REFERENCES p NOT VALID;
CREATE TABLE c3 (id int, p_id int, CONSTRAINT c3_ne FOREIGN KEY (p_id) REFERENCES p NOT ENFORCED);
CREATE TABLE s (id int PRIMARY KEY, parent int REFERENCES s);
INSERT INTO s SELECT i, NULLIF(i - 1, 0) FROM generate_series(1, 10) i;
CREATE TABLE pt (id int, p_id int REFERENCES p) PARTITION BY RANGE (id);
CREATE TABLE pt1 PARTITION OF pt FOR VALUES FROM (0) TO (100); INSERT INTO pt SELECT i, i FROM generate_series(1, 20) i;
CREATE TABLE c4 (id int, p_id int REFERENCES p); INSERT INTO c4 SELECT i, i FROM generate_series(1, 20) i;
"""
FK_OPTIONS = [{"schema": "public", "table": "p", "data": "latest", "order_by": "id", "limit": 10},
              {"schema": "public", "table": "s", "data": "latest", "order_by": "id", "limit": 5},
              {"schema": "public", "table": "c4", "data": "schema"}]


def _fk_sql(plan):
    return {i.label_name.split(" ON ")[0]: i.sql for i in plan if i.label_kind == "FK CONSTRAINT"}


def test_fk_rule_versions(server, fresh_db, tmp_path):
    db = fresh_db("fk", FK_SCHEMA)
    conn = connect(db, autocommit=False)
    try:
        conn.execute("SET search_path = pg_catalog")
        cat = catalog.load_all(conn, None, conn.info.server_version)
    finally:
        conn.close()
    info, warnings = PartialInfo(), []
    fks = _fk_sql(build_plan(cat, {"tables": FK_OPTIONS}, warnings, info))
    assert fks["c1_p_id_fkey"].rstrip().endswith("REFERENCES public.p(id) NOT VALID;")
    assert fks["c2_nv"].count("NOT VALID") == 1 and "NOT ENFORCED" in fks["c3_ne"] and "VALID" not in fks["c3_ne"]
    assert fks["s_parent_fkey"].rstrip().endswith("NOT VALID;") and fks["pt_p_id_fkey"].rstrip().endswith("NOT VALID;")
    assert "NOT VALID" not in fks["c4_p_id_fkey"]
    assert sorted(info.not_valid_fks) == ["c1_p_id_fkey ON public.c1", "pt_p_id_fkey ON public.pt",
                                          "s_parent_fkey ON public.s"]
    cat.version_num = 170000
    info, warnings = PartialInfo(), []
    fks = _fk_sql(build_plan(cat, {"tables": FK_OPTIONS}, warnings, info))
    assert "pt_p_id_fkey" not in fks and fks["c1_p_id_fkey"].rstrip().endswith("NOT VALID;")
    assert info.skipped_fks == [{"constraint": "pt_p_id_fkey", "table": "public.pt",
                                 "sql": 'ALTER TABLE public.pt ADD CONSTRAINT "pt_p_id_fkey" FOREIGN KEY (p_id) '
                                        "REFERENCES public.p(id);"}]
    assert any("pt_p_id_fkey" in w and "left out" in w for w in warnings)
    res = run_backup(server, db, {"out_dir": str(tmp_path), "tables": FK_OPTIONS})
    target = fresh_db("fk_restore")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]


# ---------------------------------------------------------------- GRANT ALL (MAINTAIN Option A)

def test_grant_all_round_trip(server, analyzed, fresh_db, tmp_path):
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path)})
    text = open(res["file"], encoding="utf-8").read()
    assert 'GRANT ALL ON TABLE app.customers TO "postgres";' in text
    assert 'GRANT SELECT ON TABLE app.customers TO "pgbs_test_reader";' in text and "MAINTAIN" not in text
    db = fresh_db("grantall")
    rr = run_restore(server, db, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    assert schema_lines(db) == schema_lines(analyzed)
    acl = ("SELECT c.oid::regclass::text, c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
           " WHERE n.nspname IN ('app', 'sales data', 'audit') ORDER BY 1")
    with connect(db) as a, connect(analyzed) as b:
        assert a.execute(acl).fetchall() == b.execute(acl).fetchall()
