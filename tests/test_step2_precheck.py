"""QA tests for Step 2 - pre-check details (large flags, ordering suggestions, index advice verified with EXPLAIN,
partitioned / inherited tables, impact estimate) and GRANT ALL.
"""
import os
import re

import pytest

from conftest import FIXTURE_SQL, connect, run_psql
from engine import backup as backup_mod
from engine import catalog
from engine.backup import build_plan, run_backup
from engine.precheck import Thresholds, run_precheck
from engine.restore import run_restore
from step1_helpers import schema_lines, table_counts
from step2_helpers import Env, server_dict

MB = 2 ** 20


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


def by_name(pre):
    return {t["qname"]: t for t in pre["tables"]}


# ------------------------------------------------------------------ listing, partitions, inheritance, sizes

def test_listing_partitioned_as_one_parent_and_sizes(server, analyzed):
    pre = run_precheck(server, analyzed)
    names = by_name(pre)
    assert not [n for n in names if re.search(r"orders_(20|default)", n)]            # no partitions
    assert not [n for n in names if n.endswith(("_seq", "view_a", "view_b", "monthly_totals"))]  # no seq/views
    o = names['"sales data".orders']
    assert o["kind"] == "partitioned" and o["partitions"] == 3 and o["est_rows"] == 5000
    assert names["app.customers"]["kind"] == "table" and names["app.customers"]["partitions"] == 0
    with connect(analyzed) as c:
        want = c.execute("SELECT sum(pg_table_size(relid)) FROM pg_partition_tree('\"sales data\".orders')"
                         " WHERE isleaf").fetchone()[0]
        assert o["size"] == want and o["size_exact"] is True
        for qn, t in names.items():
            if t["kind"] == "partitioned":
                want_size = c.execute("SELECT sum(pg_table_size(relid)) FROM pg_partition_tree(%s::regclass)"
                                      " WHERE isleaf", (qn,)).fetchone()[0]
            else:
                want_size = c.execute("SELECT pg_table_size(%s::regclass)", (qn,)).fetchone()[0]
            assert t["size"] == want_size, qn
    assert [c["name"] for c in o["order_columns"]] == ["id", "ordered_at"]
    assert o["default_order_column"] == "ordered_at"
    assert {t["qname"] for t in pre["tables"]} >= {"app.customers", "app.edge", "app.secrets", "audit.log"}


def test_partitions_of_non_full_parent_are_not_offered_and_options_ignored(server, analyzed):
    opts = [{"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 10},
            {"schema": "sales data", "table": "orders_default", "data": "schema"}]
    pre = run_precheck(server, analyzed, tables=opts)
    assert not [t for t in pre["tables"] if "orders_" in t["name"]]
    assert pre["ignored_options"] == ['"sales data".orders_default']
    assert by_name(pre)['"sales data".orders']["option"]["data"] == "latest"
    pre = run_precheck(server, analyzed, tables=[dict(opts[0], data="schema")])
    assert not [t for t in pre["tables"] if "orders_" in t["name"]]


def test_inheritance_children_are_separate_tables(server, analyzed):
    names = by_name(run_precheck(server, analyzed))
    assert names["app.vehicle"]["est_rows"] == 1 and names["app.car"]["est_rows"] == 2   # ONLY semantics
    with connect(analyzed) as c:
        assert names["app.vehicle"]["size"] == c.execute("SELECT pg_table_size('app.vehicle')").fetchone()[0]
        assert names["app.car"]["size"] == c.execute("SELECT pg_table_size('app.car')").fetchone()[0]


def test_unanalyzed_table_rows_unknown(env, server):
    db = env.db("unanalyzed", "CREATE TABLE a (id int PRIMARY KEY); INSERT INTO a SELECT generate_series(1, 1000);")
    # autovacuum could analyze in the meantime: only check the flag matches pg_class
    with connect(db) as c:
        rt = c.execute("SELECT reltuples FROM pg_class WHERE oid = 'a'::regclass").fetchone()[0]
    t = by_name(run_precheck(server, db))["public.a"]
    assert t["rows_unknown"] == (rt < 0)
    if rt < 0:
        assert t["est_rows"] == 0
    assert t["size"] > 0


def test_precheck_never_errors_on_empty_and_odd_dbs(env, server):
    db = env.db("emptydb")
    pre = run_precheck(server, db)
    assert pre["tables"] == [] and pre["problems"] == [] and pre["impact"]["data_bytes"] == 0
    db2 = env.db("onlyseq", "CREATE SEQUENCE s; CREATE VIEW v AS SELECT 1 AS x;")
    pre = run_precheck(server, db2)
    assert pre["tables"] == [] and not pre["blocking"]


# ------------------------------------------------------------------ large flags

def test_large_flag_thresholds(server, analyzed):
    base = run_precheck(server, analyzed)
    assert base["thresholds"] == {"large_mb": 500, "large_rows": 1_000_000}
    assert not any(t["large"] for t in base["tables"])
    o = by_name(base)['"sales data".orders']
    # rows: strictly greater than the threshold
    for lim, flagged in ((5000, False), (4999, True)):
        pre = run_precheck(server, analyzed, thresholds={"large_table_rows": lim})
        assert [t["qname"] for t in pre["tables"] if t["large"]] == (['"sales data".orders'] if flagged else [])
        if flagged:
            assert by_name(pre)['"sales data".orders']["large_reason"] == ["rows"]
    # size: strictly greater than the threshold (power-of-two division is exact)
    for mb, flagged in ((o["size"] / MB, False), ((o["size"] - 1) / MB, True)):
        pre = run_precheck(server, analyzed, thresholds={"large_table_mb": mb})
        big = [t for t in pre["tables"] if t["large"]]
        assert ('"sales data".orders' in [t["qname"] for t in big]) == flagged
    pre = run_precheck(server, analyzed, thresholds=Thresholds(large_mb=0.0001, large_rows=10))
    both = by_name(pre)
    assert both['"sales data".orders']["large_reason"] == ["size", "rows"]
    assert both["app.customers"]["large_reason"] == ["size", "rows"]
    assert pre["thresholds"] == {"large_mb": 0.0001, "large_rows": 10}
    assert by_name(pre)["app.zero_cols"]["large"] is False or by_name(pre)["app.zero_cols"]["size"] > 0


def test_threshold_settings_parsing():
    assert (Thresholds.from_settings(None).large_mb, Thresholds.from_settings({}).large_rows) == (500, 1_000_000)
    t = Thresholds.from_settings({"large_table_mb": 12.5, "large_table_rows": 77})
    assert (t.large_mb, t.large_rows) == (12.5, 77)
    for bad in ({"large_table_mb": "x"}, {"large_table_rows": None}, {"large_table_mb": True}, []):
        Thresholds.from_settings(bad)  # must not raise
    for neg in ({"large_table_mb": -1}, {"large_table_rows": -5}, {"large_table_mb": float("nan")},
                {"large_table_rows": 1.5}):
        got = Thresholds.from_settings(neg)
        assert got.large_mb >= 0 and got.large_rows >= 0 and got.large_mb == got.large_mb, (neg, got)


@pytest.mark.xfail(reason="BUG-S2-5 (minor): Thresholds.from_settings raises AttributeError for a non-dict value", strict=True)
def test_threshold_settings_non_dict_does_not_raise():
    assert Thresholds.from_settings("junk") == Thresholds()


# ------------------------------------------------------------------ ordering column suggestions

ORDER_SQL = """
CREATE DOMAIN ts_dom AS timestamptz; CREATE DOMAIN ts_dom2 AS ts_dom;
CREATE TABLE a (id int PRIMARY KEY, created date, ts timestamptz);
CREATE TABLE b (id int PRIMARY KEY, note text);
CREATE TABLE c (x int, y text);
CREATE TABLE d (a int, z int, PRIMARY KEY (z, a));
CREATE TABLE e (id int PRIMARY KEY, happened ts_dom2, other time, iv interval, arr timestamptz[]);
CREATE TABLE f (id int PRIMARY KEY, w timestamp);
CREATE TABLE "G H" ("Id" int PRIMARY KEY, "Created At" timestamptz NOT NULL);
CREATE TABLE h (ts timestamptz, id int);
"""


def test_ordering_column_suggestions(env, server):
    db = env.db("ordercols", ORDER_SQL)
    t = by_name(run_precheck(server, db))
    assert t["public.a"]["default_order_column"] == "created"  # first timestamp-like column in table order
    assert [c["name"] for c in t["public.a"]["order_columns"]] == ["id", "created", "ts"]
    assert t["public.b"]["default_order_column"] == "id"       # no timestamp: the primary key
    assert t["public.c"]["default_order_column"] is None and t["public.c"]["order_columns"] == []
    assert t["public.d"]["default_order_column"] in ("z", "a")  # composite PK: a PK column (see QA report OBS-S2-4)
    assert [c["name"] for c in t["public.e"]["order_columns"]] == ["id", "happened"]  # domain-of-domain counts
    assert t["public.e"]["default_order_column"] == "happened"
    assert t["public.f"]["default_order_column"] == "w"
    g = t['public."G H"']
    assert g["default_order_column"] == "Created At" and [c["name"] for c in g["order_columns"]] == ["Id", "Created At"]
    assert g["order_columns"][1]["not_null"] is True and g["order_columns"][0]["pk"] is True
    assert t["public.h"]["default_order_column"] == "ts" and [c["name"] for c in t["public.h"]["order_columns"]] == ["ts"]
    for tab in t.values():
        for col in tab["order_columns"]:
            assert set(col) >= {"name", "type", "pk", "timestamp_like", "not_null", "index"}
    # the default shows up as the pre-selected option's ordering column
    assert t["public.a"]["option"] == {"data": "full", "order_by": "created", "limit": 100_000}


def test_suggested_default_can_be_used_by_a_real_backup(env, server, tmp_path):
    db = env.db("ordercols_run", ORDER_SQL + "INSERT INTO a SELECT g, date '2024-01-01' + g, now() FROM generate_series(1, 20) g;")
    pre = run_precheck(server, db)
    for t in pre["tables"]:
        if t["default_order_column"]:
            opt = [{"schema": t["schema"], "table": t["name"], "data": "latest", "order_by": t["default_order_column"],
                    "limit": 3}]
            res = run_backup(server, db, {"out_dir": str(tmp_path / t["name"].replace(" ", "_")), "tables": opt,
                                          "schemas": ["public"]})
            target = env.db("ocr_" + re.sub(r"\W", "", t["name"]))
            rr = run_restore(server, target, res["file"])
            assert rr["error_count"] == 0, (t["qname"], rr["errors"])


# ------------------------------------------------------------------ index advice, verified with EXPLAIN

INDEX_CASES = [
    # name, column, index DDL (None = no index), expected "usable" for the backup's query
    ("nn_none", "nn", None, False),
    ("nn_asc", "nn", "(nn)", True),
    ("nn_desc", "nn", "(nn DESC)", True),
    ("nn_desc_nl", "nn", "(nn DESC NULLS LAST)", False),     # null-safe query is plain DESC (NULLS FIRST)
    ("nl_none", "nl", None, False),
    ("nl_asc", "nl", "(nl)", False),
    ("nl_desc", "nl", "(nl DESC)", False),
    ("nl_desc_nl", "nl", "(nl DESC NULLS LAST)", True),
    ("nl_asc_nf", "nl", "(nl NULLS FIRST)", True),
    ("nl_partial", "nl", "(nl DESC NULLS LAST) WHERE id > 0", False),
    ("nl_second_col", "nl", "(id, nl)", False),
]


@pytest.fixture(scope="module")
def idx_db(pg_admin, src_db):
    e = Env(pg_admin)
    ddl = []
    for name, col, index, _ in INDEX_CASES:
        ddl.append(f"CREATE TABLE {name} (id int PRIMARY KEY, nn timestamptz NOT NULL, nl timestamptz);"
                   f"INSERT INTO {name} SELECT g, timestamptz '2020-01-01' + g * interval '1 min',"
                   f" CASE WHEN g % 10 = 0 THEN NULL ELSE timestamptz '2020-01-01' + g * interval '1 min' END"
                   f" FROM generate_series(1, 30000) g;")
        if index:
            ddl.append(f"CREATE INDEX {name}_ix ON {name} {index};")
    ddl.append("ANALYZE;")
    name = e.db("idx", "".join(ddl))
    yield name
    e.close()


@pytest.mark.parametrize("case", INDEX_CASES, ids=[c[0] for c in INDEX_CASES])
def test_index_advice_matches_the_planner(idx_db, server, case):
    name, col, _, expect = case
    pre = run_precheck(server, idx_db)
    column = next(c for c in by_name(pre)[f"public.{name}"]["order_columns"] if c["name"] == col)
    idx = column["index"]
    assert idx["usable"] is expect, idx
    assert idx["message"] and ("serves" in idx["message"]) == expect
    if not expect:
        assert "sorted" in idx["message"] or "cannot be used" in idx["message"] or "NULLS" in idx["message"]
    # ground truth: does the real "latest N" query use an index scan?
    conn = connect(idx_db, autocommit=False)
    try:
        conn.execute("SET search_path = pg_catalog")
        cat = catalog.load_all(conn, None, conn.info.server_version)
        opts = {"tables": [{"schema": "public", "table": name, "data": "latest", "order_by": col, "limit": 100}]}
        spec = next(i.copy for i in build_plan(cat, opts) if i.copy and i.copy.name == name)
        select = spec.source.as_string(conn)[len("COPY ("):-len(") TO STDOUT")]
        plan = "\n".join(r[0] for r in conn.execute("EXPLAIN " + select))
    finally:
        conn.close()
    uses_index = "Index" in plan and "Sort" not in plan
    assert uses_index == expect, (select, plan)


def test_orders_ordered_at_warning_then_clears_with_index(env, server, analyzed):
    pre = run_precheck(server, analyzed)
    col = next(c for c in by_name(pre)['"sales data".orders']["order_columns"] if c["name"] == "ordered_at")
    assert col["index"]["indexed"] is False and col["index"]["usable"] is False
    assert col["index"]["message"].startswith("No index starts with ordered_at")
    # private copy: add an index on the partitioned parent
    db = env.db("orders_idx")
    assert run_psql(db, "-f", str(FIXTURE_SQL)).returncode == 0
    with connect(db) as c:
        c.execute('CREATE INDEX orders_ordered_at_ix ON "sales data".orders (ordered_at)')
        c.execute("ANALYZE")
    col = next(c for c in by_name(run_precheck(server, db))['"sales data".orders']["order_columns"]
               if c["name"] == "ordered_at")
    assert col["index"]["usable"] is True and col["index"]["null_safe"] is True
    with connect(db) as c:  # an invalid index (e.g. failed CONCURRENTLY build) does not count
        c.execute("UPDATE pg_index SET indisvalid = false WHERE indexrelid = '\"sales data\".orders_ordered_at_ix'::regclass")
    col = next(c for c in by_name(run_precheck(server, db))['"sales data".orders']["order_columns"]
               if c["name"] == "ordered_at")
    assert col["index"]["usable"] is False


# ------------------------------------------------------------------ impact estimate

def test_impact_estimate(server, analyzed):
    base = run_precheck(server, analyzed)
    imp = base["impact"]
    tabs = base["tables"]
    assert imp["tables"] == len(tabs) and imp["data_bytes"] == imp["full_data_bytes"] == sum(t["size"] for t in tabs)
    assert imp["est_rows"] == imp["full_est_rows"] == sum(t["est_rows"] for t in tabs)
    with connect(analyzed) as c:  # estimate within 1% of reality (small, freshly analysed tables)
        actual = sum(table_counts(analyzed).values())
    assert abs(imp["est_rows"] - actual) <= max(2, actual * 0.01), (imp["est_rows"], actual)
    largest = imp["largest"]
    assert len(largest) == min(10, len(tabs)) and [x["size"] for x in largest] == sorted(
        (x["size"] for x in largest), reverse=True)
    assert largest[0]["size"] == max(t["size"] for t in tabs)
    assert all(set(x) == {"qname", "size", "est_rows"} for x in largest)
    # latest 1000 of 5000
    o = by_name(base)['"sales data".orders']
    opts = [{"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 1000}]
    lat = run_precheck(server, analyzed, tables=opts)["impact"]
    assert lat["est_rows"] == imp["est_rows"] - 4000
    assert abs(lat["data_bytes"] - (imp["data_bytes"] - o["size"] * 0.8)) <= 2
    assert lat["full_data_bytes"] == imp["full_data_bytes"]
    # N larger than the table changes nothing; schema-only removes the table; schema mode removes everything
    big = run_precheck(server, analyzed, tables=[dict(opts[0], limit=10 ** 6)])["impact"]
    assert big["est_rows"] == imp["est_rows"] and big["data_bytes"] == imp["data_bytes"]
    so = run_precheck(server, analyzed, tables=[dict(opts[0], data="schema")])["impact"]
    assert so["data_bytes"] == imp["data_bytes"] - o["size"] and so["est_rows"] == imp["est_rows"] - 5000
    assert so["tables"] == imp["tables"] - 1
    sch = run_precheck(server, analyzed, mode="schema")["impact"]
    assert (sch["data_bytes"], sch["est_rows"], sch["tables"]) == (0, 0, 0) and sch["full_data_bytes"] > 0
    only = run_precheck(server, analyzed, schemas=["audit"])
    assert [t["qname"] for t in only["tables"]] == ["audit.log"]
    assert run_precheck(server, analyzed)["notes"] and "off-peak" in run_precheck(server, analyzed)["notes"][0]


def test_impact_actual_vs_estimated_bytes_for_latest(env, server, tmp_path):
    db = env.db("impactn", "CREATE TABLE t (id int PRIMARY KEY, ts timestamptz NOT NULL, p text);"
                           "INSERT INTO t SELECT g, now() + g * interval '1 s', repeat('x', 100) FROM generate_series(1, 20000) g;"
                           "ANALYZE t;")
    opt = [{"schema": "public", "table": "t", "data": "latest", "order_by": "ts", "limit": 2000}]
    pre = run_precheck(server, db, tables=opt)
    assert pre["impact"]["est_rows"] == 2000
    ev = []
    run_backup(server, db, {"out_dir": str(tmp_path), "tables": opt}, lambda e, d: ev.append((e, d)))
    totals = next(d for e, d in ev if e == "totals")
    assert totals["est_rows"] == 2000 and totals["data_bytes"] == pre["impact"]["data_bytes"]


# ------------------------------------------------------------------ GRANT ALL

ACL_SQL = """
SELECT 'rel', c.oid::regclass::text, coalesce(pg_get_userbyid(a.grantee), 'PUBLIC'), a.privilege_type, a.is_grantable
  FROM pg_class c, aclexplode(c.relacl) a WHERE c.relnamespace::regnamespace::text !~ '^pg_|information_schema'
UNION ALL SELECT 'schema', n.nspname, coalesce(pg_get_userbyid(a.grantee), 'PUBLIC'), a.privilege_type, a.is_grantable
  FROM pg_namespace n, aclexplode(n.nspacl) a WHERE n.nspname !~ '^pg_|information_schema'
UNION ALL SELECT 'func', p.oid::regprocedure::text, coalesce(pg_get_userbyid(a.grantee), 'PUBLIC'), a.privilege_type, a.is_grantable
  FROM pg_proc p, aclexplode(p.proacl) a WHERE p.pronamespace::regnamespace::text !~ '^pg_|information_schema'
UNION ALL SELECT 'type', t.oid::regtype::text, coalesce(pg_get_userbyid(a.grantee), 'PUBLIC'), a.privilege_type, a.is_grantable
  FROM pg_type t, aclexplode(t.typacl) a WHERE t.typnamespace::regnamespace::text !~ '^pg_|information_schema'
UNION ALL SELECT 'col', a2.attrelid::regclass::text || '.' || a2.attname, coalesce(pg_get_userbyid(a.grantee), 'PUBLIC'), a.privilege_type, a.is_grantable
  FROM pg_attribute a2, aclexplode(a2.attacl) a WHERE a2.attrelid::regclass::text !~ '^(pg_|information_schema)'
ORDER BY 1, 2, 3, 4
"""

GRANT_ALL_SQL = """
CREATE SCHEMA gs; CREATE TABLE gs.t (a int, b int); CREATE SEQUENCE gs.q;
CREATE FUNCTION gs.f() RETURNS int LANGUAGE sql AS 'SELECT 1';
CREATE DOMAIN gs.d AS int; CREATE TYPE gs.e AS ENUM ('x');
CREATE VIEW gs.v AS SELECT a FROM gs.t;
GRANT ALL ON SCHEMA gs TO {all}; GRANT USAGE ON SCHEMA gs TO {six}, {one}, {go};
GRANT ALL ON gs.t TO {all}; GRANT ALL ON gs.t TO {go} WITH GRANT OPTION;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON gs.t TO {six};   -- everything but MAINTAIN
GRANT SELECT ON gs.t TO {one} WITH GRANT OPTION; GRANT INSERT ON gs.t TO {one};
GRANT ALL ON gs.t TO PUBLIC;
GRANT SELECT (a), UPDATE (b) ON gs.t TO {one};
GRANT ALL ON gs.q TO {all}; GRANT USAGE ON gs.q TO {one}; GRANT SELECT, UPDATE ON gs.q TO {six};
GRANT ALL ON FUNCTION gs.f() TO {all}; REVOKE EXECUTE ON FUNCTION gs.f() FROM PUBLIC;
GRANT ALL ON TYPE gs.e TO {all}; GRANT ALL ON DOMAIN gs.d TO {go} WITH GRANT OPTION;
GRANT ALL ON gs.v TO {all};
REVOKE UPDATE, TRIGGER ON gs.t FROM CURRENT_USER;   -- the owner holds a partial set
"""


def test_grant_all_fixture_roundtrip(env, server, analyzed, tmp_path):
    res = run_backup(server, analyzed, {"out_dir": str(tmp_path)})
    text = open(res["file"], encoding="utf-8").read()
    assert 'GRANT ALL ON TABLE app.customers TO "postgres";' in text and "MAINTAIN" not in text
    target = env.db("grantall_fx")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    assert schema_lines(target) == schema_lines(analyzed)
    with connect(analyzed) as a, connect(target) as b:
        got, want = b.execute(ACL_SQL).fetchall(), a.execute(ACL_SQL).fetchall()
    assert got == want and len(want) > 50
    with connect(analyzed) as a, connect(target) as b:
        q = "SELECT oid::regclass::text, relacl::text FROM pg_class WHERE relnamespace::regnamespace::text IN ('app','audit','sales data') ORDER BY 1"
        assert a.execute(q).fetchall() == b.execute(q).fetchall()


def test_grant_all_only_when_full_set_is_held(env, server, tmp_path):
    names = {k: env.role("ga_" + k)[0] for k in ("all", "six", "one", "go")}
    db = env.db("grantall", GRANT_ALL_SQL.format(**{k: f'"{v}"' for k, v in names.items()}))
    res = run_backup(server, db, {"out_dir": str(tmp_path), "schemas": ["gs"]})
    text = open(res["file"], encoding="utf-8").read()
    a, six, one, go = (names[k] for k in ("all", "six", "one", "go"))
    assert f'GRANT ALL ON TABLE gs.t TO "{a}";' in text
    assert f'GRANT ALL ON TABLE gs.t TO "{go}" WITH GRANT OPTION;' in text
    assert "GRANT ALL ON TABLE gs.t TO PUBLIC;" in text
    assert f'GRANT ALL ON TABLE gs.t TO "{six}"' not in text           # lacks MAINTAIN on PG 17+
    assert f'GRANT ALL ON TABLE gs.t TO "{one}"' not in text
    assert f'GRANT ALL ON SEQUENCE gs.q TO "{a}";' in text and f'GRANT ALL ON SEQUENCE gs.q TO "{one}"' not in text
    assert f'GRANT ALL ON SCHEMA "gs" TO "{a}";' in text
    assert f'GRANT ALL ON FUNCTION gs.f() TO "{a}";' in text
    assert re.search(rf'GRANT ALL ON (TYPE|DOMAIN) gs.d TO "{go}" WITH GRANT OPTION;', text)
    assert f'GRANT ALL ON TYPE gs.e TO "{a}";' in text
    # MAINTAIN (PG 17+) shows up only where it is part of a partial set (the owner's), never in a GRANT ALL
    # line; such a partial grant would fail on a PG 12-16 target (QA report limitation L3)
    assert [ln for ln in text.splitlines() if "MAINTAIN" in ln] ==         [f'GRANT INSERT, SELECT, DELETE, TRUNCATE, REFERENCES, MAINTAIN ON TABLE gs.t TO "{server["user"]}";']
    target = env.db("grantall_r")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    with connect(db) as s, connect(target) as t:
        want = s.execute(ACL_SQL).fetchall()
        assert t.execute(ACL_SQL).fetchall() == want
    assert any(r[1] == "gs.t" and r[3] == "MAINTAIN" for r in want)  # sanity: PG18 ALL includes MAINTAIN
    # owner (current user) lost UPDATE and TRIGGER and must still lack them after restore
    with connect(target) as t:
        assert not t.execute("SELECT has_table_privilege('gs.t', 'UPDATE') OR has_table_privilege('gs.t', 'TRIGGER')"
                             ).fetchone()[0] or t.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                                                          ).fetchone()[0]
        owner_items = [r for r in t.execute(
            "SELECT a.privilege_type FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = 'gs.t'::regclass"
            " AND a.grantee = c.relowner")]
        assert ("UPDATE",) not in owner_items and ("TRIGGER",) not in owner_items and ("SELECT",) in owner_items
