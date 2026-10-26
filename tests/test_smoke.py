"""Step 0 smoke tests: imports, Flask app, fixture loads, fixture coverage, skip behaviour."""
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT, connect, run_psql

MODULES = ["app", "jobs", "store", "engine", "engine.util", "engine.db", "engine.precheck",
           "engine.catalog", "engine.backup", "engine.restore"]


@pytest.mark.parametrize("mod", MODULES)
def test_module_imports(mod):
    importlib.import_module(mod)


def test_cancelled_exception():
    from engine.util import Cancelled
    assert issubclass(Cancelled, Exception)


def test_flask_routes():
    from app import create_app
    c = create_app().test_client()
    r = c.get("/")
    assert r.status_code == 200 and b"<" in r.data
    r.close()
    assert c.get("/x").status_code == 404


def test_main_binds_localhost_only():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '"127.0.0.1"' in src and "0.0.0.0" not in src


# ---------------- DB tests ----------------

@pytest.fixture(scope="module")
def sdb(src_db):
    conn = connect(src_db)
    yield conn
    conn.close()


def q1(conn, sql, *params):
    return conn.execute(sql, params or None).fetchone()[0]


def test_fixture_loads_clean(src_db):
    r = run_psql(src_db, "-c", "select 1")
    assert r.returncode == 0 and r.stderr == ""


def test_fixture_is_utf8(sdb):
    assert q1(sdb, "select pg_encoding_to_char(encoding) from pg_database where datname = current_database()") == "UTF8"


COUNT_CHECKS = {
    "extension citext": ("select count(*) from pg_extension where extname='citext'", 1),
    "extension btree_gist": ("select count(*) from pg_extension where extname='btree_gist'", 1),
    "citext column": ("select count(*) from pg_attribute a join pg_type t on t.oid=a.atttypid where t.typname='citext' and a.attrelid='app.customers'::regclass", 1),
    "exclusion constraint": ("select count(*) from pg_constraint where contype='x'", 1),
    "schemas": ("select count(*) from pg_namespace where nspname in ('app','sales data','audit')", 3),
    "enum": ("select count(*) from pg_type where typtype='e' and typnamespace='app'::regnamespace", 1),
    "domain w/ check": ("select count(*) from pg_constraint where contypid='app.posint'::regtype and contype='c'", 1),
    "composite type": ("select count(*) from pg_type where typtype='c' and typname='address' and typnamespace='app'::regnamespace", 1),
    "range type": ("select count(*) from pg_type where typtype='r' and typname='floatrange'", 1),
    "standalone sequence": ("select count(*) from pg_sequence s join pg_class c on c.oid=s.seqrelid where c.relname='ticket_seq' and s.seqincrement=5 and s.seqcycle and s.seqcache=3", 1),
    "identity always": ("select count(*) from pg_attribute where attidentity='a' and attrelid='app.ident_always'::regclass", 1),
    "identity by default w/ options": ("select count(*) from pg_attribute a join pg_sequence s on s.seqrelid = pg_get_serial_sequence('app.ident_default','id')::regclass where a.attidentity='d' and a.attrelid='app.ident_default'::regclass and s.seqstart=1000 and s.seqincrement=10", 1),
    "serial column": ("select count(*) from pg_attrdef d where d.adrelid='app.customers'::regclass and pg_get_expr(d.adbin,d.adrelid) like 'nextval(%'", 1),
    "stored generated column": ("select count(*) from pg_attribute where attgenerated='s' and attrelid='app.customers'::regclass", 1),
    "collate C column": ("select count(*) from pg_attribute a join pg_collation c on c.oid=a.attcollation where a.attrelid='app.customers'::regclass and a.attname='name' and c.collname='C'", 1),
    "partitioned table": ("select count(*) from pg_class where oid='\"sales data\".orders'::regclass and relkind='p'", 1),
    "partitions incl default": ("select count(*) from pg_inherits where inhparent='\"sales data\".orders'::regclass", 3),
    "default partition": ("select count(*) from pg_class where relname='orders_default' and relispartition and pg_get_expr(relpartbound,oid)='DEFAULT'", 1),
    "orders rows ~5000": ("select count(*) from \"sales data\".orders", 5000),
    "inheritance child": ("select count(*) from pg_inherits where inhparent='app.vehicle'::regclass and inhrelid='app.car'::regclass", 1),
    "unlogged w/ fillfactor": ("select count(*) from pg_class where oid='app.scratch'::regclass and relpersistence='u' and reloptions @> array['fillfactor=70']", 1),
    "self-referencing FK": ("select count(*) from pg_constraint where contype='f' and conrelid=confrelid and conrelid='app.customers'::regclass", 1),
    "FK to partitioned table": ("select count(*) from pg_constraint where contype='f' and conrelid='\"sales data\".\"order lines\"'::regclass and confrelid='\"sales data\".orders'::regclass", 1),
    "plpgsql function": ("select count(*) from pg_proc where proname='add_plpgsql' and prolang=(select oid from pg_language where lanname='plpgsql')", 1),
    "sql function": ("select count(*) from pg_proc where proname='add_sql' and prolang=(select oid from pg_language where lanname='sql')", 1),
    "row-type arg function": ("select count(*) from pg_proc where proname='customer_label' and proargtypes[0]='app.customers'::regtype", 1),
    "atomic function": ("select count(*) from pg_proc where proname='atomic_double'", 1),
    "procedure": ("select count(*) from pg_proc where proname='add_note' and prokind='p'", 1),
    "aggregate w/ initcond": ("select count(*) from pg_aggregate where aggfnoid='app.sum_squares(integer)'::regprocedure and agginitval='0'", 1),
    "view B depends on A": ("select count(*) from pg_depend d join pg_rewrite r on r.oid=d.objid where d.refobjid='app.view_a'::regclass and r.ev_class='app.view_b'::regclass", 1),
    "matview": ("select count(*) from pg_class where oid='\"sales data\".monthly_totals'::regclass and relkind='m'", 1),
    "matview unique index": ("select count(*) from pg_index where indrelid='\"sales data\".monthly_totals'::regclass and indisunique", 1),
    "enabled trigger": ("select count(*) from pg_trigger where tgname='customers_audit' and tgenabled='O' and not tgisinternal", 1),
    "disabled trigger": ("select count(*) from pg_trigger where tgname='customers_audit_disabled' and tgenabled='D'", 1),
    "rule": ("select count(*) from pg_rewrite where rulename='notes_log'", 1),
    "RLS enabled": ("select count(*) from pg_class where oid='app.secrets'::regclass and relrowsecurity", 1),
    "policy": ("select count(*) from pg_policy where polname='secrets_owner'", 1),
    "comment with quotes": ("select count(*) from pg_description where description like '%''%' and description like '%\"%'", 3),
    "grants to reader": ("select count(*) from information_schema.table_privileges where grantee='pgbs_test_reader' and privilege_type='SELECT'", 1),
    "empty table": ("select count(*) from app.empty_table", 0),
    "zero-column table": ("select count(*) from pg_attribute where attrelid='app.zero_cols'::regclass and attnum>0", 0),
    "identifier with space/reserved": ("select count(*) from pg_attribute where attrelid='app.\"Weird Table\"'::regclass and attname in ('select','from col','Mixed\"Quote')", 3),
}


@pytest.mark.parametrize("name", list(COUNT_CHECKS))
def test_fixture_coverage(sdb, name):
    sql, expected = COUNT_CHECKS[name]
    got = q1(sdb, sql)
    if name in ("grants to reader", "comment with quotes", "exclusion constraint", "enabled trigger", "view B depends on A"):
        assert got >= expected, f"{name}: got {got}"
    else:
        assert got == expected, f"{name}: got {got}, expected {expected}"


def test_fixture_edge_data(sdb):
    rows = {r[0]: r for r in sdb.execute("select id, t, b from app.edge order by id").fetchall()}
    assert "\t" in rows[1][1] and b"\x00" in bytes(rows[1][2])
    assert "\n" in rows[2][1]
    assert "\\" in rows[3][1] and "\\N" in rows[3][1]
    assert rows[4][1] is None and rows[4][2] is None
    assert rows[8][1] == "NULL"
    assert "日本語" in rows[6][1]
    assert q1(sdb, "select count(*) from app.edge where b = '\\x00'::bytea") == 1


def test_fixture_orders_span_partitions_and_default(sdb):
    for part in ("orders_2024q1", "orders_2024q2", "orders_default"):
        assert q1(sdb, f'select count(*) from "sales data".{part}') > 0, part


def test_fixture_deterministic_values(sdb):
    assert q1(sdb, "select md5(string_agg(o::text, '|' order by id)) from \"sales data\".orders o") is not None
    assert q1(sdb, "select last_value from app.customers_id_seq") == 77
    assert q1(sdb, "select last_value from app.ticket_seq") == 150
    assert q1(sdb, "select app.atomic_double(21)") == 42
    assert q1(sdb, "select app.sum_squares(g) from generate_series(1,3) g") == 14


def test_fixture_source_has_no_nondeterminism():
    text = (Path(__file__).parent / "fixtures" / "sample_db.sql").read_text(encoding="utf-8").lower()
    text = " ".join(l for l in text.splitlines() if not l.startswith("--"))
    assert "random()" not in text and "now()" not in text and "current_timestamp" not in text


# ---------------- restore factory guard & skip behaviour ----------------

def test_drop_guard_refuses_non_test_db(pg_admin):
    from conftest import drop_database
    for bad in ("postgres", "template1", "mydb", "pgbs_test_x; drop database postgres"):
        with pytest.raises(RuntimeError):
            drop_database(pg_admin, bad)


def test_restore_db_factory_naming(restore_db):
    assert restore_db("abc") == "pgbs_test_restore_abc"


def test_wrong_password_skips_not_errors():
    env = dict(os.environ, PGPASSWORD="definitely-wrong-password", PGPASSFILE=os.devnull)
    # If the server trusts us regardless of password, the premise cannot be tested.
    try:
        import psycopg
        s = {k: os.environ.get(k) for k in ("PGHOST", "PGPORT", "PGUSER")}
        psycopg.connect(host=s["PGHOST"] or "localhost", port=int(s["PGPORT"] or 5432),
                        user=s["PGUSER"] or "postgres", password="definitely-wrong-password",
                        dbname="postgres", connect_timeout=5).close()
        pytest.skip("server accepts any password (trust auth); cannot test skip path")
    except pytest.skip.Exception:
        raise
    except Exception:
        pass
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider",
         str(Path(__file__).resolve()), "-k", "test_fixture_loads_clean"],
        cwd=str(ROOT), env=env, capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "1 skipped" in out and " error" not in out.lower() and "failed" not in out.lower(), out
    assert "not reachable" in out
