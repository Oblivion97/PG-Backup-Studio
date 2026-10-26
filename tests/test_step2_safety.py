"""QA tests for Step 2 - safety: read-only login, no writes, lock timeout, up-front locks, cancel.

Real PostgreSQL only. Scratch DBs/roles pgbs_test_qa_s2_* are dropped by the `env` fixture.
"""
import os
import threading
import time

import psycopg
import pytest

from conftest import FIXTURE_SQL, connect, run_psql
from engine import backup as backup_mod
from engine.backup import run_backup
from engine.precheck import run_precheck
from engine.restore import run_restore
from engine.util import BackupBlockedError, BackupError, Cancelled, LockTimeoutError
from step1_helpers import fixture_state, nextvals, schema_lines
from step2_helpers import (Env, app_backends, connect_as, counters, files, hold, in_thread, release,
                           server_dict)


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


@pytest.fixture
def fx(env, analyzed):
    """A private, mutable copy of the rich sample DB (roles may be granted on it)."""
    name = env.db("fx")
    r = run_psql(name, "-f", str(FIXTURE_SQL))
    assert r.returncode == 0, r.stderr
    with connect(name) as c:
        c.execute("ANALYZE")
    return name


def problems_of(pre):
    return sorted((p["kind"], p["qname"]) for p in pre["problems"])


# ------------------------------------------------------------------ 1. read-only logins

def test_ro_pg_read_all_data_blocked_then_partial_roundtrip(env, server, analyzed, tmp_path):
    ro, pw = env.role("ro14", "IN ROLE pg_read_all_data")
    srv = dict(server, user=ro, password=pw)
    with connect_as(ro, pw, analyzed) as c:  # the login really is read-only
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO app.notes (body) VALUES ('x')")
    pre = run_precheck(srv, analyzed)
    assert pre["blocking"] and problems_of(pre) == [("rls", "app.secrets")]
    assert "row-level security" in pre["problems"][0]["message"]
    assert pre["problems"][0]["fixes"] == ["exclude_schema", "schema_only"]
    assert pre["server"]["read_all_data"] and not pre["server"]["bypassrls"] and not pre["server"]["is_superuser"]
    assert run_precheck(srv, analyzed, mode="schema")["problems"] == []
    assert run_precheck(srv, analyzed, mode="data")["blocking"]
    with pytest.raises(BackupBlockedError) as ei:
        run_backup(srv, analyzed, {"out_dir": str(tmp_path)})
    assert "app.secrets" in str(ei.value) and [p["kind"] for p in ei.value.problems] == ["rls"]
    assert files(tmp_path) == []  # no .part, no final file
    time.sleep(0.3)
    assert app_backends(analyzed) == 0

    opt = [{"schema": "app", "table": "secrets", "data": "schema"}]
    assert not run_precheck(srv, analyzed, tables=opt)["blocking"]
    res = run_backup(srv, analyzed, {"out_dir": str(tmp_path), "tables": opt})
    assert res["partial"] is True and "_partial" in os.path.basename(res["file"])
    assert [(t["table"], t["data"]) for t in res["partial_tables"]] == [("app.secrets", "schema")]
    target = env.db("ro14_restore")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    assert schema_lines(target) == schema_lines(analyzed)
    got, want = fixture_state(target), fixture_state(analyzed)
    assert got["counts"].pop("app.secrets") == 0 and want["counts"].pop("app.secrets") == 3
    assert got == want  # counts, md5s, sequence state, triggers, matview


def test_ro_with_bypassrls_reads_rls_table(env, server, analyzed, tmp_path):
    ro, pw = env.role("ro_bypass", "BYPASSRLS IN ROLE pg_read_all_data")
    srv = dict(server, user=ro, password=pw)
    pre = run_precheck(srv, analyzed)
    assert not pre["blocking"] and pre["server"]["bypassrls"]
    res = run_backup(srv, analyzed, {"out_dir": str(tmp_path)})
    assert not res["partial"]
    target = env.db("bypass_restore")
    assert run_restore(server, target, res["file"])["error_count"] == 0
    assert fixture_state(target) == fixture_state(analyzed)  # all 3 secrets rows came along


def test_rls_owner_ok_but_force_rls_blocks(env, server, tmp_path):
    owner, pw = env.role("rlsowner")
    db = env.db("rlsowner", f"""
        CREATE SCHEMA s AUTHORIZATION "{owner}";
        CREATE TABLE s.t (id int, who text); ALTER TABLE s.t OWNER TO "{owner}";
        INSERT INTO s.t VALUES (1, 'a'), (2, 'b'), (3, 'c');
        ALTER TABLE s.t ENABLE ROW LEVEL SECURITY;
        CREATE POLICY only_a ON s.t USING (who = 'a');""")
    srv = dict(server, user=owner, password=pw)
    assert run_precheck(srv, db, schemas=["s"])["problems"] == []
    res = run_backup(srv, db, {"out_dir": str(tmp_path), "schemas": ["s"]})
    assert res["rows"] == 3  # the owner is exempt: nothing filtered
    with connect(db) as c:
        c.execute("ALTER TABLE s.t FORCE ROW LEVEL SECURITY")
    pre = run_precheck(srv, db, schemas=["s"])
    assert problems_of(pre) == [("rls", "s.t")]
    with pytest.raises(BackupBlockedError):
        run_backup(srv, db, {"out_dir": str(tmp_path / "x"), "schemas": ["s"]})
    assert files(tmp_path / "x") == []


def _grant_12_style(db, role, extra=""):
    with connect(db) as c:
        c.execute(f'GRANT CONNECT ON DATABASE "{db}" TO "{role}"')
        for s in ("public", "app", '"sales data"', "audit"):
            c.execute(f'GRANT USAGE ON SCHEMA {s} TO "{role}"')
            c.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO "{role}"')
            c.execute(f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA {s} TO "{role}"')
        if extra:
            c.execute(extra)


def test_pg12_style_role_connect_usage_select(env, server, fx, tmp_path):
    ro, pw = env.role("ro12")
    _grant_12_style(fx, ro)
    srv = dict(server, user=ro, password=pw)
    pre = run_precheck(srv, fx)
    assert problems_of(pre) == [("rls", "app.secrets")] and not pre["server"]["read_all_data"]
    with pytest.raises(BackupBlockedError, match="app.secrets"):
        run_backup(srv, fx, {"out_dir": str(tmp_path)})
    assert files(tmp_path) == []
    opt = [{"schema": "app", "table": "secrets", "data": "schema"}]
    res = run_backup(srv, fx, {"out_dir": str(tmp_path), "tables": opt})
    assert res["partial"]
    target = env.db("ro12_restore")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    got, want = fixture_state(target), fixture_state(fx)
    assert got["counts"].pop("app.secrets") == 0 and want["counts"].pop("app.secrets") == 3
    assert got == want


def test_partial_grants_every_missing_item_listed_once(env, server, fx, tmp_path):
    role, pw = env.role("partial12")
    _grant_12_style(fx, role)
    with connect(fx) as c:
        c.execute(f'REVOKE USAGE ON SCHEMA audit FROM "{role}"')
        c.execute(f'REVOKE SELECT ON ALL TABLES IN SCHEMA audit FROM "{role}"')
        c.execute(f'REVOKE SELECT ON ALL SEQUENCES IN SCHEMA audit FROM "{role}"')
        c.execute(f'REVOKE SELECT ON app.notes FROM "{role}"')
        c.execute(f'REVOKE SELECT ON app.edge FROM "{role}"; GRANT SELECT (id) ON app.edge TO "{role}"')
        c.execute(f'REVOKE SELECT ON app.ticket_seq FROM "{role}"')
        # all columns granted one by one counts as readable
        c.execute(f'REVOKE SELECT ON app.empty_table FROM "{role}"; GRANT SELECT (id, x) ON app.empty_table TO "{role}"')
    srv = dict(server, user=role, password=pw)
    pre = run_precheck(srv, fx)
    want = sorted([("schema_usage", "audit"), ("table_select", "app.notes"), ("table_select", "app.edge"),
                   ("table_select", "audit.log"), ("rls", "app.secrets"), ("sequence_select", "app.ticket_seq"),
                   ("sequence_select", "audit.log_id_seq")])
    assert problems_of(pre) == want and len(pre["problems"]) == len(want)
    for p in pre["problems"]:
        assert p["qname"] in p["message"]
        assert p["fixes"] == (["exclude_schema", "schema_only"] if p["kind"] in ("table_select", "rls")
                              else ["exclude_schema"])
    assert run_precheck(srv, fx, mode="schema")["problems"] == []
    with pytest.raises(BackupBlockedError) as ei:
        run_backup(srv, fx, {"out_dir": str(tmp_path)})
    assert len(ei.value.problems) == len(want) and files(tmp_path) == []

    # "schema only" fixes the table ones (audit then has nothing to read, so its missing USAGE no longer
    # matters); the standalone sequence can only be fixed by a grant or by excluding its schema
    tabs = [{"schema": s, "table": t, "data": "schema"}
            for s, t in (("app", "notes"), ("app", "edge"), ("audit", "log"), ("app", "secrets"))]
    pre = run_precheck(srv, fx, tables=tabs)
    assert problems_of(pre) == [("sequence_select", "app.ticket_seq")]
    assert pre["problems"][0]["fixes"] == ["exclude_schema"]
    with connect(fx) as c:  # the user fixes the remaining one with a grant
        c.execute(f'GRANT SELECT ON app.ticket_seq TO "{role}"')
    assert run_precheck(srv, fx, tables=tabs)["problems"] == []
    res = run_backup(srv, fx, {"out_dir": str(tmp_path), "tables": tabs})
    assert res["partial"] and len(res["partial_tables"]) == 4
    assert any("audit.log_id_seq" in w and "not saved" in w for w in res["warnings"])
    target = env.db("partial12_restore")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    with connect(target) as c:
        for t in ("app.notes", "app.edge", "audit.log", "app.secrets"):
            assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM app.customers").fetchone()[0] == 50


def test_schema_mode_needs_no_privileges(env, server, analyzed, tmp_path):
    nobody, pw = env.role("nobody")  # only the default CONNECT/PUBLIC rights
    srv = dict(server, user=nobody, password=pw)
    pre = run_precheck(srv, analyzed, mode="schema")
    assert pre["problems"] == [] and not pre["blocking"]
    res = run_backup(srv, analyzed, {"out_dir": str(tmp_path), "mode": "schema"})
    target = env.db("nobody_restore")
    rr = run_restore(server, target, res["file"])
    assert rr["error_count"] == 0, rr["errors"]
    assert schema_lines(target) == schema_lines(analyzed)
    assert run_precheck(srv, analyzed)["blocking"]  # but data needs rights


def test_full_backup_ro_without_rls_roundtrip(env, server, fx, tmp_path):
    """No RLS table selected: a pg_read_all_data login backs up everything and the round trip is exact."""
    ro, pw = env.role("ro_norls", "IN ROLE pg_read_all_data")
    with connect(fx) as c:
        c.execute("DROP TABLE app.secrets")
    srv = dict(server, user=ro, password=pw)
    assert run_precheck(srv, fx)["problems"] == []
    res = run_backup(srv, fx, {"out_dir": str(tmp_path)})
    assert not res["partial"] and "_partial" not in res["file"]
    target = env.db("norls_restore")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    assert schema_lines(target) == schema_lines(fx)
    assert fixture_state(target) == fixture_state(fx)
    assert nextvals(target) == nextvals(fx)
    with connect(target) as c:
        assert c.execute("SELECT app.add_plpgsql(2, 3), app.atomic_double(4)").fetchone() == (5, 8)
        assert c.execute("SELECT count(*) FROM app.view_b").fetchone()[0] > 0


# ------------------------------------------------------------------ 2. no writes on the source

def test_no_writes_across_precheck_backup_failure_cancel(env, server, analyzed, tmp_path):
    ro, pw = env.role("ro_nw", "IN ROLE pg_read_all_data")
    srv = dict(server, user=ro, password=pw)
    tabs = [{"schema": "sales data", "table": "orders", "data": "latest", "order_by": "ordered_at", "limit": 1000},
            {"schema": "app", "table": "secrets", "data": "schema"}]
    time.sleep(1.5)
    before = counters(analyzed)
    run_precheck(server, analyzed)
    run_precheck(srv, analyzed, tables=tabs)
    run_backup(server, analyzed, {"out_dir": str(tmp_path)})
    run_backup(srv, analyzed, {"out_dir": str(tmp_path), "tables": tabs, "gzip": True})
    h = hold(analyzed, "app.edge")
    try:
        with pytest.raises(LockTimeoutError):
            run_backup(server, analyzed, {"out_dir": str(tmp_path / "f"), "lock_timeout_s": 1})
    finally:
        release(h)
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        run_backup(server, analyzed, {"out_dir": str(tmp_path / "c")}, None, ev)
    time.sleep(1.5)
    after = counters(analyzed)
    for k in ("tup_inserted", "tup_updated", "tup_deleted", "temp_files", "temp_bytes", "tables"):
        assert after[k] == before[k], (k, before, after)
    assert after["xact_commit"] > before["xact_commit"]  # reads do commit transactions; that is not a write


def test_backup_session_is_read_only(analyzed, tmp_path, monkeypatch, server):
    """Every statement of the backup runs in a READ ONLY transaction; a write attempt would fail."""
    seen = {}
    orig = backup_mod._BackupRun._plan

    def spy(self, conn, version, skipped):
        seen["ro"] = conn.execute("SHOW transaction_read_only").fetchone()[0]
        seen["iso"] = conn.execute("SHOW transaction_isolation").fetchone()[0]
        seen["rowsec"] = conn.execute("SHOW row_security").fetchone()[0]
        try:
            conn.execute("CREATE TABLE pgbs_test_must_not_exist (id int)")
            seen["wrote"] = True
        except psycopg.errors.ReadOnlySqlTransaction:
            seen["wrote"] = False
            conn.rollback()
            raise
        return orig(self, conn, version, skipped)

    monkeypatch.setattr(backup_mod._BackupRun, "_plan", spy)
    with pytest.raises(Exception):
        run_backup(server, analyzed, {"out_dir": str(tmp_path)})
    assert seen == {"ro": "on", "iso": "repeatable read", "rowsec": "off", "wrote": False}
    assert files(tmp_path) == []


# ------------------------------------------------------------------ 3. lock timeout

@pytest.mark.parametrize("table", ['"sales data".orders', '"sales data".orders_default', "app.customers",
                                   "app.vehicle", "app.zero_cols", "audit.log"])
def test_lock_timeout_names_table_no_part_file(analyzed, server, tmp_path, table):
    h = hold(analyzed, table)
    try:
        t0 = time.monotonic()
        with pytest.raises(LockTimeoutError) as ei:
            run_backup(server, analyzed, {"out_dir": str(tmp_path), "lock_timeout_s": 1.5})
        took = time.monotonic() - t0
        assert 1.3 <= took < 4.5, took
        # LOCK on a parent also locks its partitions / inheritance children; the first one hit is named
        family = {'"sales data".orders': '"sales data".orders_', "app.vehicle": "app.car"}.get(table)
        assert ei.value.relation == table or (family and ei.value.relation.startswith(family)), ei.value.relation
        assert ei.value.relation in str(ei.value)
        assert "Nothing was written" in str(ei.value)
        assert files(tmp_path) == []
        t0 = time.monotonic()
        pre = run_precheck(server, analyzed)
        assert time.monotonic() - t0 < 5
        got = [(lk["qname"], lk["granted"], lk["mode"]) for lk in pre["locks"]]
        assert (table, True, "AccessExclusiveLock") in got and all(g[1:] == (True, "AccessExclusiveLock") for g in got)
        assert len(got) == len({g[0] for g in got})
        assert pre["tables"]  # the pre-check still lists everything
        assert not pre["blocking"]
    finally:
        release(h)
    time.sleep(0.3)
    assert app_backends(analyzed) == 0
    # and once the lock is gone the same backup works
    assert run_backup(server, analyzed, {"out_dir": str(tmp_path), "lock_timeout_s": 1.5})["tables"] > 10


def test_default_lock_timeout_is_10s(analyzed, server, tmp_path):
    assert backup_mod.BackupOptions().lock_timeout_s == 10
    h = hold(analyzed, "app.edge")
    try:
        t0 = time.monotonic()
        with pytest.raises(LockTimeoutError, match="10 s"):
            run_backup(server, analyzed, {"out_dir": str(tmp_path)})
        assert 9.5 <= time.monotonic() - t0 < 13
    finally:
        release(h)
    assert files(tmp_path) == []


@pytest.mark.parametrize("mode", ["ACCESS SHARE", "ROW SHARE", "ROW EXCLUSIVE", "SHARE UPDATE EXCLUSIVE", "SHARE",
                                  "SHARE ROW EXCLUSIVE", "EXCLUSIVE"])
def test_only_access_exclusive_blocks(analyzed, server, tmp_path, mode):
    """Writers, VACUUM, CREATE INDEX etc. must not stop a backup: only ACCESS EXCLUSIVE conflicts."""
    h = hold(analyzed, "app.customers", mode) if mode != "EXCLUSIVE" else hold(analyzed, "app.edge", mode)
    try:
        t0 = time.monotonic()
        res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "lock_timeout_s": 1.5})
        assert time.monotonic() - t0 < 6 and res["tables"] > 10
    finally:
        release(h)


def test_lock_outside_selected_schemas_is_ignored(analyzed, server, tmp_path):
    h = hold(analyzed, "audit.log")
    try:
        res = run_backup(server, analyzed, {"out_dir": str(tmp_path), "schemas": ["sales data"], "lock_timeout_s": 1})
        assert res["schemas"] == ["sales data"]
    finally:
        release(h)


def test_lock_on_schema_only_table_still_times_out(analyzed, server, tmp_path):
    """Structure is read from the catalog, so even a schema-only table is locked first (documented design)."""
    h = hold(analyzed, "app.notes")
    try:
        with pytest.raises(LockTimeoutError, match="app.notes"):
            run_backup(server, analyzed, {"out_dir": str(tmp_path), "lock_timeout_s": 1,
                                          "tables": [{"schema": "app", "table": "notes", "data": "schema"}]})
    finally:
        release(h)
    assert files(tmp_path) == []


def test_lock_wait_zero_waits_until_released(analyzed, server, tmp_path):
    h = hold(analyzed, "app.edge")
    th, out = in_thread(lambda: run_backup(server, analyzed, {"out_dir": str(tmp_path), "lock_timeout_s": 0}))
    time.sleep(2.0)
    assert th.is_alive() and files(tmp_path) == []  # still waiting, nothing written
    release(h)
    th.join(30)
    assert "result" in out, out
    assert out["result"]["rows"] > 0
    assert [f for f in files(tmp_path) if f.endswith(".part")] == []


def test_lock_on_view_and_matview_do_not_hang(env, server, tmp_path):
    db = env.db("lockobjs", "CREATE TABLE t (id int); INSERT INTO t VALUES (1),(2);"
                            "CREATE VIEW v AS SELECT * FROM t; CREATE MATERIALIZED VIEW mv AS SELECT * FROM t;")
    for obj in ("v", "mv"):
        try:
            h = hold(db, obj)
        except psycopg.errors.Error:
            continue  # not lockable (matview)
        try:
            t0 = time.monotonic()
            try:
                run_backup(server, db, {"out_dir": str(tmp_path / obj), "lock_timeout_s": 1.5})
            except LockTimeoutError:
                pass
            assert time.monotonic() - t0 < 5
        finally:
            release(h)
        assert files(tmp_path / obj) in ([], [f for f in files(tmp_path / obj) if f.endswith(".sql")])


# ------------------------------------------------------------------ 4. up-front locks (before the snapshot)

DDL_OPS = {
    "truncate": "TRUNCATE {t}",
    "alter_type": "ALTER TABLE {t} ALTER COLUMN v TYPE text",
    "drop": "DROP TABLE {t}",
    "vacuum_full": "VACUUM FULL {t}",
    "cluster": "CLUSTER {t} USING {t}_pkey",
    "rename": "ALTER TABLE {t} RENAME TO {t}_x",
    "add_column_default": "ALTER TABLE {t} ADD COLUMN w int DEFAULT random()::int",
}


@pytest.mark.parametrize("op", sorted(DDL_OPS))
def test_ddl_before_snapshot_waits_and_times_out(env, server, tmp_path, monkeypatch, op):
    db = env.db("upfront_" + op, "".join(
        f"CREATE TABLE t{i} (id int PRIMARY KEY, v int); INSERT INTO t{i} SELECT g, g FROM generate_series(1, 2000) g;"
        for i in (1, 2, 3)))
    attempts = {}
    orig = backup_mod.catalog.load_all

    def hooked(conn, schemas, version, **kw):
        # first query that takes the snapshot: every table must already be locked by the backup
        for t in ("t1", "t2", "t3"):
            with connect(db) as other:
                other.execute("SET lock_timeout = '300ms'")
                try:
                    other.execute(DDL_OPS[op].format(t=t))
                    attempts[t] = "done"
                except psycopg.Error as e:
                    attempts[t] = e.sqlstate
        return orig(conn, schemas, version, **kw)

    monkeypatch.setattr(backup_mod.catalog, "load_all", hooked)
    res = run_backup(server, db, {"out_dir": str(tmp_path)})
    assert attempts == {"t1": "55P03", "t2": "55P03", "t3": "55P03"}, attempts
    assert res["rows"] == 6000
    target = env.db("upfront_" + op + "_r")
    assert run_restore(server, target, res["file"])["error_count"] == 0
    with connect(target) as c:
        assert [c.execute(f"SELECT count(*) FROM t{i}").fetchone()[0] for i in (1, 2, 3)] == [2000] * 3
    with connect(db) as c:  # source untouched
        assert [c.execute(f"SELECT count(*) FROM t{i}").fetchone()[0] for i in (1, 2, 3)] == [2000] * 3


def test_dml_during_backup_not_blocked_and_not_seen(env, server, tmp_path):
    db = env.db("dml", "CREATE TABLE a (id int PRIMARY KEY, v text); CREATE TABLE z (id int PRIMARY KEY, v text);"
                       "INSERT INTO a SELECT g, 'a' FROM generate_series(1, 3000) g;"
                       "INSERT INTO z SELECT g, 'z' FROM generate_series(1, 3000) g;")
    done = {}

    def progress(event, data):
        if event == "table_start" and data["name"] == "public.a" and not done:
            t0 = time.monotonic()
            with connect(db) as other:
                other.execute("SET lock_timeout = '2s'")
                other.execute("INSERT INTO z SELECT g, 'new' FROM generate_series(3001, 3500) g")
                other.execute("UPDATE z SET v = 'upd' WHERE id <= 100")
                other.execute("DELETE FROM z WHERE id BETWEEN 200 AND 300")
            done["t"] = time.monotonic() - t0

    res = run_backup(server, db, {"out_dir": str(tmp_path)}, progress)
    assert done["t"] < 2  # writers were not blocked by the backup
    assert res["rows"] == 6000
    target = env.db("dml_r")
    assert run_restore(server, target, res["file"])["error_count"] == 0
    with connect(target) as c:
        assert c.execute("SELECT count(*), count(*) FILTER (WHERE v = 'z') FROM z").fetchone() == (3000, 3000)


# ------------------------------------------------------------------ 5. cancel

def test_cancel_during_lock_wait_fast(analyzed, server, tmp_path):
    for lock_timeout in (0, 30):
        h = hold(analyzed, "app.customers")
        ev = threading.Event()
        try:
            th, out = in_thread(lambda: run_backup(server, analyzed, {"out_dir": str(tmp_path),
                                                                      "lock_timeout_s": lock_timeout}, None, ev))
            time.sleep(1.0)
            assert th.is_alive()
            t0 = time.monotonic()
            ev.set()
            th.join(5)
            stopped = time.monotonic() - t0
        finally:
            release(h)
            th.join(35)
        assert isinstance(out.get("error"), Cancelled), out
        assert stopped < 2, stopped
        assert files(tmp_path) == []
        time.sleep(0.4)
        assert app_backends(analyzed) == 0


def test_cancel_before_start_and_in_precheck(analyzed, server, tmp_path):
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        run_backup(server, analyzed, {"out_dir": str(tmp_path)}, None, ev)
    with pytest.raises(Cancelled):
        run_precheck(server, analyzed, cancel=ev)
    assert files(tmp_path) == []


@pytest.fixture(scope="module")
def big(pg_admin):
    from conftest import drop_database
    name = "pgbs_test_qa_s2_big"
    drop_database(pg_admin, name)
    pg_admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
    with connect(name) as c:
        c.execute("CREATE TABLE big (id int PRIMARY KEY, payload text);"
                  "INSERT INTO big SELECT i, md5(i::text) || md5((i + 1)::text) FROM generate_series(1, 120000) i;")
    yield name
    drop_database(pg_admin, name)


def test_cancel_during_speed_limited_copy(big, server, tmp_path):
    ev = threading.Event()
    th, out = in_thread(lambda: run_backup(server, big, {"out_dir": str(tmp_path), "speed_limit_mb_s": 0.2},
                                           None, ev))
    time.sleep(2.0)
    assert th.is_alive() and any(f.endswith(".part") for f in files(tmp_path))  # mid-copy, .part exists
    t0 = time.monotonic()
    ev.set()
    th.join(5)
    assert time.monotonic() - t0 < 1.0
    assert isinstance(out.get("error"), Cancelled), out
    assert files(tmp_path) == []
    time.sleep(0.4)
    assert app_backends(big) == 0


def test_cancel_during_unthrottled_copy_leaves_nothing(big, server, tmp_path):
    ev = threading.Event()
    seen = threading.Event()
    th, out = in_thread(lambda: run_backup(
        server, big, {"out_dir": str(tmp_path)},
        lambda e, d: seen.set() if e == "table_start" else None, ev))
    assert seen.wait(10)
    ev.set()
    th.join(15)
    # either it was cancelled or it had already finished (fast machine): never half a file
    if isinstance(out.get("error"), Cancelled):
        assert files(tmp_path) == []
    else:
        assert "result" in out and not any(f.endswith(".part") for f in files(tmp_path))


def test_restore_cancel_long_statement(env, server, tmp_path):
    db = env.db("rcancel")
    path = tmp_path / "sleep.sql"
    path.write_bytes(b"CREATE TABLE before_sleep (id int);\nSELECT pg_sleep(30);\nCREATE TABLE after_sleep (id int);\n")
    ev = threading.Event()
    th, out = in_thread(lambda: run_restore(server, db, str(path), None, None, ev))
    time.sleep(1.0)
    t0 = time.monotonic()
    ev.set()
    th.join(6)
    assert time.monotonic() - t0 < 2
    res = out["result"]
    assert res["status"] == "cancelled" and res["error_count"] == 0
    with connect(db) as c:
        assert c.execute("SELECT to_regclass('before_sleep'), to_regclass('after_sleep')").fetchone() == \
            ("before_sleep", None)
    time.sleep(0.4)
    assert app_backends(db) == 0


def test_restore_cancel_single_transaction_rolls_back(env, server, tmp_path):
    db = env.db("rcancel1")
    path = tmp_path / "sleep1.sql"
    path.write_bytes(b"CREATE TABLE before_sleep (id int);\nINSERT INTO before_sleep VALUES (1);\n"
                     b"SELECT pg_sleep(30);\nCREATE TABLE after_sleep (id int);\n")
    ev = threading.Event()
    th, out = in_thread(lambda: run_restore(server, db, str(path), {"single_transaction": True}, None, ev))
    time.sleep(1.0)
    t0 = time.monotonic()
    ev.set()
    th.join(6)
    assert time.monotonic() - t0 < 2
    assert out["result"]["status"] == "cancelled"
    with connect(db) as c:
        assert c.execute("SELECT to_regclass('before_sleep'), to_regclass('after_sleep')").fetchone() == (None, None)
    assert app_backends(db) == 0


def test_restore_cancel_during_big_copy(big, env, server, tmp_path):
    res = run_backup(server, big, {"out_dir": str(tmp_path)})
    db = env.db("rcancel_copy")
    ev = threading.Event()
    started = threading.Event()
    th, out = in_thread(lambda: run_restore(server, db, res["file"], None,
                                            lambda e, d: started.set() if e == "statement" else None, ev))
    time.sleep(0.15)
    ev.set()
    th.join(15)
    assert "result" in out, out
    assert out["result"]["status"] in ("cancelled", "ok")
    assert app_backends(db) == 0


def test_reindex_and_create_index_are_not_blocked(env, server, tmp_path):
    """REINDEX TABLE / CREATE INDEX take SHARE locks: they run while the backup holds ACCESS SHARE (no data risk)."""
    db = env.db("shareddl", "CREATE TABLE t (id int PRIMARY KEY, v int); INSERT INTO t SELECT g, g FROM generate_series(1, 500) g;")
    seen = {}

    def progress(event, data):
        if event == "table_start" and not seen:
            with connect(db) as o:
                o.execute("SET lock_timeout = '1s'")
                o.execute("REINDEX TABLE t")
                o.execute("CREATE INDEX t_v ON t (v)")
                seen["ok"] = True

    res = run_backup(server, db, {"out_dir": str(tmp_path)}, progress)
    assert seen == {"ok": True} and res["rows"] == 500


def test_queued_ddl_behind_a_reader_blocks_backup_and_precheck_lists_it(env, server, tmp_path):
    """Classic pile-up: A reads t (ACCESS SHARE), B's ALTER TABLE waits for ACCESS EXCLUSIVE, so the backup's own
    ACCESS SHARE queues behind B. The backup must time out naming t; the pre-check shows the waiting request."""
    db = env.db("queued", "CREATE TABLE t (id int PRIMARY KEY); INSERT INTO t VALUES (1);")
    reader = connect(db, autocommit=False)
    reader.execute("SELECT * FROM t")
    ddl_conn = connect(db)
    th, out = in_thread(lambda: ddl_conn.execute("ALTER TABLE t ADD COLUMN x int"))
    try:
        time.sleep(0.8)
        pre = run_precheck(server, db)
        assert [(lk["qname"], lk["granted"]) for lk in pre["locks"]] == [("public.t", False)]
        t0 = time.monotonic()
        with pytest.raises(LockTimeoutError, match="t"):
            run_backup(server, db, {"out_dir": str(tmp_path), "lock_timeout_s": 1.5})
        assert 1.3 <= time.monotonic() - t0 < 4.5
        assert files(tmp_path) == []
    finally:
        release(reader)
        th.join(10)
        ddl_conn.close()
    assert run_backup(server, db, {"out_dir": str(tmp_path)})["rows"] == 1


def test_cancel_during_throttled_gzip_copy_leaves_no_file(big, server, tmp_path):
    ev = threading.Event()
    th, out = in_thread(lambda: run_backup(server, big, {"out_dir": str(tmp_path), "gzip": True,
                                                         "speed_limit_mb_s": 0.2}, None, ev))
    time.sleep(2.0)
    assert th.is_alive() and any(f.endswith(".sql.gz.part") for f in files(tmp_path))
    ev.set()
    th.join(5)
    assert isinstance(out.get("error"), Cancelled), out
    assert files(tmp_path) == []
