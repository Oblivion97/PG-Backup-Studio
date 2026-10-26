"""QA tests for Step 2 - concurrency edge cases (tables dropped / created between check and snapshot), many
sequences, many tables (pre-check speed, 53200 message) and speed-limit accuracy.
"""
import os
import time

import psycopg
import pytest

from conftest import connect, drop_database
from engine import backup as backup_mod
from engine.backup import run_backup
from engine.precheck import run_precheck
from engine.restore import run_restore
from engine.util import BackupBlockedError, BackupError
from step1_helpers import sequence_state, table_counts
from step2_helpers import Env, app_backends, connect_as, files, server_dict


@pytest.fixture
def env(pg_admin, src_db):
    e = Env(pg_admin)
    yield e
    e.close()


@pytest.fixture(scope="module")
def server():
    return server_dict()


BASE = ("CREATE TABLE t1 (id int PRIMARY KEY); INSERT INTO t1 SELECT generate_series(1, 100);"
        "CREATE TABLE t2 (id int PRIMARY KEY); INSERT INTO t2 SELECT generate_series(1, 200);"
        "CREATE TABLE t3 (id int PRIMARY KEY); INSERT INTO t3 SELECT generate_series(1, 300);")


# ------------------------------------------------------------------ table dropped between check and snapshot

def _after(monkeypatch, name, action, times=1):
    """Run action(callcount) right after the named _BackupRun method returns (first `times` calls)."""
    orig = getattr(backup_mod._BackupRun, name)
    calls = {"n": 0}

    def wrapper(self, *a, **k):
        r = orig(self, *a, **k)
        calls["n"] += 1
        if calls["n"] <= times:
            action(calls["n"])
        return r

    monkeypatch.setattr(backup_mod._BackupRun, name, wrapper)
    return calls


def test_table_dropped_after_check_is_retried_once(env, server, tmp_path, monkeypatch):
    db = env.db("dropped", BASE)

    def drop(n):
        with connect(db) as c:
            c.execute("DROP TABLE t2")

    calls = _after(monkeypatch, "_check_access", drop)
    res = run_backup(server, db, {"out_dir": str(tmp_path)})
    assert calls["n"] == 2  # checked again after the retry
    assert res["tables"] == 2 and res["rows"] == 400
    target = env.db("dropped_r")
    assert run_restore(server, target, res["file"])["error_count"] == 0
    assert sorted(table_counts(target)) == ["t1", "t3"]


def test_table_renamed_after_check(env, server, tmp_path, monkeypatch):
    db = env.db("renamed", BASE)
    _after(monkeypatch, "_check_access", lambda n: connect(db).execute("ALTER TABLE t1 RENAME TO t1_new"))
    res = run_backup(server, db, {"out_dir": str(tmp_path)})
    assert res["rows"] == 600
    target = env.db("renamed_r")
    assert run_restore(server, target, res["file"])["error_count"] == 0
    assert sorted(table_counts(target)) == ["t1_new", "t2", "t3"]


def _drop_schema_after_check(env, server, tmp_path, monkeypatch, suffix):
    db = env.db(suffix, BASE + "CREATE SCHEMA gone; CREATE TABLE gone.x (id int); INSERT INTO gone.x VALUES (1);")
    _after(monkeypatch, "_check_access", lambda n: connect(db).execute("DROP SCHEMA gone CASCADE"))
    return db, lambda: run_backup(server, db, {"out_dir": str(tmp_path)})


def test_schema_dropped_after_check_fails_cleanly(env, server, tmp_path, monkeypatch):
    db, go = _drop_schema_after_check(env, server, tmp_path, monkeypatch, "schemagone")
    try:
        res = go()
    except BackupError as e:  # acceptable outcome 1: a clear error, nothing left behind
        assert "gone" in str(e) and files(tmp_path) == []
    else:                      # acceptable outcome 2: retried without the schema
        assert "gone" not in res["schemas"]
    time.sleep(0.3)
    assert app_backends(db) == 0


@pytest.mark.xfail(strict=True, reason="BUG-S2-4 (minor): a schema dropped between check and lock raises 3F000 "
                                       "(InvalidSchemaName), which is not retried like a dropped table (42P01)")
def test_schema_dropped_after_check_is_retried_like_a_table(env, server, tmp_path, monkeypatch):
    db, go = _drop_schema_after_check(env, server, tmp_path, monkeypatch, "schemagone2")
    res = go()
    assert "gone" not in res["schemas"] and res["rows"] == 600


def test_tables_dropped_twice_fails_cleanly(env, server, tmp_path, monkeypatch):
    db = env.db("dropped2", BASE)

    def drop(n):
        with connect(db) as c:
            c.execute(f"DROP TABLE t{n + 1}")

    _after(monkeypatch, "_check_access", drop, times=2)
    with pytest.raises(BackupError, match="dropped or renamed"):
        run_backup(server, db, {"out_dir": str(tmp_path)})
    assert files(tmp_path) == []
    time.sleep(0.3)
    assert app_backends(db) == 0
    assert run_backup(server, db, {"out_dir": str(tmp_path)})["tables"] == 1  # next try just works


def test_table_dropped_with_schema_only_option_and_latest(env, server, tmp_path, monkeypatch):
    db = env.db("dropped_opt", BASE)
    _after(monkeypatch, "_check_access", lambda n: connect(db).execute("DROP TABLE t3"))
    tabs = [{"schema": "public", "table": "t3", "data": "schema"},
            {"schema": "public", "table": "t2", "data": "latest", "order_by": "id", "limit": 10}]
    res = run_backup(server, db, {"out_dir": str(tmp_path), "tables": tabs})
    assert res["partial"] and [t["table"] for t in res["partial_tables"]] == ["public.t2"]
    assert any("t3" in w and "ignored" in w for w in res["warnings"])


# ------------------------------------------------------------------ table created after the check

def test_table_created_after_check_is_locked_and_included(env, server, tmp_path, monkeypatch):
    db = env.db("late", BASE)

    def create(n):
        with connect(db) as c:
            c.execute("CREATE TABLE late (id int PRIMARY KEY, v text); INSERT INTO late SELECT g, 'x' FROM generate_series(1, 50) g")

    _after(monkeypatch, "_lock_tables", create)
    locked = {}
    orig_new = backup_mod._BackupRun._lock_new_tables

    def spy(self, conn, cat):
        locked["before"] = {t["qname"] for t in cat.tables if t["oid"] in self.locked}
        orig_new(self, conn, cat)
        locked["after"] = {t["qname"] for t in cat.tables if t["oid"] in self.locked}

    monkeypatch.setattr(backup_mod._BackupRun, "_lock_new_tables", spy)
    res = run_backup(server, db, {"out_dir": str(tmp_path)})
    assert "public.late" not in locked["before"] and "public.late" in locked["after"]
    assert res["tables"] == 4 and res["rows"] == 650
    target = env.db("late_r")
    assert run_restore(server, target, res["file"])["error_count"] == 0
    assert table_counts(target)["late"] == 50


def test_partition_attached_after_check_and_locked_tables_cannot_be_attached(env, server, tmp_path, monkeypatch):
    db = env.db("attach", """
        CREATE TABLE p (id int NOT NULL, d date NOT NULL) PARTITION BY RANGE (d);
        CREATE TABLE p1 PARTITION OF p FOR VALUES FROM ('2024-01-01') TO ('2024-07-01');
        INSERT INTO p SELECT g, date '2024-01-01' + g FROM generate_series(1, 100) g;
        CREATE TABLE old2 (id int NOT NULL, d date NOT NULL);
        INSERT INTO old2 SELECT g, date '2025-02-01' + g FROM generate_series(1, 10) g;""")
    seen = {}

    def attach(n):
        with connect(db) as c:   # a brand-new table: not locked by the backup yet
            c.execute("CREATE TABLE p2 (id int NOT NULL, d date NOT NULL);"
                      "INSERT INTO p2 SELECT g, date '2024-08-01' + g FROM generate_series(1, 40) g;"
                      "ALTER TABLE p ATTACH PARTITION p2 FOR VALUES FROM ('2024-07-01') TO ('2025-01-01')")
        with connect(db) as c:   # an old table the backup already holds ACCESS SHARE on: DDL must wait
            c.execute("SET lock_timeout = '500ms'")
            try:
                c.execute("ALTER TABLE p ATTACH PARTITION old2 FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')")
                seen["old2"] = "attached"
            except psycopg.Error as e:
                seen["old2"] = e.sqlstate

    _after(monkeypatch, "_lock_tables", attach)
    res = run_backup(server, db, {"out_dir": str(tmp_path)})
    assert seen == {"old2": "55P03"}
    target = env.db("attach_r")
    assert run_restore(server, target, res["file"])["error_count"] == 0
    with connect(target) as c:
        assert c.execute("SELECT count(*) FROM p").fetchone()[0] == 140
        assert c.execute("SELECT count(*) FROM old2").fetchone()[0] == 10


def test_late_rls_table_for_readonly_login_fails_loudly(env, server, tmp_path, monkeypatch):
    ro, pw = env.role("late_ro", "IN ROLE pg_read_all_data")
    db = env.db("late_rls", BASE)

    def create(n):
        with connect(db) as c:
            c.execute("CREATE TABLE late (id int, who text); INSERT INTO late VALUES (1, 'a'), (2, 'b');"
                      "ALTER TABLE late ENABLE ROW LEVEL SECURITY; CREATE POLICY p ON late USING (who = 'a');"
                      f'GRANT SELECT ON late TO "{ro}"')

    _after(monkeypatch, "_lock_tables", create)
    srv = dict(server, user=ro, password=pw)
    try:
        res = run_backup(srv, db, {"out_dir": str(tmp_path)})
    except BackupError:
        assert files(tmp_path) == []   # loud failure, nothing left behind: acceptable
        return
    # if it succeeded it must not have saved a filtered copy of the table
    text = open(res["file"], encoding="utf-8").read()
    assert "COPY public.late" not in text or "2\tb" in text


def test_late_table_without_select_for_login_fails_cleanly(env, server, tmp_path, monkeypatch):
    ro, pw = env.role("late_ro2")
    db = env.db("late_nosel", BASE + f'GRANT USAGE ON SCHEMA public TO "{ro}"; GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{ro}";')

    def create(n):
        with connect(db) as c:
            c.execute("CREATE TABLE late (id int); INSERT INTO late VALUES (1)")

    _after(monkeypatch, "_lock_tables", create)
    srv = dict(server, user=ro, password=pw)
    with pytest.raises(BackupError, match="late"):
        run_backup(srv, db, {"out_dir": str(tmp_path)})
    assert files(tmp_path) == []
    time.sleep(0.3)
    assert app_backends(db) == 0


# ------------------------------------------------------------------ many sequences (regression of the >50 bug)

def test_many_sequences_privileges_and_roundtrip(env, server, tmp_path):
    ro, pw = env.role("manyseq_ro")
    n_seq, n_tab = 150, 80
    ddl = "".join(f"CREATE SEQUENCE s{i} START {i + 10};" for i in range(n_seq))
    ddl += "".join(f"CREATE TABLE t{i} (id serial PRIMARY KEY, v text); INSERT INTO t{i} (v) VALUES ('a'), ('b');"
                   for i in range(n_tab))
    ddl += "CREATE VIEW vv AS SELECT * FROM t1; CREATE INDEX t1_v ON t1 (v);"
    missing = {"s7", "s70", "s119"}
    ddl += f'GRANT USAGE ON SCHEMA public TO "{ro}"; GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{ro}";'
    ddl += f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{ro}";'
    ddl += "".join(f'REVOKE SELECT ON SEQUENCE {s} FROM "{ro}";' for s in sorted(missing))
    db = env.db("manyseq", ddl)
    srv = dict(server, user=ro, password=pw)
    pre = run_precheck(srv, db)
    assert sorted(p["qname"] for p in pre["problems"]) == sorted(f"public.{s}" for s in missing)
    assert {p["kind"] for p in pre["problems"]} == {"sequence_select"}
    with pytest.raises(BackupBlockedError) as ei:
        run_backup(srv, db, {"out_dir": str(tmp_path)})
    assert len(ei.value.problems) == 3 and files(tmp_path) == []
    with connect(db) as c:
        c.execute(f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO "{ro}"')
    assert run_precheck(srv, db)["problems"] == []
    res = run_backup(srv, db, {"out_dir": str(tmp_path)})
    assert res["warnings"] == []
    text = open(res["file"], encoding="utf-8").read()
    assert text.count("pg_catalog.setval(") == n_seq + n_tab
    target = env.db("manyseq_r")
    rr = run_restore(server, target, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    assert sequence_state(target) == sequence_state(db) and len(sequence_state(db)) == n_seq + n_tab


# ------------------------------------------------------------------ many tables

@pytest.fixture(scope="module")
def many(pg_admin, src_db):
    """6500 tables in schema s2 (more than the shared lock table holds), 1500 in schema s1."""
    name = "pgbs_test_qa_s2_many"
    drop_database(pg_admin, name)
    pg_admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
    with connect(name) as c:
        s = c.execute("SELECT current_setting('max_locks_per_transaction')::int,"
                      " current_setting('max_connections')::int + current_setting('max_prepared_transactions')::int"
                      ).fetchone()
        capacity = s[0] * s[1]
        c.execute("CREATE SCHEMA s1; CREATE SCHEMA s2")
        n2 = int(capacity * 2.6)  # the lock table has ~2x slack: ~14.6k tables fail on the default 6400
        for lo in range(0, 1500, 500):
            c.execute(f"DO $$ BEGIN FOR i IN {lo}..{lo + 499} LOOP EXECUTE format('CREATE TABLE s1.t%s (id int)', i); END LOOP; END $$")
        for lo in range(0, n2, 500):
            hi = min(lo + 499, n2 - 1)
            c.execute(f"DO $$ BEGIN FOR i IN {lo}..{hi} LOOP EXECUTE format('CREATE TABLE s2.t%s (id int)', i); END LOOP; END $$")
        c.execute("CREATE SCHEMA s3")
        for lo in range(0, 4500, 500):
            c.execute(f"DO $$ BEGIN FOR i IN {lo}..{lo + 499} LOOP EXECUTE format('CREATE TABLE s3.t%s (id int)', i); END LOOP; END $$")
        c.execute("INSERT INTO s1.t0 VALUES (1); ANALYZE s1.t0")
    yield name, capacity, n2
    drop_database(pg_admin, name)


def test_precheck_speed_with_1500_tables(many, server):
    t0 = time.monotonic()
    pre = run_precheck(server, many[0], schemas=["s1"])
    took = time.monotonic() - t0
    assert len(pre["tables"]) == 1500 and took < 10, took


@pytest.mark.xfail(strict=True, reason="BUG-S2-3 (minor, performance): run_check groups leaves per root with a list "
                                       "comprehension over all leaves, O(n^2): 8000 tables ~10 s, 18000 ~140 s")
def test_precheck_time_grows_linearly_with_table_count(many, server, capsys):
    name = many[0]
    t0 = time.monotonic()
    run_precheck(server, name, schemas=["s1"])
    t_small = time.monotonic() - t0
    t0 = time.monotonic()
    pre = run_precheck(server, name, schemas=["s3"])
    t_big = time.monotonic() - t0
    assert len(pre["tables"]) == 4500
    with capsys.disabled():
        print(f"[QA] pre-check: 1500 tables {t_small:.2f} s, 4500 tables {t_big:.2f} s (linear would be {3 * t_small:.2f} s)")
    assert t_big < 5 * t_small + 0.5, (t_small, t_big)


def test_backup_of_1500_tables_ok(many, server, tmp_path):
    name, _, _ = many
    t0 = time.monotonic()
    res = run_backup(server, name, {"out_dir": str(tmp_path), "schemas": ["s1"]})
    assert res["tables"] == 1500 and res["rows"] == 1
    assert time.monotonic() - t0 < 60


def test_too_many_locks_gives_clear_message_and_cleans_up(many, server, tmp_path):
    name, capacity, n2 = many
    with pytest.raises(BackupError) as ei:
        run_backup(server, name, {"out_dir": str(tmp_path), "schemas": ["s2"]})
    msg = str(ei.value)
    assert "max_locks_per_transaction" in msg, msg
    assert "out of shared memory" not in msg.lower()
    assert files(tmp_path) == []
    time.sleep(0.5)
    assert app_backends(name) == 0
    # the server is healthy afterwards and the smaller backup works
    assert run_backup(server, name, {"out_dir": str(tmp_path), "schemas": ["s1"]})["tables"] == 1500


# ------------------------------------------------------------------ speed limit accuracy

@pytest.fixture(scope="module")
def sized(pg_admin, src_db):
    name = "pgbs_test_qa_s2_speed"
    drop_database(pg_admin, name)
    pg_admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
    with connect(name) as c:
        c.execute("CREATE TABLE big (id int PRIMARY KEY, payload text);"
                  "INSERT INTO big SELECT i, md5(i::text) || md5((i + 1)::text) FROM generate_series(1, 200000) i;")
        for i in range(40):
            c.execute(f"CREATE TABLE small{i} (id int, p text); INSERT INTO small{i} SELECT g, md5(g::text) FROM generate_series(1, 1500) g")
    yield name
    drop_database(pg_admin, name)


def _copy_mib(path):
    """Uncompressed COPY data in the file, in MiB."""
    import gzip
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:
        data = f.read()
    return len(data) / MB


MB = 2 ** 20


@pytest.mark.parametrize("rate", [2, 8])
def test_speed_limit_accuracy_on_bigger_table(sized, server, tmp_path, rate):
    schemas = ["public"]
    t0 = time.monotonic()
    free = run_backup(server, sized, {"out_dir": str(tmp_path / "free"), "tables": [
        {"schema": "public", "table": f"small{i}", "data": "schema"} for i in range(40)]})
    free_s = time.monotonic() - t0
    mib = _copy_mib(free["file"])
    assert mib > 10
    t0 = time.monotonic()
    run_backup(server, sized, {"out_dir": str(tmp_path / "lim"), "speed_limit_mb_s": rate,
                               "tables": [{"schema": "public", "table": f"small{i}", "data": "schema"} for i in range(40)]})
    took = time.monotonic() - t0
    ideal = mib / rate
    assert 0.85 * ideal <= took <= 1.3 * ideal + 1.5, (took, ideal, free_s)


def test_speed_limit_applies_to_many_small_tables(sized, server, tmp_path):
    only_small = [{"schema": "public", "table": "big", "data": "schema"}]
    t0 = time.monotonic()
    free = run_backup(server, sized, {"out_dir": str(tmp_path / "f"), "tables": only_small})
    free_s = time.monotonic() - t0
    mib = _copy_mib(free["file"])
    t0 = time.monotonic()
    run_backup(server, sized, {"out_dir": str(tmp_path / "l"), "tables": only_small, "speed_limit_mb_s": 1})
    took = time.monotonic() - t0
    assert took >= mib / 1 * 0.8, (took, mib, free_s)  # 40 tables of ~100 KB each: the limit holds overall


def test_speed_limit_counts_uncompressed_bytes_with_gzip(sized, server, tmp_path):
    only_big = [{"schema": "public", "table": f"small{i}", "data": "schema"} for i in range(40)]
    t0 = time.monotonic()
    res = run_backup(server, sized, {"out_dir": str(tmp_path), "tables": only_big, "gzip": True,
                                     "speed_limit_mb_s": 4})
    took = time.monotonic() - t0
    mib = _copy_mib(res["file"])
    assert took >= 0.8 * mib / 4, (took, mib, os.path.getsize(res["file"]) / MB)
