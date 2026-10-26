"""Developer self-tests for the Step 1 engine against a real PostgreSQL server.

Scratch databases are named pgbs_test_dev_* and dropped afterwards.
"""
import os
import re
import subprocess
import threading

import pytest

from conftest import connect, drop_database, pg_settings, tool
from engine import catalog
from engine.backup import run_backup
from engine.restore import run_restore
from engine.util import Cancelled

DEV_DBS = []


@pytest.fixture(scope="module")
def server():
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


@pytest.fixture
def fresh_db(pg_admin):
    made = []

    def make(suffix):
        name = f"pgbs_test_dev_{suffix}"
        drop_database(pg_admin, name)
        pg_admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
        made.append(name)
        return name

    yield make
    for n in made:
        drop_database(pg_admin, n)


def schema_lines(db):
    s = pg_settings()
    r = subprocess.run([tool("pg_dump"), "-s", "-h", s["host"], "-p", str(s["port"]), "-U", s["user"], db],
                       capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
    assert r.returncode == 0, r.stderr
    return sorted(l for l in r.stdout.splitlines() if l.strip() and not l.startswith("--")
                  and not l.startswith("\\restrict ") and not l.startswith("\\unrestrict "))


def state(db, data=True):
    with connect(db) as c:
        rels = [r[0] for r in c.execute(
            "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relkind = 'r' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'")]
        res = {t: c.execute(f"SELECT count(*) FROM ONLY {t}").fetchone()[0] for t in rels}
        if not data:
            return res
        for (s,) in c.execute("SELECT oid::regclass::text FROM pg_class WHERE relkind = 'S'").fetchall():
            res["seq " + s] = tuple(c.execute(f"SELECT last_value, is_called FROM {s}").fetchone())
        for t, key in (('"sales data".orders', "id, ordered_at"), ("app.edge", "id"), ("app.customers", "id")):
            res["md5 " + t] = c.execute(f"SELECT md5(string_agg(x::text, '|' ORDER BY {key})) FROM {t} x").fetchone()[0]
        res["triggers"] = c.execute("SELECT string_agg(tgname || '=' || tgenabled::text, ',' ORDER BY tgname)"
                                    " FROM pg_trigger WHERE NOT tgisinternal").fetchone()[0]
        res["matview"] = c.execute('SELECT count(*) FROM "sales data".monthly_totals').fetchone()[0]
    return res


def backup(server, src, tmp_path, **opts):
    return run_backup(server, src, {"out_dir": str(tmp_path), **opts})


@pytest.mark.parametrize("gz", [False, True])
def test_full_roundtrip(server, src_db, fresh_db, tmp_path, gz):
    res = backup(server, src_db, tmp_path, gzip=gz)
    assert res["warnings"] == [] and res["rows"] > 5000
    with open(res["file"], "rb") as f:
        raw = f.read()
    assert raw[:2] == b"\x1f\x8b" if gz else b"\r\n" not in raw
    db = fresh_db("full_gz" if gz else "full")
    rr = run_restore(server, db, res["file"])
    assert rr["status"] == "ok" and rr["error_count"] == 0 and rr["warning_count"] == 0, rr["errors"]
    assert schema_lines(db) == schema_lines(src_db)
    assert state(db) == state(src_db)
    with connect(db) as a, connect(src_db) as b:
        for seq in ("app.customers_id_seq", "app.ident_always_id_seq", "app.ticket_seq"):
            expected = b.execute("SELECT last_value + increment_by FROM pg_sequences"
                                 " WHERE format('%%I.%%I', schemaname, sequencename) = %s", (seq,)).fetchone()[0]
            assert a.execute(f"SELECT nextval('{seq}')").fetchone()[0] == expected


def test_schema_then_data_only(server, src_db, fresh_db, tmp_path):
    schema = backup(server, src_db, tmp_path, mode="schema")
    data = backup(server, src_db, tmp_path, mode="data")
    db = fresh_db("data_only")
    rs = run_restore(server, db, schema["file"])
    assert rs["status"] == "ok", rs["errors"]
    assert schema_lines(db) == schema_lines(src_db)
    assert set(state(db, data=False).values()) == {0}
    rd = run_restore(server, db, data["file"])
    assert rd["status"] == "ok", rd["errors"]
    assert state(db) == state(src_db)


def test_privileges_off_and_schema_filter(server, src_db, fresh_db, tmp_path):
    res = backup(server, src_db, tmp_path, privileges=False, schemas=["app", "audit"])
    text = open(res["file"], encoding="utf-8").read()
    assert not re.search(r"^(GRANT|REVOKE)|OWNER TO", text, re.M)
    assert '"sales data"' not in text.split("SET row_security = off;", 1)[1]
    assert "CREATE EXTENSION IF NOT EXISTS \"citext\"" in text
    db = fresh_db("filter")
    rr = run_restore(server, db, res["file"])
    assert rr["status"] == "ok", rr["errors"]


def _inject(path, tmp_path, old, new):
    text = open(path, encoding="utf-8").read()
    assert old in text
    out = tmp_path / "edited.sql"
    out.write_bytes(text.replace(old, new, 1).encode("utf-8"))
    return str(out)


@pytest.mark.parametrize("mode", ["continue", "stop", "single"])
def test_restore_error_modes(server, src_db, fresh_db, tmp_path, mode):
    res = backup(server, src_db, tmp_path)
    bad = _inject(res["file"], tmp_path, "-- Type: TABLE; Name: app.edge\n",
                  "-- Type: TABLE; Name: app.edge\nSELECT * FROM no_such_table;\n")
    db = fresh_db("mode_" + mode)
    opts = {"single_transaction": True} if mode == "single" else {"on_error": mode}
    rr = run_restore(server, db, bad, opts)
    assert rr["error_count"] == 1 and rr["errors"][0]["object"] == "TABLE app.edge"
    assert rr["errors"][0]["sqlstate"] == "42P01" and rr["errors"][0]["sql"].startswith("SELECT * FROM")
    with connect(db) as c:
        tables = c.execute("SELECT count(*) FROM pg_class WHERE relname = 'customers'").fetchone()[0]
        edge = c.execute("SELECT count(*) FROM pg_class WHERE relname = 'edge'").fetchone()[0]
    if mode == "continue":
        assert rr["status"] == "completed_with_errors" and tables == 1 and edge == 1
    elif mode == "stop":
        assert rr["status"] == "failed" and tables == 1 and edge == 0
    else:
        assert rr["status"] == "failed" and tables == 0


@pytest.mark.parametrize("single", [False, True])
def test_missing_role_is_warning(server, src_db, fresh_db, tmp_path, single):
    res = backup(server, src_db, tmp_path)
    text = open(res["file"], encoding="utf-8").read().replace('"pgbs_test_reader"', '"pgbs_test_no_such_role"')
    path = tmp_path / "missing_role.sql"
    path.write_bytes(text.encode("utf-8"))
    db = fresh_db("role")
    rr = run_restore(server, db, str(path), {"single_transaction": single})
    assert rr["status"] == "ok" and rr["error_count"] == 0, rr["errors"]
    assert rr["warning_count"] > 0 and rr["missing_roles"] == ["pgbs_test_no_such_role"]


def test_backup_cancel_removes_part(server, src_db, tmp_path):
    ev = threading.Event()

    def progress(event, data):
        if event == "table_start":
            ev.set()

    with pytest.raises(Cancelled):
        run_backup(server, src_db, {"out_dir": str(tmp_path)}, progress, ev)
    assert list(tmp_path.iterdir()) == []


def test_restore_cancel_single_transaction(server, src_db, fresh_db, tmp_path):
    res = backup(server, src_db, tmp_path)
    db = fresh_db("cancel")
    ev = threading.Event()

    def progress(event, data):
        if event == "object" and data["kind"] == "TABLE DATA":
            ev.set()

    rr = run_restore(server, db, res["file"], {"single_transaction": True}, progress, ev)
    assert rr["status"] == "cancelled"
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'app'").fetchone()[0] == 0


def test_truncated_file_fails_cleanly(server, src_db, fresh_db, tmp_path):
    res = backup(server, src_db, tmp_path)
    raw = open(res["file"], "rb").read()
    cut = tmp_path / "cut.sql"
    cut.write_bytes(raw[: raw.index(b"COPY app.edge") + 200])
    db = fresh_db("cut")
    rr = run_restore(server, db, str(cut))
    assert rr["status"] == "failed" and "truncated" in rr["errors"][-1]["message"]


class _RecordingConn:
    """Stands in for a connection and records the catalog SQL that would run."""

    def __init__(self):
        self.queries = []

    def cursor(self, row_factory=None):
        conn = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, q, params=None):
                conn.queries.append(q)

            def fetchall(self):
                return []
        return Cur()

    def execute(self, q, params=None):
        self.queries.append(q)

        class R:
            def fetchone(self):
                return (0,)
        return R()


@pytest.mark.parametrize("version", [120000, 130000, 140000, 180000])
def test_version_gates(version):
    conn = _RecordingConn()
    ctx = catalog.CatalogContext(version, [1])
    for reader in (catalog.get_types, catalog.get_functions, catalog.get_triggers, catalog.get_columns,
                   catalog.get_aliases, catalog.get_sequences):
        reader(conn, ctx)
    text = "\n".join(conn.queries)
    assert ("prosqlbody" in text) == (version >= 140000)
    assert ("rngmultitypid" in text) == (version >= 140000)
    assert ("tgparentid" in text) == (version >= 130000)
    assert ("contype = 'n'" in text) == (version >= 180000)
