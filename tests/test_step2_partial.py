"""QA tests for Step 2 - partial backups (latest N / schema only), NOT VALID foreign keys, header, result,
progress estimates, profile helpers, option validation and the PG 12-17 FK omission path.
"""
import gzip
import json
import os
import re

import psycopg
import pytest

from conftest import FIXTURE_SQL, connect, run_psql
from engine import backup as backup_mod
from engine import catalog
from engine.backup import BackupOptions, run_backup
from engine.precheck import TableOption, reconcile_table_options, run_precheck
from engine.restore import run_restore
from engine.util import BackupError
from step1_helpers import md5s, schema_lines, table_counts
from step2_helpers import Env, files, server_dict

ORDERS = {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 1000}
NOTES = {"schema": "app", "table": "notes", "data": "schema"}
LINES_FK = '"order lines_order_id_ordered_at_fkey" ON "sales data"."order lines"'


@pytest.fixture
def env(pg_admin, src_db):
    e = Env(pg_admin)
    yield e
    e.close()


@pytest.fixture(scope="module")
def server():
    return server_dict()


@pytest.fixture(scope="module")
def analyzed(src_db):
    with connect(src_db) as c:
        c.execute("ANALYZE")
    return src_db


def fks(db):
    """{(table, constraint): convalidated} of all top-level foreign keys."""
    with connect(db) as c:
        return {(r[0], r[1]): r[2] for r in c.execute(
            "SELECT conrelid::regclass::text, conname, convalidated FROM pg_constraint"
            " WHERE contype = 'f' AND conparentid = 0")}


def backup_restore(env, server, src, tmp_path, tables, suffix, **opts):
    events = []
    res = run_backup(server, src, {"out_dir": str(tmp_path), "tables": tables, **opts},
                     lambda e, d: events.append((e, d)))
    target = env.db(suffix)
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    return res, target, events


def header_of(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        text = f.read()
    return text.split("SET statement_timeout", 1)[0]


# ------------------------------------------------------------------ spec acceptance: latest 1000 + schema only

def test_spec_partial_backup(env, server, analyzed, tmp_path):
    res, target, events = backup_restore(env, server, analyzed, tmp_path, [ORDERS, NOTES], "spec")
    name = os.path.basename(res["file"])
    assert re.fullmatch(r"pgbs_test_src_\d{8}_\d{6}_partial\.sql", name)
    # result
    assert res["partial"] is True
    assert res["partial_tables"] == [{"table": "app.notes", "data": "schema", "order_by": None, "limit": None},
                                     {"table": '"sales data".orders', "data": "latest", "order_by": "ordered_at",
                                      "limit": 1000}]
    assert res["not_valid_fks"] == [LINES_FK] and res["skipped_fks"] == []
    # header block: each partial table, its option, ordering column and N; relaxed FKs listed
    h = header_of(res["file"])
    assert "-- PARTIAL BACKUP: some tables do not contain all rows.\n" in h
    assert '--   "sales data".orders: latest 1000 rows by ordered_at (newest first)\n' in h
    assert "--   app.notes: schema only (no data)\n" in h
    assert "-- Foreign keys written as NOT VALID" in h and f"--   {LINES_FK}\n" in h
    assert all(ln.startswith("--") or not ln.strip() for ln in h.splitlines())
    # data: exactly the newest 1000 rows, byte-identical
    q = ('SELECT id, customer_id, "order", amount, ordered_at FROM "sales data".orders'
         ' ORDER BY ordered_at DESC LIMIT 1000')
    with connect(analyzed) as c:
        want = c.execute(q).fetchall()
    with connect(target) as c:
        got = c.execute('SELECT id, customer_id, "order", amount, ordered_at FROM "sales data".orders'
                        ' ORDER BY ordered_at DESC').fetchall()
        assert c.execute("SELECT count(*) FROM app.notes").fetchone()[0] == 0
        assert c.execute('SELECT count(*) FROM "sales data".orders').fetchone()[0] == 1000
        # rows were routed to the partitions
        parts = dict(c.execute("SELECT tableoid::regclass::text, count(*) FROM \"sales data\".orders GROUP BY 1"))
        assert sum(parts.values()) == 1000 and set(parts) <= {'"sales data".orders_2024q1',
                                                              '"sales data".orders_2024q2',
                                                              '"sales data".orders_default'}
    assert got == want and len({r[0] for r in got}) == 1000
    # FKs: affected NOT VALID, unaffected validated
    assert fks(target) == {('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey"): False,
                           ('"sales data".orders', "orders_customer_id_fkey"): True,
                           ("app.customers", "customers_referrer_id_fkey"): True}
    # everything else identical
    src_counts = table_counts(analyzed)
    got_counts = table_counts(target)
    assert got_counts.pop('"sales data".orders_2024q1') + got_counts.pop('"sales data".orders_2024q2') + \
        got_counts.pop('"sales data".orders_default') == 1000
    for k in ('"sales data".orders_2024q1', '"sales data".orders_2024q2', '"sales data".orders_default'):
        src_counts.pop(k)
    assert got_counts.pop("app.notes") == 0 and src_counts.pop("app.notes") == 2
    assert got_counts == src_counts
    # structure identical to the source
    assert schema_lines(target) == [ln for ln in schema_lines(analyzed)] or \
        sorted(x.replace(" NOT VALID", "") for x in schema_lines(target)) == \
        sorted(x.replace(" NOT VALID", "") for x in schema_lines(analyzed))
    # rule: no phantom order lines are lost, FK still enforced for new rows
    with connect(target) as c:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute('INSERT INTO "sales data"."order lines" (order_id, ordered_at, qty) VALUES (999999, now(), 1)')
        # unrelated tables keep working
        assert c.execute("SELECT count(*) FROM app.view_a").fetchone()[0] > 0


def test_progress_estimates_use_n_and_skip_schema_only(env, server, analyzed, tmp_path):
    with connect(analyzed) as c:
        others = c.execute(
            "SELECT sum(GREATEST(c.reltuples, 0))::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relkind = 'r' AND NOT c.relispartition AND n.nspname IN ('app','audit','sales data','public')"
            " AND c.oid NOT IN ('app.notes'::regclass, '\"sales data\".orders'::regclass)").fetchone()[0]
        orders_leaves = c.execute("SELECT sum(reltuples)::bigint FROM pg_class WHERE oid IN ("
                                  "SELECT relid FROM pg_partition_tree('\"sales data\".orders') WHERE isleaf)"
                                  ).fetchone()[0]
    assert orders_leaves == 5000
    for limit, expect_orders in ((1000, 1000), (100000, 5000)):  # N, or reltuples when smaller
        ev = []
        res = run_backup(server, analyzed, {"out_dir": str(tmp_path / str(limit)),
                                            "tables": [dict(ORDERS, limit=limit), NOTES]},
                         lambda e, d: ev.append((e, d)))
        totals = [d for e, d in ev if e == "totals"]
        assert len(totals) == 1 and totals[0]["est_rows"] == others + expect_orders, (totals, others)
        started = [d["name"] for e, d in ev if e == "table_start"]
        assert "app.notes" not in started and not any("orders_20" in n or "orders_default" in n for n in started)
        assert '"sales data".orders' in started and totals[0]["tables"] == len(started) == res["tables"]
        done = {d["name"]: d["rows"] for e, d in ev if e == "table_done"}
        assert done['"sales data".orders'] == min(limit, 5000)
        assert sum(done.values()) == res["rows"]
        # phases move forward and done comes last
        phases = [d["name"] for e, d in ev if e == "phase"]
        assert phases == ["catalog", "write", "done"]
        # table_start est_rows of the latest table is the same estimate
        assert next(d["est_rows"] for e, d in ev if e == "table_start" and d["name"] == '"sales data".orders') == \
            expect_orders
    # estimated bytes scale with N
    ev = []
    run_backup(server, analyzed, {"out_dir": str(tmp_path / "b"), "tables": [ORDERS]}, lambda e, d: ev.append((e, d)))
    ev2 = []
    run_backup(server, analyzed, {"out_dir": str(tmp_path / "c")}, lambda e, d: ev2.append((e, d)))
    t1 = next(d for e, d in ev if e == "totals")
    t2 = next(d for e, d in ev2 if e == "totals")
    assert t1["data_bytes"] < t2["data_bytes"] and t1["est_rows"] == t2["est_rows"] - 4000


def test_gzip_partial_name_and_explicit_filename(env, server, analyzed, tmp_path):
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": [ORDERS], "gzip": True})
    assert res["file"].endswith("_partial.sql.gz") and "PARTIAL BACKUP" in header_of(res["file"])
    target = env.db("gzpartial")
    rr = run_restore(server, target, res["file"])
    assert rr["error_count"] == 0, rr["errors"]
    named = run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": [ORDERS], "filename": "mine.sql"})
    assert os.path.basename(named["file"]) == "mine.sql" and named["partial"] is True
    assert "PARTIAL BACKUP" in header_of(named["file"])
    assert files(tmp_path / "nothing") == []
    # no option, no _partial
    plain = run_backup(server, analyzed, {"out_dir": str(tmp_path / "p")})
    assert plain["partial"] is False and "_partial" not in os.path.basename(plain["file"])
    assert "PARTIAL BACKUP" not in header_of(plain["file"])
    # a table explicitly Full is not partial
    full = run_backup(server, analyzed, {"out_dir": str(tmp_path / "q"),
                                         "tables": [{"schema": "app", "table": "notes", "data": "full"}]})
    assert full["partial"] is False


# ------------------------------------------------------------------ FK rule matrix (real restores)

@pytest.fixture
def fx(env, analyzed):
    name = env.db("pfx")
    r = run_psql(name, "-f", str(FIXTURE_SQL))
    assert r.returncode == 0, r.stderr
    with connect(name) as c:
        c.execute("ANALYZE")
    return name


CUST = {"schema": "app", "table": "customers"}


@pytest.mark.parametrize("label,tables,expected", [
    ("customers_schema_only", [dict(CUST, data="schema")],
     {("app.customers", "customers_referrer_id_fkey"): True,  # own table has no rows
      ('"sales data".orders', "orders_customer_id_fkey"): False,  # references an empty table, has data
      ('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey"): True}),
    ("customers_latest_10", [dict(CUST, data="latest", order_by="id", limit=10)],
     {("app.customers", "customers_referrer_id_fkey"): False,  # self reference: parents 1..5 are cut off
      ('"sales data".orders', "orders_customer_id_fkey"): False,
      ('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey"): True}),
    ("orders_schema_only", [{"schema": "sales data", "table": "orders", "data": "schema"}],
     {("app.customers", "customers_referrer_id_fkey"): True,
      ('"sales data".orders', "orders_customer_id_fkey"): True,  # orders has no rows
      ('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey"): False}),
    ("order_lines_schema_only", [{"schema": "sales data", "table": "order lines", "data": "schema"}],
     {("app.customers", "customers_referrer_id_fkey"): True,
      ('"sales data".orders', "orders_customer_id_fkey"): True,
      ('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey"): True}),
    ("referencing_partial_only", [dict(ORDERS, limit=10), {"schema": "sales data", "table": "order lines",
                                                           "data": "latest", "order_by": "id", "limit": 5}],
     {("app.customers", "customers_referrer_id_fkey"): True,
      ('"sales data".orders', "orders_customer_id_fkey"): True,  # parent customers is full
      ('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey"): False}),
    ("both_schema_only", [dict(CUST, data="schema"), {"schema": "sales data", "table": "orders", "data": "schema"}],
     {("app.customers", "customers_referrer_id_fkey"): True,
      ('"sales data".orders', "orders_customer_id_fkey"): True,
      ('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey"): False}),
])
def test_fk_not_valid_rule_and_restore_has_no_errors(env, server, fx, tmp_path, label, tables, expected):
    res, target, _ = backup_restore(env, server, fx, tmp_path, tables, "fk_" + label)
    assert fks(target) == expected
    assert res["partial"] and len(res["not_valid_fks"]) == sum(1 for v in expected.values() if not v)
    # NOT VALID FKs still reject bad new rows
    if not expected[('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey")]:
        with connect(target) as c:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                c.execute('INSERT INTO "sales data"."order lines" (order_id, ordered_at, qty)'
                          " VALUES (987654, now(), 1)")
    # full-table content outside the partial tables identical to the source
    for t in ("app.edge", "app.vehicle", "app.car"):
        with connect(target) as a, connect(fx) as b:
            assert a.execute(f"SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM {t} x").fetchone() == \
                b.execute(f"SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM {t} x").fetchone()


def test_latest_on_customers_generated_citext_and_identity(env, server, fx, tmp_path):
    tabs = [dict(CUST, data="latest", order_by="id", limit=10),
            {"schema": "app", "table": "ident_always", "data": "latest", "order_by": "id", "limit": 3}]
    res, target, _ = backup_restore(env, server, fx, tmp_path, tabs, "gen")
    with connect(target) as t, connect(fx) as s:
        q = "SELECT id, email, name, full_name, mood, addr, band FROM app.customers ORDER BY id DESC LIMIT 10"
        assert t.execute(q).fetchall() == s.execute(q).fetchall()
        assert [r[0] for r in t.execute("SELECT id FROM app.ident_always ORDER BY id")] == [5, 6, 7]
        # sequence values are those of the source even for partial tables
        assert t.execute("SELECT last_value FROM app.customers_id_seq").fetchone() == \
            s.execute("SELECT last_value FROM app.customers_id_seq").fetchone()
        assert t.execute("SELECT nextval('app.ident_always_id_seq')").fetchone() == \
            s.execute("SELECT nextval('app.ident_always_id_seq')").fetchone()
        # the audit trigger must not have fired during restore of partial data
        assert t.execute("SELECT count(*) FROM audit.log").fetchone() == s.execute(
            "SELECT count(*) FROM audit.log").fetchone()
        assert t.execute("SELECT relispopulated FROM pg_class WHERE oid = '\"sales data\".monthly_totals'::regclass"
                         ).fetchone()[0] is True


def test_latest_on_inheritance_parent_uses_only(env, server, fx, tmp_path):
    veh = {"schema": "app", "table": "vehicle", "data": "latest", "order_by": "id", "limit": 100}
    res, target, ev = backup_restore(env, server, fx, tmp_path, [veh], "inh")
    with connect(target) as c:
        assert c.execute("SELECT count(*) FROM ONLY app.vehicle").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM app.car").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM app.vehicle").fetchone()[0] == 3
    rows = {d["name"]: d["rows"] for e, d in ev if e == "table_done"}
    assert rows["app.vehicle"] == 1 and rows["app.car"] == 2


def test_latest_ties_and_n_larger_than_table_and_empty_table(env, server, tmp_path):
    db = env.db("ties", '''
        CREATE TABLE t (id int PRIMARY KEY, d date NOT NULL); INSERT INTO t SELECT g, date '2024-01-01' FROM generate_series(1, 100) g;
        CREATE TABLE small (id int PRIMARY KEY); INSERT INTO small VALUES (1), (2);
        CREATE TABLE empty (id int PRIMARY KEY);
        CREATE TABLE "Weird ""Name""" ("select" int PRIMARY KEY, "order" timestamptz, "tab col" text);
        INSERT INTO "Weird ""Name""" SELECT g, timestamptz '2024-01-01' + g * interval '1 day', 'x' FROM generate_series(1, 30) g;''')
    tabs = [{"schema": "public", "table": "t", "data": "latest", "order_by": "d", "limit": 7},
            {"schema": "public", "table": "small", "data": "latest", "order_by": "id", "limit": 50},
            {"schema": "public", "table": "empty", "data": "latest", "order_by": "id", "limit": 50},
            {"schema": "public", "table": 'Weird "Name"', "data": "latest", "order_by": "order", "limit": 4}]
    res, target, _ = backup_restore(env, server, db, tmp_path, tabs, "ties_r")
    with connect(target) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 7  # exactly N even with ties
        assert c.execute("SELECT count(*) FROM small").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM empty").fetchone()[0] == 0
        assert [r[0] for r in c.execute('SELECT "select" FROM "Weird ""Name""" ORDER BY 1')] == [27, 28, 29, 30]
    h = header_of(res["file"])
    # (the reserved word "order" is shown bare: cosmetic, see QA report OBS-S2-3)
    assert '--   public."Weird ""Name""": latest 4 rows by order (newest first)' in h
    assert "--   public.t: latest 7 rows by d (newest first)" in h


def test_nullable_ordering_column_nulls_last_and_non_null_form(env, server, tmp_path):
    db = env.db("nulls", "CREATE TABLE n (id int PRIMARY KEY, ts timestamptz);"
                         "INSERT INTO n SELECT g, CASE WHEN g % 3 = 0 THEN NULL ELSE timestamptz '2024-01-01' + g * interval '1h' END"
                         " FROM generate_series(1, 30) g;")
    # newest 20 of 30: 20 non-null rows exist (10 are NULL) -> no NULL row may be taken before a dated one
    res, target, _ = backup_restore(env, server, db, tmp_path,
                                    [{"schema": "public", "table": "n", "data": "latest", "order_by": "ts",
                                      "limit": 20}], "nulls_r")
    with connect(target) as c:
        assert c.execute("SELECT count(*), count(ts) FROM n").fetchone() == (20, 20)
    res, target, _ = backup_restore(env, server, db, tmp_path / "b",
                                    [{"schema": "public", "table": "n", "data": "latest", "order_by": "ts",
                                      "limit": 25}], "nulls_r2")
    with connect(target) as c:
        assert c.execute("SELECT count(*), count(ts) FROM n").fetchone() == (25, 20)


def test_latest_on_subpartitioned_table(env, server, tmp_path):
    db = env.db("subpart", """
        CREATE TABLE m (id int NOT NULL, d date NOT NULL, region text NOT NULL, PRIMARY KEY (id, d, region)) PARTITION BY RANGE (d);
        CREATE TABLE m_2024 PARTITION OF m FOR VALUES FROM ('2024-01-01') TO ('2025-01-01') PARTITION BY LIST (region);
        CREATE TABLE m_2024_eu PARTITION OF m_2024 FOR VALUES IN ('eu');
        CREATE TABLE m_2024_us PARTITION OF m_2024 FOR VALUES IN ('us');
        CREATE TABLE m_2025 PARTITION OF m FOR VALUES FROM ('2025-01-01') TO ('2026-01-01');
        INSERT INTO m SELECT g, date '2024-01-01' + g, CASE WHEN g % 2 = 0 THEN 'eu' ELSE 'us' END FROM generate_series(1, 600) g;
        ANALYZE m;""")
    pre = run_precheck(server, db)
    assert [t["qname"] for t in pre["tables"]] == ["public.m"]  # one logical table
    assert pre["tables"][0]["partitions"] == 3 and pre["tables"][0]["est_rows"] == 600  # leaves only
    tabs = [{"schema": "public", "table": "m", "data": "latest", "order_by": "d", "limit": 50}]
    res, target, _ = backup_restore(env, server, db, tmp_path, tabs, "subpart_r")
    with connect(target) as t, connect(db) as s:
        assert t.execute("SELECT count(*) FROM m").fetchone()[0] == 50
        assert sorted(t.execute("SELECT id FROM m")) == sorted(s.execute("SELECT id FROM m ORDER BY d DESC LIMIT 50"))
    res, target, _ = backup_restore(env, server, db, tmp_path / "full", [], "subpart_full")
    assert table_counts(target) == table_counts(db)


# ------------------------------------------------------------------ header safety with hostile names

def test_header_stays_comment_with_hostile_names(env, server, tmp_path):
    db = env.db("hostile", """
        CREATE TABLE "par
ent;DROP TABLE x;--" (id int PRIMARY KEY); INSERT INTO "par
ent;DROP TABLE x;--" SELECT generate_series(1, 5);
        CREATE TABLE "chi
ld" (id int, pid int, CONSTRAINT "fk
name" FOREIGN KEY (pid) REFERENCES "par
ent;DROP TABLE x;--" (id)); INSERT INTO "chi
ld" VALUES (1, 1), (2, 5);
        CREATE TABLE x (id int);""")
    tabs = [{"schema": "public", "table": "par\nent;DROP TABLE x;--", "data": "latest", "order_by": "id", "limit": 2}]
    res, target, _ = backup_restore(env, server, db, tmp_path, tabs, "hostile_r")
    h = header_of(res["file"])
    assert "PARTIAL BACKUP" in h
    assert all(ln.startswith("--") or not ln.strip() for ln in h.splitlines()), h
    with connect(target) as c:
        assert c.execute("SELECT to_regclass('x')").fetchone()[0] == "x"
        assert c.execute('SELECT count(*) FROM "chi\nld"').fetchone()[0] == 2


# ------------------------------------------------------------------ PG 12-17: FK on partitioned table left out

def test_fk_on_partitioned_table_omitted_pre_18_end_to_end(env, server, fx, tmp_path, monkeypatch):
    orig = catalog.load_all

    def as_pg17(*a, **k):
        cat = orig(*a, **k)
        cat.version_num = 170000
        return cat

    monkeypatch.setattr(catalog, "load_all", as_pg17)
    tabs = [dict(CUST, data="latest", order_by="id", limit=50)]  # all rows are in, but the option makes it partial
    res, target, _ = backup_restore(env, server, fx, tmp_path, tabs, "pg17")
    stmt = ('ALTER TABLE "sales data".orders ADD CONSTRAINT "orders_customer_id_fkey" FOREIGN KEY (customer_id)'
            " REFERENCES app.customers(id);")
    assert [f["sql"] for f in res["skipped_fks"]] == [stmt]
    assert res["skipped_fks"][0]["constraint"] == "orders_customer_id_fkey"
    assert any("orders_customer_id_fkey" in w and "left out" in w for w in res["warnings"])
    h = header_of(res["file"])
    assert f"--   {stmt}\n" in h and "Foreign keys left out" in h
    assert all(ln.startswith("--") or not ln.strip() for ln in h.splitlines())
    got = fks(target)
    assert ('"sales data".orders', "orders_customer_id_fkey") not in got
    assert got[("app.customers", "customers_referrer_id_fkey")] is False  # non-partitioned: NOT VALID is fine
    assert got[('"sales data"."order lines"', "order lines_order_id_ordered_at_fkey")] is True
    with connect(target) as c:  # the statement from the header runs and restores the constraint
        c.execute(stmt)
        assert c.execute("SELECT convalidated FROM pg_constraint WHERE conname = 'orders_customer_id_fkey'"
                         " AND conparentid = 0").fetchone()[0] is True
    # a non-partitioned child of a partial parent is still written NOT VALID on a pre-18 server
    assert "NOT VALID" in open(res["file"], encoding="utf-8").read()


def test_pg17_mock_changes_nothing_when_no_partitioned_fk_is_affected(env, server, fx, tmp_path, monkeypatch):
    orig = catalog.load_all
    monkeypatch.setattr(catalog, "load_all", lambda *a, **k: setattr(c := orig(*a, **k), "version_num", 170000) or c)
    res, target, _ = backup_restore(env, server, fx, tmp_path, [ORDERS], "pg17b")
    assert res["skipped_fks"] == []  # orders (partitioned) references a FULL table: not touched
    assert fks(target)[('"sales data".orders', "orders_customer_id_fkey")] is True


# ------------------------------------------------------------------ option validation

@pytest.mark.parametrize("opt", [
    {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 0},
    {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": -5},
    {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": "1000"},
    {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 10.5},
    {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": True},
    {"schema": "sales data", "table": "orders", "data": "latest", "order_by": "", "limit": 5},
    {"schema": "sales data", "table": "orders", "data": "latest", "limit": 5},
    {"schema": "sales data", "table": "orders", "data": "LATEST", "order_by": "ordered_at", "limit": 5},
    {"schema": "sales data", "table": "orders", "data": "ignore"},
    {"schema": "", "table": "orders", "data": "schema"},
    {"table": "orders", "data": "schema"},
])
def test_invalid_table_options_refused_before_anything_happens(analyzed, server, tmp_path, opt):
    with pytest.raises(BackupError):
        run_backup(server, analyzed, {"out_dir": str(tmp_path / "o"), "tables": [opt]})
    assert not (tmp_path / "o").exists() or files(tmp_path / "o") == []
    with pytest.raises(BackupError):
        BackupOptions.from_value({"tables": [opt]})


def test_unknown_ordering_column_refused_by_name(analyzed, server, tmp_path):
    for col in ("no_such_col", "ORDERED_AT", "ordered at"):
        with pytest.raises(BackupError, match=re.escape(col)):
            run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": [dict(ORDERS, order_by=col)]})
    assert files(tmp_path) == []
    # the pre-check shows it instead of failing
    pre = run_precheck(server, analyzed, tables=[dict(ORDERS, order_by="nope")])
    assert "nope" in next(t for t in pre["tables"] if t["name"] == "orders")["option_error"]


def test_options_for_missing_tables_are_ignored_with_warning(analyzed, server, tmp_path):
    tabs = [{"schema": "app", "table": "no_such_table", "data": "schema"},
            {"schema": "nosuchschema", "table": "x", "data": "latest", "order_by": "id", "limit": 3},
            {"schema": "sales data", "table": "orders_2024q1", "data": "schema"},  # a partition
            {"schema": "app", "table": "view_a", "data": "schema"}]  # a view
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "tables": tabs})
    assert res["partial"] is False and "_partial" not in res["file"]
    assert len([w for w in res["warnings"] if "ignored" in w]) == 4
    assert res["rows"] > 0
    pre = run_precheck(server, analyzed, tables=tabs)
    assert len(pre["ignored_options"]) == 4 and pre["impact"]["tables"] > 10


def test_unsortable_ordering_column_fails_cleanly_midfile(env, server, tmp_path):
    db = env.db("unsortable", "CREATE TABLE a (id int PRIMARY KEY); INSERT INTO a VALUES (1);"
                              "CREATE TABLE z (id int PRIMARY KEY, j json); INSERT INTO z VALUES (1, '{}');")
    tabs = [{"schema": "public", "table": "z", "data": "latest", "order_by": "j", "limit": 3}]
    with pytest.raises(BackupError, match="public.z"):
        run_backup(server, db, {"out_dir": str(tmp_path), "tables": tabs})
    assert files(tmp_path) == []  # the .part written so far is removed


def test_huge_limit(env, server, tmp_path):
    db = env.db("hugelimit", "CREATE TABLE a (id int PRIMARY KEY); INSERT INTO a VALUES (1), (2);")
    ok = {"schema": "public", "table": "a", "data": "latest", "order_by": "id", "limit": 2 ** 62}
    assert run_backup(server, db, {"out_dir": str(tmp_path), "tables": [ok]})["rows"] == 2
    bad = dict(ok, limit=2 ** 70)
    try:
        r = run_backup(server, db, {"out_dir": str(tmp_path / "b"), "tables": [bad]})
        assert r["rows"] == 2
    except BackupError:
        pass
    assert [f for f in files(tmp_path / "b") if f.endswith(".part")] == []


@pytest.mark.parametrize("bad", [{"lock_timeout_s": -1}, {"lock_timeout_s": 3601}, {"lock_timeout_s": "10"},
                                 {"speed_limit_mb_s": 0}, {"speed_limit_mb_s": 0.09}, {"speed_limit_mb_s": -3},
                                 {"speed_limit_mb_s": "fast"}])
def test_invalid_safety_options(analyzed, server, tmp_path, bad):
    with pytest.raises(BackupError):
        run_backup(server, analyzed, {"out_dir": str(tmp_path), **bad})
    assert files(tmp_path) == []


# ------------------------------------------------------------------ profile helpers (file I/O is Step 3)

def test_to_profile_shape_and_roundtrip():
    opts = BackupOptions.from_value({"mode": "full", "schemas": ["app", "sales data"], "privileges": False, "gzip": True,
                                     "out_dir": "~/pg_backups", "filename": "x.sql", "lock_timeout_s": 5,
                                     "speed_limit_mb_s": 2.5,
                                     "tables": [ORDERS, NOTES, {"schema": "app", "table": "customers", "data": "full"}]})
    prof = opts.to_profile()
    assert set(prof) == {"saved_at", "options"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", prof["saved_at"])
    assert set(prof["options"]) == {"mode", "schemas", "privileges", "gzip", "out_dir", "lock_timeout_s",
                                    "speed_limit_mb_s", "tables"}
    assert prof["options"]["tables"] == [ORDERS, NOTES]  # only non-full tables, exact option keys
    assert prof["options"]["schemas"] == ["app", "sales data"] and prof["options"]["privileges"] is False
    assert json.loads(json.dumps(prof)) == prof  # JSON-serialisable
    again = BackupOptions.from_value(prof["options"])
    assert again.to_profile(saved_at=None)["options"] == prof["options"]
    default = BackupOptions().to_profile()["options"]
    assert default["schemas"] is None and default["tables"] == [] and default["speed_limit_mb_s"] is None
    assert default["lock_timeout_s"] == 10 and default["mode"] == "full" and default["privileges"] is True
    # a stored "latest" entry does not carry limit for others
    assert TableOption.from_dict(NOTES).to_dict() == NOTES


def test_saved_options_reconcile_with_live_database(env, server, analyzed):
    saved = [ORDERS, NOTES,
             {"schema": "app", "table": "vanished", "data": "schema"},
             {"schema": "sales data", "table": "orders_2024q2", "data": "schema"},   # a partition
             {"schema": "gone_schema", "table": "t", "data": "latest", "order_by": "id", "limit": 9},
             {"schema": "app", "table": "customers", "data": "mystery"}]           # corrupt entry
    pre = run_precheck(server, analyzed, tables=saved)
    assert sorted(pre["ignored_options"]) == sorted(["app.vanished", '"sales data".orders_2024q2', "gone_schema.t",
                                                     "app.customers"])
    by = {t["qname"]: t for t in pre["tables"]}
    assert by['"sales data".orders']["option"] == {"data": "latest", "order_by": "ordered_at", "limit": 1000}
    assert by["app.notes"]["option"]["data"] == "schema"
    new = [t for t in pre["tables"] if t["qname"] not in ('"sales data".orders', "app.notes")]
    assert new and all(t["option"]["data"] == "full" for t in new)  # tables without a saved option are Full
    # schema-restricted pre-check: options for tables outside the selection are ignored
    pre2 = run_precheck(server, analyzed, schemas=["audit"], tables=saved)
    assert '"sales data".orders' in pre2["ignored_options"] or "sales data.orders" in " ".join(pre2["ignored_options"])
    # reconcile (pure) agrees
    rels = catalog_relations(server, analyzed)
    kept, ignored = reconcile_table_options(rels, saved)
    assert [o.to_dict() for o in kept] == [ORDERS, NOTES]
    assert len(ignored) == 4


def catalog_relations(server, db):
    from engine.db import connect as eng_connect
    conn = eng_connect(server, db)
    try:
        conn.execute("SET search_path = pg_catalog")
        return catalog.list_relations(conn, None)
    finally:
        conn.close()
