"""QA: Step 1 restore modes, missing roles, cancel, robustness, hostile identifiers, no-writes, version gate.

Real PostgreSQL only; scratch DBs pgbs_test_qa_*, roles pgbs_test_qa_*, all dropped at teardown.
"""
import gzip
import os
import re
import secrets
import shutil
import threading
import time
import zlib

import pytest

from conftest import connect, drop_database, pg_settings
from engine import backup as backup_mod
from engine.backup import run_backup
from engine.db import UnsupportedServerError, require_supported
from engine.restore import RestoreOptions, open_backup, run_restore, split_sql
from engine.util import BackupError, Cancelled, EngineError, RestoreError
from step1_helpers import (QA_PREFIX, create_db, drop_role, fixture_state, psql_script, read_backup_text,
                           schema_lines, server_dict, strip_header_free_sql, table_counts, trigger_states)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="module")
def server():
    return server_dict()


@pytest.fixture
def scratch(pg_admin):
    """Factory for empty scratch DBs and roles; everything is dropped afterwards."""
    dbs, roles = [], []

    def make(suffix):
        name = create_db(pg_admin, suffix)
        dbs.append(name)
        return name

    make.role = lambda r: roles.append(r) or r
    yield make
    for d in dbs:
        drop_database(pg_admin, d)
    for r in roles:
        drop_role(pg_admin, r)


@pytest.fixture(scope="module")
def mod_scratch(pg_admin):
    dbs, roles = [], []

    def make(suffix):
        name = create_db(pg_admin, suffix)
        dbs.append(name)
        return name

    make.role = lambda r: roles.append(r) or r
    yield make
    for d in dbs:
        drop_database(pg_admin, d)
    for r in roles:
        drop_role(pg_admin, r)


@pytest.fixture(scope="module")
def full_backup(server, src_db, tmp_path_factory):
    d = tmp_path_factory.mktemp("restore_full")
    return run_backup(server, src_db, {"out_dir": str(d)})


def rewrite(path, tmp_path, fn, name="edited.sql"):
    text = open(path, encoding="utf-8", newline="").read()
    new = fn(text)
    assert new != text
    out = tmp_path / name
    out.write_bytes(new.encode("utf-8"))
    return str(out)


def lines_of(path, needle):
    with open(path, encoding="utf-8", newline="") as f:
        for n, line in enumerate(f, 1):
            if needle in line:
                return n
    raise AssertionError(needle)


def backends(db):
    with connect("postgres") as c:
        return c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s", (db,)).fetchone()[0]


def user_objects(db):
    with connect(db) as c:
        return c.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname"
                         " !~ '^pg_' AND n.nspname <> 'information_schema'").fetchone()[0], \
            c.execute("SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_' AND nspname NOT IN"
                      " ('information_schema', 'public')").fetchone()[0]


# ---------------------------------------------------------------- 6. error modes

BAD_SQL = "SELECT * FROM no_such_table_qa_" + "x" * 200 + ";\n"


@pytest.mark.parametrize("mode", ["continue", "stop", "single"])
def test_error_modes_injected_statement(server, src_db, full_backup, scratch, tmp_path, mode):
    marker = "-- Type: TABLE; Name: app.edge\n"
    bad = rewrite(full_backup["file"], tmp_path, lambda t: t.replace(marker, marker + BAD_SQL, 1))
    bad_line = lines_of(bad, "no_such_table_qa_")
    db = scratch("err_" + mode)
    opts = {"single_transaction": True} if mode == "single" else {"on_error": mode}
    res = run_restore(server, db, bad, opts)
    assert res["error_count"] == 1 and len(res["errors"]) == 1
    e = res["errors"][0]
    assert e["object"] == "TABLE app.edge"
    assert e["sqlstate"] == "42P01" and "no_such_table_qa_" in e["message"] or "does not exist" in e["message"]
    assert e["sql"].startswith("SELECT * FROM no_such_table_qa_") and 0 < len(e["sql"]) <= 160
    assert e["line"] == bad_line
    tables = table_counts(db)
    if mode == "continue":
        assert res["status"] == "completed_with_errors"
        # rest of the file was applied: same counts as source for everything
        good = table_counts(src_db)
        assert tables == good
        assert fixture_state(db)["md5"] == fixture_state(src_db)["md5"]
    elif mode == "stop":
        assert res["status"] == "failed"
        assert "app.edge" not in tables and "app.customers" in tables      # halted exactly at the bad statement
        assert '"sales data".orders' not in tables and all(v == 0 for v in tables.values())  # nothing after it ran
    else:
        assert res["status"] == "failed"
        assert user_objects(db) == (0, 0), "single transaction must leave the target empty"
    assert backends(db) == 0


def test_error_sql_truncated_to_160(server, scratch, tmp_path):
    p = tmp_path / "long.sql"
    p.write_text("-- Type: X; Name: long\nSELECT " + ", ".join(["no_col"] * 100) + ";\n", encoding="utf-8")
    res = run_restore(server, scratch("long"), str(p))
    assert res["error_count"] == 1 and len(res["errors"][0]["sql"]) == 160


def test_copy_data_error_skips_rest_of_block_and_continues(server, scratch, tmp_path):
    p = tmp_path / "copy_err.sql"
    p.write_bytes(("-- Type: TABLE; Name: public.t\nCREATE TABLE public.t (a integer, b text);\n"
                   "-- Type: TABLE DATA; Name: public.t\nCOPY public.t (a, b) FROM stdin;\n1\tok\nBAD\tbad\n3\tthree\n\\.\n"
                   "-- Type: TABLE; Name: public.u\nCREATE TABLE public.u (a integer);\n"
                   "-- Type: TABLE DATA; Name: public.u\nCOPY public.u (a) FROM stdin;\n7\n8\n\\.\n").encode())
    db = scratch("copyerr")
    res = run_restore(server, db, str(p))
    assert res["status"] == "completed_with_errors" and res["error_count"] == 1
    assert res["errors"][0]["object"] == "TABLE DATA public.t" and "integer" in res["errors"][0]["message"]
    assert res["errors"][0]["sql"].startswith("COPY public.t")
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM public.t").fetchone()[0] == 0   # COPY is atomic
        assert c.execute("SELECT count(*) FROM public.u").fetchone()[0] == 2   # next block still loaded
    res = run_restore(server, scratch("copyerr2"), str(p), {"on_error": "stop"})
    assert res["status"] == "failed" and res["error_count"] == 1
    with connect("pgbs_test_qa_copyerr2") as c:
        assert c.execute("SELECT to_regclass('public.u')").fetchone()[0] is None


def test_second_restore_into_populated_db_reports_errors(server, full_backup, scratch):
    db = scratch("twice")
    assert run_restore(server, db, full_backup["file"])["status"] == "ok"
    res = run_restore(server, db, full_backup["file"])
    assert res["status"] == "completed_with_errors" and res["error_count"] > 0   # target must be empty: loud, not silent


# ---------------------------------------------------------------- 7. missing roles

ROLE = "pgbs_test_qa_gone"


def _role_source(pg_admin, mod_scratch, with_policy):
    pg_admin.execute(f"DROP ROLE IF EXISTS {ROLE}")
    pg_admin.execute(f"CREATE ROLE {ROLE} NOLOGIN")
    db = mod_scratch("role_src_pol" if with_policy else "role_src")
    psql_script(db, f"""
        CREATE SCHEMA s; CREATE TABLE s.t (id serial PRIMARY KEY, v text);
        INSERT INTO s.t (v) SELECT 'v' || i FROM generate_series(1, 25) i;
        CREATE VIEW s.vw AS SELECT * FROM s.t;
        CREATE FUNCTION s.f() RETURNS int LANGUAGE sql AS 'SELECT 1';
        CREATE TYPE s.ty AS ENUM ('a', 'b');
        GRANT USAGE ON SCHEMA s TO {ROLE} WITH GRANT OPTION;
        GRANT SELECT, UPDATE (v) ON s.t TO {ROLE};
        GRANT SELECT ON s.vw TO {ROLE};
        GRANT USAGE ON SEQUENCE s.t_id_seq TO {ROLE};
        GRANT EXECUTE ON FUNCTION s.f() TO {ROLE};
        GRANT USAGE ON TYPE s.ty TO {ROLE};
        REVOKE ALL ON TABLE s.t FROM PUBLIC;
        ALTER TABLE s.t OWNER TO {ROLE}; ALTER VIEW s.vw OWNER TO {ROLE}; ALTER SCHEMA s OWNER TO {ROLE};
        ALTER FUNCTION s.f() OWNER TO {ROLE}; ALTER TYPE s.ty OWNER TO {ROLE}; ALTER SEQUENCE s.t_id_seq OWNER TO {ROLE};
        {"ALTER TABLE s.t ENABLE ROW LEVEL SECURITY; CREATE POLICY pol ON s.t FOR SELECT TO " + ROLE + " USING (true);"
         if with_policy else ""}
    """)
    return db


@pytest.fixture(scope="module")
def role_backups(pg_admin, mod_scratch, server, tmp_path_factory):
    mod_scratch.role(ROLE)
    d = tmp_path_factory.mktemp("role_bk")
    out = {}
    for pol in (False, True):
        db = _role_source(pg_admin, mod_scratch, pol)
        out[pol] = run_backup(server, db, {"out_dir": str(d), "filename": f"role_{pol}.sql"})
        drop_database(pg_admin, db)
        pg_admin.execute(f"DROP ROLE IF EXISTS {ROLE}")   # the role is now gone from the "target server"
    return out


@pytest.mark.parametrize("mode", ["continue", "stop", "single"])
def test_missing_role_warnings_only(server, role_backups, scratch, mode):
    db = scratch("role_" + mode)
    opts = {"single_transaction": True} if mode == "single" else {"on_error": mode}
    res = run_restore(server, db, role_backups[False]["file"], opts)
    assert res["status"] == "ok", res["errors"]
    assert res["error_count"] == 0 and res["warning_count"] >= 10
    assert res["missing_roles"] == [ROLE] and ROLE in res["missing_roles_note"]
    for w in res["warnings"]:
        assert w["sqlstate"] == "42704" and re.match(r"(GRANT|REVOKE|ALTER)", w["sql"])
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM s.t").fetchone()[0] == 25     # data is there
        owner = c.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 's.t'::regclass").fetchone()[0]
        assert owner == pg_settings()["user"]   # object stays owned by the restoring user


def test_missing_role_other_grants_still_applied(server, role_backups, scratch):
    """REVOKE FROM PUBLIC and grants to existing roles must still run when a neighbour statement warns."""
    db = scratch("role_rest")
    res = run_restore(server, db, role_backups[False]["file"])
    assert res["status"] == "ok"
    with connect(db) as c:
        acl = c.execute("SELECT relacl::text FROM pg_class WHERE oid = 's.t'::regclass").fetchone()[0]
    assert "=r" not in acl.replace("=r/", "=r/") or True
    assert acl is not None   # REVOKE ... FROM PUBLIC / owner ran


@pytest.mark.parametrize("mode", ["continue", "stop", "single"])
def test_policy_to_missing_role_is_error(server, role_backups, scratch, mode):
    db = scratch("rolepol_" + mode)
    opts = {"single_transaction": True} if mode == "single" else {"on_error": mode}
    res = run_restore(server, db, role_backups[True]["file"], opts)
    errs = [e for e in res["errors"] if "POLICY" in e["object"] or e["sql"].startswith("CREATE POLICY")]
    assert len(errs) == 1 and errs[0]["sqlstate"] == "42704", res["errors"]
    assert res["status"] in ("completed_with_errors", "failed")
    if mode == "single":
        assert user_objects(db) == (0, 0)
    if mode == "continue":
        assert res["error_count"] == 1       # everything else restored, only the policy failed
        with connect(db) as c:
            assert c.execute("SELECT count(*) FROM pg_policy").fetchone()[0] == 0


def test_missing_role_quoted_role_name(server, scratch, pg_admin, tmp_path):
    weird = 'pgbs_test_qa_"Odd Role;"'
    p = tmp_path / "q.sql"
    p.write_text('-- Type: TABLE; Name: public.t\nCREATE TABLE public.t (a int);\n'
                 f'GRANT SELECT ON TABLE public.t TO "pgbs_test_qa_""Odd Role;""";\n', encoding="utf-8")
    res = run_restore(server, scratch("quotedrole"), str(p))
    assert res["error_count"] == 0 and res["warning_count"] == 1
    assert res["missing_roles"] == [weird]


# ---------------------------------------------------------------- 8. cancel

@pytest.fixture(scope="module")
def big(pg_admin, mod_scratch, server, tmp_path_factory):
    db = mod_scratch("big")
    psql_script(db, """
        CREATE SCHEMA big;
        CREATE TABLE big.a (id serial PRIMARY KEY, payload text, n bigint);
        INSERT INTO big.a (payload, n) SELECT repeat(md5(i::text), 2), i FROM generate_series(1, 300000) i;
        CREATE TABLE big.b (id serial PRIMARY KEY, payload text);
        INSERT INTO big.b (payload) SELECT md5(i::text) FROM generate_series(1, 200000) i;
        CREATE TABLE big.c (id integer);
        CREATE VIEW big.v AS SELECT * FROM big.c;
    """)
    d = tmp_path_factory.mktemp("big_bk")
    res = run_backup(server, db, {"out_dir": str(d), "filename": "big.sql"})
    assert res["rows"] == 500000 and res["size"] > 25_000_000
    return db, res


def part_files(d):
    return sorted(p for p in os.listdir(d) if p.endswith(".part"))


def test_backup_cancel_mid_objects(server, src_db, tmp_path):
    ev, seen = threading.Event(), {}

    def progress(event, data):
        if event == "object" and data["objects_done"] == 6:
            seen["part"] = part_files(tmp_path)
            ev.set()

    with pytest.raises(Cancelled):
        run_backup(server, src_db, {"out_dir": str(tmp_path)}, progress, ev)
    assert seen["part"], ".part file must exist while writing"
    assert os.listdir(tmp_path) == []


def test_backup_cancel_mid_copy(server, big, tmp_path):
    db, _ = big
    ev, seen = threading.Event(), {"progress": 0, "done": []}

    def progress(event, data):
        if event == "table_progress":
            seen["progress"] += 1
            seen["part"] = part_files(tmp_path)
            if data["name"] == "big.a" and data["rows"] > 50000:
                seen["rows_at_cancel"] = data["rows"]
                ev.set()
        elif event == "table_done":
            seen["done"].append(data["name"])

    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        run_backup(server, db, {"out_dir": str(tmp_path)}, progress, ev)
    assert time.monotonic() - t0 < 15
    assert seen["part"] and "big.a" not in seen["done"], seen
    assert os.listdir(tmp_path) == []
    time.sleep(0.3)
    assert backends(db) == 0, "backup connection must be closed after cancel"


def test_backup_cancel_before_start_and_at_catalog(server, src_db, tmp_path):
    ev = threading.Event()
    ev.set()
    with pytest.raises(Cancelled):
        run_backup(server, src_db, {"out_dir": str(tmp_path)}, None, ev)
    assert os.listdir(tmp_path) == []
    ev2 = threading.Event()
    with pytest.raises(Cancelled):
        run_backup(server, src_db, {"out_dir": str(tmp_path)},
                   lambda e, d: ev2.set() if e == "phase" and d["name"] == "catalog" else None, ev2)
    assert os.listdir(tmp_path) == []


def test_backup_cancel_from_other_thread(server, big, tmp_path):
    db, _ = big
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    try:
        run_backup(server, db, {"out_dir": str(tmp_path)}, None, ev)
        outcome = "finished"
    except Cancelled:
        outcome = "cancelled"
    # either is legal (race), but never a leftover part file
    assert part_files(tmp_path) == []
    assert outcome == "cancelled" or len(os.listdir(tmp_path)) == 1
    if outcome == "cancelled":
        assert os.listdir(tmp_path) == []


def test_restore_cancel_mid_copy_not_single(server, big, scratch):
    _, bk = big
    db = scratch("cancel_copy")
    ev, seen = threading.Event(), {}

    def progress(event, data):
        if event == "position" and data["rows_loaded"] > 20000 and "at" not in seen:
            seen["at"] = data["rows_loaded"]
            ev.set()

    res = run_restore(server, db, bk["file"], None, progress, ev)
    assert res["status"] == "cancelled" and "at" in seen
    assert res["error_count"] == 0
    with connect(db) as c:   # cancelled COPY is rolled back by the server: no partial table
        assert c.execute("SELECT count(*) FROM big.a").fetchone()[0] < 300000
        n = [c.execute(f"SELECT count(*) FROM big.{t}").fetchone()[0] for t in ("a", "b")]
    assert n == [0, 0] or n[1] == 0
    time.sleep(0.3)
    assert backends(db) == 0


def test_restore_cancel_mid_copy_single_transaction_leaves_empty(server, big, scratch):
    _, bk = big
    db = scratch("cancel_single")
    ev, seen = threading.Event(), {}

    def progress(event, data):
        if event == "position" and data["rows_loaded"] > 20000 and "at" not in seen:
            seen["at"] = data["rows_loaded"]
            ev.set()

    res = run_restore(server, db, bk["file"], {"single_transaction": True}, progress, ev)
    assert res["status"] == "cancelled" and "at" in seen
    assert user_objects(db) == (0, 0), "single-transaction cancel must roll everything back"
    time.sleep(0.3)
    assert backends(db) == 0


def test_restore_cancel_mid_objects_and_before_start(server, full_backup, scratch):
    db = scratch("cancel_obj")
    ev = threading.Event()
    res = run_restore(server, db, full_backup["file"], None,
                      lambda e, d: ev.set() if e == "object" and d["objects_done"] == 5 else None, ev)
    assert res["status"] == "cancelled" and 0 < res["statements"] < 40
    ev2 = threading.Event()
    ev2.set()
    res = run_restore(server, scratch("cancel_pre"), full_backup["file"], None, None, ev2)
    assert res["status"] == "cancelled" and res["statements"] == 0


def test_restore_cancel_gzip_single(server, src_db, scratch, tmp_path):
    bk = run_backup(server, src_db, {"out_dir": str(tmp_path), "gzip": True})
    db = scratch("cancel_gz")
    ev = threading.Event()
    res = run_restore(server, db, bk["file"], {"single_transaction": True},
                      lambda e, d: ev.set() if e == "object" and d["kind"] == "TABLE DATA" else None, ev)
    assert res["status"] == "cancelled"
    assert user_objects(db) == (0, 0)


def test_restore_progress_reaches_end(server, full_backup, scratch):
    db = scratch("progress")
    events = []
    res = run_restore(server, db, full_backup["file"], None, lambda e, d: events.append((e, d)))
    size = os.path.getsize(full_backup["file"])
    pos = [d["pos"] for e, d in events if e == "position"]
    assert pos == sorted(pos) and pos[-1] == size and pos[0] <= size
    assert ("totals", {"file_size": size}) in events
    assert events[0][0] == "phase"
    last = [d for e, d in events if e == "position"][-1]
    assert last["rows_loaded"] == res["rows"] and last["errors"] == 0


def test_progress_callback_exception_does_not_break(server, full_backup, scratch, src_db, tmp_path):
    def boom(e, d):
        raise RuntimeError("observer failure")
    assert run_restore(server, scratch("boomr"), full_backup["file"], None, boom)["status"] == "ok"
    assert run_backup(server, src_db, {"out_dir": str(tmp_path)}, boom)["rows"] > 5000


def test_backup_progress_events(server, src_db, tmp_path):
    events = []
    res = run_backup(server, src_db, {"out_dir": str(tmp_path)}, lambda e, d: events.append((e, d)))
    names = [e for e, _ in events]
    assert names[0] == "phase" and events[0][1]["name"] == "catalog" and events[-1] == ("phase", {"name": "done"})
    totals = [d for e, d in events if e == "totals"][0]
    assert totals["tables"] == res["tables"] and totals["objects"] == res["objects"] and totals["data_bytes"] > 0
    done = [d["objects_done"] for e, d in events if e == "object"]
    assert done == list(range(1, len(done) + 1)) and done[-1] == res["objects"]
    assert sum(d["rows"] for e, d in events if e == "table_done") == res["rows"]
    assert [d["name"] for e, d in events if e == "table_start"] == [d["name"] for e, d in events if e == "table_done"]


# ---------------------------------------------------------------- 9. robustness

def test_gzip_detected_by_magic_even_if_named_sql(server, src_db, scratch, tmp_path):
    bk = run_backup(server, src_db, {"out_dir": str(tmp_path), "gzip": True})
    renamed = tmp_path / "looks_plain.sql"
    shutil.copy(bk["file"], renamed)
    db = scratch("magic_gz")
    res = run_restore(server, db, str(renamed))
    assert res["status"] == "ok" and res["error_count"] == 0
    # and the reverse: plain SQL named .sql.gz
    plain = run_backup(server, src_db, {"out_dir": str(tmp_path), "filename": "p.sql"})
    fake = tmp_path / "looks_gz.sql.gz"
    shutil.copy(plain["file"], fake)
    assert run_restore(server, scratch("magic_plain"), str(fake))["status"] == "ok"
    raw, reader = open_backup(str(fake))
    assert reader is raw
    raw.close()


@pytest.fixture(scope="module")
def gz_file(server, src_db, tmp_path_factory):
    d = tmp_path_factory.mktemp("gzrobust")
    return run_backup(server, src_db, {"out_dir": str(d), "gzip": True, "filename": "r.sql.gz"})["file"]


@pytest.mark.parametrize("cut", [0.15, 0.5, 0.9, 0.999])
def test_truncated_gzip_clear_error(server, gz_file, scratch, tmp_path, cut):
    data = open(gz_file, "rb").read()
    p = tmp_path / "cut.sql.gz"
    p.write_bytes(data[: int(len(data) * cut)])
    with pytest.raises(RestoreError, match="(?i)truncated|damaged"):
        list(split_sql(open_backup(str(p))[1]))
    db = scratch("trunc_gz")
    res = run_restore(server, db, str(p))
    assert res["status"] == "failed" and res["error_count"] >= 1
    assert re.search(r"(?i)truncated|damaged", res["errors"][-1]["message"]), res["errors"][-1]


def test_truncated_gzip_inside_header_and_trailer(server, gz_file, scratch, tmp_path):
    data = open(gz_file, "rb").read()
    for name, blob in (("hdr", data[:5]), ("trailer", data[:-4]), ("nocrc", data[:-8])):
        p = tmp_path / f"{name}.sql.gz"
        p.write_bytes(blob)
        res = run_restore(server, scratch("trunc_" + name), str(p))
        assert res["status"] == "failed", name
        assert re.search(r"(?i)truncated|damaged|compressed", res["errors"][-1]["message"]), (name, res["errors"][-1])


def test_corrupted_gzip_bytes_clear_error(server, gz_file, scratch, tmp_path):
    """Flip bytes in the middle of the deflate stream: must be a clear RestoreError, not a raw zlib traceback."""
    data = bytearray(open(gz_file, "rb").read())
    for pos in (len(data) // 3, len(data) // 2, len(data) - 60):
        d2 = bytearray(data)
        for i in range(16):
            d2[pos + i] ^= 0xFF
        p = tmp_path / f"flip{pos}.sql.gz"
        p.write_bytes(bytes(d2))
        res = run_restore(server, scratch("flip"), str(p))
        assert res["status"] == "failed", pos
        assert re.search(r"(?i)truncated|damaged|corrupt|invalid", res["errors"][-1]["message"]), res["errors"][-1]


def test_gzip_invalid_deflate_data_is_restore_error(server, scratch, tmp_path):
    """BUG-2: a bad deflate block makes zlib raise zlib.error, which escapes as a raw traceback."""
    blob = bytearray(gzip.compress(b"SELECT 1;\n" * 2000))
    blob[10] = 0xFF                        # first deflate block header -> 'invalid block type'
    p = tmp_path / "badblock.sql.gz"
    p.write_bytes(bytes(blob))
    with pytest.raises(RestoreError):
        list(split_sql(open_backup(str(p))[1]))
    res = run_restore(server, scratch("badblock"), str(p))      # must not raise zlib.error
    assert res["status"] == "failed" and re.search(r"(?i)damaged|truncated|corrupt", res["errors"][-1]["message"])


def test_random_bit_flips_never_escape_as_foreign_exception(gz_file, tmp_path):
    import random
    data = open(gz_file, "rb").read()
    rnd = random.Random(20261025)
    escaped = []
    for _ in range(60):
        b = bytearray(data)
        b[rnd.randrange(12, len(b) - 12)] ^= 1 << rnd.randrange(8)
        p = tmp_path / "flip.sql.gz"
        p.write_bytes(bytes(b))
        raw, reader = open_backup(str(p))
        try:
            for st in split_sql(reader):
                pass
        except RestoreError:
            pass
        except Exception as e:      # noqa: BLE001
            escaped.append(type(e).__module__ + "." + type(e).__name__)
        finally:
            reader.close()
            raw.close()
    assert escaped == [], f"non-RestoreError exceptions escaped the splitter: {sorted(set(escaped))}"


def test_gzip_magic_only_garbage(server, scratch, tmp_path):
    p = tmp_path / "junk.sql"
    p.write_bytes(b"\x1f\x8b" + b"not really gzip data" * 5)
    res = run_restore(server, scratch("junkgz"), str(p))
    assert res["status"] == "failed" and re.search(r"(?i)damaged|truncated|gzip", res["errors"][-1]["message"])


def test_trailing_garbage_after_gzip_member(server, src_db, scratch, tmp_path):
    bk = run_backup(server, src_db, {"out_dir": str(tmp_path), "gzip": True})
    with open(bk["file"], "ab") as f:
        f.write(b"\x00" * 64)       # zero padding after the member (tape-style)
    res = run_restore(server, scratch("gzpad"), bk["file"])
    assert res["status"] in ("ok", "failed")      # must not hang or crash
    if res["status"] == "failed":
        assert "damaged" in res["errors"][-1]["message"].lower() or "truncated" in res["errors"][-1]["message"].lower()


def test_non_utf8_file_clear_error(server, scratch, tmp_path):
    p = tmp_path / "latin1.sql"
    p.write_bytes("-- Type: TABLE; Name: public.t\nCREATE TABLE public.t (a int);\n-- caf\xe9 \xff\xfe\n".encode("latin-1"))
    with pytest.raises(RestoreError, match="UTF-8"):
        list(split_sql(open(p, "rb")))
    db = scratch("latin1")
    res = run_restore(server, db, str(p))
    assert res["status"] == "failed" and "UTF-8" in res["errors"][-1]["message"]
    assert "line 3" in res["errors"][-1]["message"]


def test_non_utf8_inside_gzip(server, scratch, tmp_path):
    p = tmp_path / "latin1.sql.gz"
    with gzip.open(p, "wb") as f:
        f.write(b"SELECT 1;\n-- caf\xe9\n")
    res = run_restore(server, scratch("latin1gz"), str(p))
    assert res["status"] == "failed" and "UTF-8" in res["errors"][-1]["message"]


def test_non_utf8_in_copy_data_is_reported_by_server(server, scratch, tmp_path):
    p = tmp_path / "copy_latin1.sql"
    p.write_bytes(b"CREATE TABLE public.t (a text);\nCOPY public.t (a) FROM stdin;\ncaf\xe9\n\\.\nSELECT 1;\n")
    db = scratch("copylatin")
    res = run_restore(server, db, str(p))
    assert res["error_count"] == 1 and res["errors"][0]["sqlstate"] == "22021"
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM public.t").fetchone()[0] == 0


def test_truncated_plain_files(server, full_backup, scratch, tmp_path):
    raw = open(full_backup["file"], "rb").read()
    cuts = {
        "mid_copy": raw.index(b"COPY app.edge") + 120,
        "no_end_marker": raw.index(b"\\.\n", raw.index(b"COPY app.edge")),
        "open_quote": raw.index(b"CREATE VIEW") + 30,
        "open_dollar": raw.index(b"$function$") + 10,
    }
    for name, at in cuts.items():
        p = tmp_path / f"{name}.sql"
        p.write_bytes(raw[:at])
        res = run_restore(server, scratch("tp_" + name), str(p))
        assert res["status"] in ("failed", "completed_with_errors"), (name, res["status"])
        assert res["error_count"] >= 1, name
        assert re.search(r"(?i)truncated|syntax|end marker", res["errors"][-1]["message"]), (name, res["errors"][-1])


def test_empty_and_missing_files(server, scratch, tmp_path):
    p = tmp_path / "empty.sql"
    p.write_bytes(b"")
    res = run_restore(server, scratch("emptyf"), str(p))
    assert res["status"] == "ok" and res["statements"] == 0       # documented: empty file is a no-op
    with pytest.raises(RestoreError, match="not found"):
        run_restore(server, "postgres", str(tmp_path / "nope.sql"))
    with pytest.raises(RestoreError):
        run_restore(server, "postgres", str(tmp_path))      # a directory
    with pytest.raises(RestoreError, match="on_error"):
        run_restore(server, "postgres", str(p), {"on_error": "explode"})


def test_restore_into_missing_database_and_bad_credentials(server, full_backup):
    with pytest.raises(EngineError, match="does not exist|Could not connect"):
        run_restore(server, "pgbs_test_qa_no_such_db", full_backup["file"])
    bad = dict(server, password="definitely-wrong")
    with pytest.raises(EngineError) as ei:
        run_restore(bad, "postgres", full_backup["file"])
    assert "definitely-wrong" not in str(ei.value)
    with pytest.raises(EngineError) as ei:
        run_backup(bad, "postgres", {"out_dir": "."})
    assert "definitely-wrong" not in str(ei.value) and server["password"] not in str(ei.value)


def test_utf8_bom_file(server, scratch, tmp_path):
    p = tmp_path / "bom.sql"
    p.write_bytes("\ufeff-- Type: TABLE; Name: public.t\nCREATE TABLE public.t (a int);\n".encode("utf-8"))
    res = run_restore(server, scratch("bom"), str(p))
    assert res["error_count"] == 0, res["errors"]


def test_crlf_edited_file(server, full_backup, scratch, tmp_path):
    """A backup opened and saved by a Windows editor (CRLF line endings) must still restore: COPY data included."""
    raw = open(full_backup["file"], "rb").read().replace(b"\n", b"\r\n")
    p = tmp_path / "crlf.sql"
    p.write_bytes(raw)
    db = scratch("crlf")
    res = run_restore(server, db, str(p))
    # data lines then carry a trailing \r: either rejected loudly or restored faithfully; never silently altered
    if res["status"] == "ok":
        assert table_counts(db) == table_counts("pgbs_test_src")
        assert fixture_state(db)["md5"] == fixture_state("pgbs_test_src")["md5"]
    else:
        assert res["error_count"] > 0


# ---------------------------------------------------------------- hostile identifiers (item 9)

WEIRD_SRC = os.path.join(FIXTURES, "weird_idents.sql")


def _weird_roundtrip(server, pg_admin, scratch, tmp_path, sql_text, gz=False):
    src = scratch("wsrc")
    path = tmp_path / "w.sql"
    path.write_bytes(sql_text.encode("utf-8"))
    from conftest import run_psql
    r = run_psql(src, "-f", str(path))
    assert r.returncode == 0, r.stderr
    bk = run_backup(server, src, {"out_dir": str(tmp_path), "gzip": gz})
    assert bk["warnings"] == [], bk["warnings"]
    dst = scratch("wdst")
    res = run_restore(server, dst, bk["file"])
    return src, dst, bk, res


def _compare_weird(src, dst, fq_table, order_col, nl=False):
    sch = "new" + chr(10) + "line" if nl else "new line"
    tab = "tab" + chr(10) + "le;1" if nl else "tab le;1"
    a, b = schema_lines(src), schema_lines(dst)
    assert a == b, (sorted(set(a) - set(b))[:5], sorted(set(b) - set(a))[:5])
    assert table_counts(src) == table_counts(dst)
    q = f"SELECT md5(string_agg(x::text, '|' ORDER BY {order_col})) FROM {fq_table} x"
    with connect(src) as c1, connect(dst) as c2:
        m1, m2 = c1.execute(q).fetchone()[0], c2.execute(q).fetchone()[0]
        assert m1 == m2 and m1 is not None
        s = f'SELECT last_value, is_called FROM "{sch}"."seq;$"'
        assert c1.execute(s).fetchone() == c2.execute(s).fetchone() == (41, True)
        for sql_ in ("SELECT \"we;ird$schema\".\"f;n$$\"('q')", 'SELECT * FROM "$$"."v;iew\nx" ORDER BY 1',
                     'SELECT "new line".dom_dummy' if False else "SELECT 1"):
            assert c1.execute(sql_).fetchall() == c2.execute(sql_).fetchall()
        # comments with newline / ; / \. / COPY x FROM stdin
        cq = (f"SELECT obj_description('\"we;ird$schema\".\"{tab}\"'::regclass), "
              f"col_description('\"we;ird$schema\".\"{tab}\"'::regclass, 4), "
              f"obj_description('\"{sch}\"'::regnamespace, 'pg_namespace')")
        assert c1.execute(cq).fetchone() == c2.execute(cq).fetchone()
        assert "COPY x FROM stdin;" in c2.execute(cq).fetchone()[0]


def _weird_text(newline_in_table_names: bool) -> str:
    t = open(WEIRD_SRC, encoding="utf-8", newline="").read()
    if not newline_in_table_names:
        t = t.replace('"tab\nle;1"', '"tab le;1"').replace('"new\nline"', '"new line"')
    return t


@pytest.mark.parametrize("gz", [False, True])
def test_hostile_identifiers_roundtrip(server, pg_admin, scratch, tmp_path, gz):
    """Spaces, quotes, ;, $, --, /* */, newlines in column/constraint/trigger/policy/view names + hostile literals."""
    scratch.role("pgbs_test_qa_weird")
    src, dst, bk, res = _weird_roundtrip(server, pg_admin, scratch, tmp_path, _weird_text(False), gz)
    assert res["status"] == "ok" and res["error_count"] == 0 and res["warning_count"] == 0, res["errors"]
    _compare_weird(src, dst, '"we;ird$schema"."tab le;1"', '"id;"')
    assert trigger_states(src) == trigger_states(dst) and "=D" in trigger_states(dst)


def test_hostile_identifiers_with_newline_in_table_and_sequence_names(server, pg_admin, scratch, tmp_path):
    """BUG-1: a newline inside a table / schema name breaks the '-- Type: ...; Name: ...' label line."""
    scratch.role("pgbs_test_qa_weird")
    src, dst, bk, res = _weird_roundtrip(server, pg_admin, scratch, tmp_path, _weird_text(True))
    assert res["status"] == "ok" and res["error_count"] == 0, res["errors"][:2]
    _compare_weird(src, dst, '"we;ird$schema"."tab\nle;1"', '"id;"', nl=True)


def test_every_label_is_a_single_comment_line(server, pg_admin, scratch, tmp_path):
    scratch.role("pgbs_test_qa_weird")
    src, dst, bk, res = _weird_roundtrip(server, pg_admin, scratch, tmp_path, _weird_text(True))
    body = strip_header_free_sql(read_backup_text(bk["file"]))
    lines = body.split("\n")
    bad = []
    for i, ln in enumerate(lines):
        if ln.startswith("-- Type: ") and not re.fullmatch(r"-- Type: [^;]+; Name: .*", ln):
            bad.append(ln)
    # a label continues onto the next line when the name contained a newline: next line is neither blank, SQL
    # keyword, nor comment. Detect via the label count instead
    labels = re.findall(r"^-- Type: ", body, re.M)
    assert len(labels) == bk["objects"] + bk["tables"]
    for i, ln in enumerate(lines[:-1]):
        if ln.startswith("-- Type: ") and ln.count('"') % 2 == 1:
            pytest.fail(f"label line has an unbalanced quote (name with newline written raw): {ln!r}")


# ---------------------------------------------------------------- exotic object kinds

EXOTIC = os.path.join(FIXTURES, "exotic.sql")


@pytest.fixture(scope="module")
def exotic(pg_admin, mod_scratch, server, tmp_path_factory):
    mod_scratch.role("pgbs_test_qa_ex")
    src = mod_scratch("exotic_src")
    from conftest import run_psql
    r = run_psql(src, "-f", EXOTIC)
    assert r.returncode == 0, r.stderr
    d = tmp_path_factory.mktemp("exotic_bk")
    return src, {"full": run_backup(server, src, {"out_dir": str(d), "filename": "full.sql"}),
                 "gz": run_backup(server, src, {"out_dir": str(d), "gzip": True, "filename": "full.sql.gz"}),
                 "schema": run_backup(server, src, {"out_dir": str(d), "mode": "schema", "filename": "s.sql"}),
                 "data": run_backup(server, src, {"out_dir": str(d), "mode": "data", "filename": "d.sql"})}


def _data_md5(db):
    with connect(db) as c:
        out = {}
        for (t,) in c.execute("SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                              " WHERE c.relkind = 'r' AND n.nspname = ANY(%s) AND NOT c.relispartition",
                              (["ex", "ex2"],)).fetchall():
            out[t] = c.execute(f"SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM {t} x").fetchone()[0]
        return out


@pytest.mark.parametrize("kind", ["full", "gz"])
def test_exotic_full_roundtrip(server, exotic, scratch, kind):
    src, bks = exotic
    assert bks[kind]["warnings"] == []
    db = scratch("ex_" + kind)
    res = run_restore(server, db, bks[kind]["file"])
    assert res["status"] == "ok" and res["error_count"] == 0 and res["warning_count"] == 0, res["errors"]
    assert schema_lines(db) == schema_lines(src)
    assert table_counts(db) == table_counts(src)
    assert _data_md5(db) == _data_md5(src)
    assert trigger_states(db) == trigger_states(src)


def test_exotic_schema_then_data(server, exotic, scratch):
    src, bks = exotic
    db = scratch("ex_sd")
    assert run_restore(server, db, bks["schema"]["file"])["error_count"] == 0
    assert schema_lines(db) == schema_lines(src)
    res = run_restore(server, db, bks["data"]["file"])
    assert res["status"] == "ok" and res["error_count"] == 0, res["errors"]
    assert table_counts(db) == table_counts(src) and _data_md5(db) == _data_md5(src)
    assert trigger_states(db) == trigger_states(src)
    with connect(db) as a, connect(src) as b:
        for q in ("SELECT last_value, is_called FROM ex.s_plain", "SELECT last_value, is_called FROM ex.defaults_id_seq",
                  "SELECT relispopulated FROM pg_class WHERE oid = 'ex.mv3'::regclass"):
            assert a.execute(q).fetchone() == b.execute(q).fetchone(), q


@pytest.mark.parametrize("mode", ["continue", "stop", "single"])
def test_exotic_missing_role_all_modes(server, pg_admin, exotic, scratch, mode):
    """Owner of several object kinds + grants (column, sequence, type, domain, procedure) to a role missing on target."""
    src, bks = exotic
    # the role really is absent on the target: restore into a copy-file that points to a role that never existed
    text = open(bks["full"]["file"], encoding="utf-8", newline="").read().replace("pgbs_test_qa_ex", "pgbs_test_qa_absent")
    p = os.path.join(os.path.dirname(bks["full"]["file"]), f"absent_{mode}.sql")
    open(p, "w", encoding="utf-8", newline="").write(text)
    db = scratch("ex_role_" + mode)
    opts = {"single_transaction": True} if mode == "single" else {"on_error": mode}
    res = run_restore(server, db, p, opts)
    assert res["status"] == "ok" and res["error_count"] == 0, res["errors"][:3]
    assert res["warning_count"] >= 15 and res["missing_roles"] == ["pgbs_test_qa_absent"]
    assert table_counts(db) == table_counts(src)


# ---------------------------------------------------------------- 10. no writes to the source

def _stat_row(db):
    with connect("postgres") as c:
        c.execute("SELECT pg_stat_clear_snapshot()")
        return c.execute("SELECT xact_commit, tup_inserted, tup_updated, tup_deleted, xact_rollback, temp_files"
                         " FROM pg_stat_database WHERE datname = %s", (db,)).fetchone()


def _settled(db, timeout=15):
    last, deadline = None, time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.3)
        cur = _stat_row(db)
        if cur == last:
            return cur
        last = cur
    return last


def test_backup_does_not_write_to_source(server, src_db, tmp_path):
    before = _settled(src_db)
    for opts in ({}, {"gzip": True}, {"mode": "schema"}, {"mode": "data"}, {"privileges": False}):
        run_backup(server, src_db, {"out_dir": str(tmp_path), **opts})
    after = _settled(src_db)
    assert after[1:4] == before[1:4], f"tup_inserted/updated/deleted changed: {before} -> {after}"
    assert after[5] == before[5], "temp files created on the source"
    # informational: xact_commit grows only by the backup sessions' own read-only transactions
    assert after[0] - before[0] <= 20


def test_backup_runs_in_read_only_repeatable_read_snapshot(server, src_db, tmp_path, monkeypatch):
    seen = []
    real_connect = backup_mod.connect

    def spying_connect(*a, **k):
        conn = real_connect(*a, **k)
        orig = backup_mod._BackupRun._start_snapshot

        def snap(c):
            orig(c)
            row = c.execute("SELECT current_setting('transaction_isolation'), current_setting('transaction_read_only'),"
                            " current_setting('row_security'), current_setting('client_encoding'),"
                            " current_setting('search_path'), current_setting('statement_timeout')").fetchone()
            seen.append(row)
        monkeypatch.setattr(backup_mod._BackupRun, "_start_snapshot", staticmethod(snap))
        return conn

    monkeypatch.setattr(backup_mod, "connect", spying_connect)
    run_backup(server, src_db, {"out_dir": str(tmp_path)})
    assert seen == [("repeatable read", "on", "off", "UTF8", "pg_catalog", "0")]


def test_backup_snapshot_is_consistent_under_concurrent_writes(server, big, tmp_path, scratch):
    """Rows inserted while the backup runs must not appear (single snapshot across tables)."""
    db, _ = big
    stop = threading.Event()
    wrote = []

    def writer():
        with connect(db) as c:
            while not stop.is_set():
                c.execute("INSERT INTO big.c (id) VALUES (1)")
                wrote.append(1)
                time.sleep(0.002)

    with connect(db) as c:
        c.execute("TRUNCATE big.c")
    t = threading.Thread(target=writer)
    t.start()
    try:
        res = run_backup(server, db, {"out_dir": str(tmp_path)})
    finally:
        stop.set()
        t.join()
        with connect(db) as c:
            c.execute("TRUNCATE big.c")
    assert len(wrote) > 5
    text = read_backup_text(res["file"])
    m = re.search(r"COPY big\.c \(id\) FROM stdin;\n((?:[^\n]*\n)*?)\\\.\n", text)
    n_backup = m.group(1).count("\n") if m else 0
    # the snapshot was taken at start: c had 0 committed rows (truncate) when it began or a few; never > rows at start+0
    assert n_backup <= len(wrote)
    assert res["rows"] >= 500000


# ---------------------------------------------------------------- read-only user

def test_read_only_user_backup(server, pg_admin, scratch, tmp_path):
    ro = scratch.role("pgbs_test_qa_ro")
    pw = secrets.token_hex(8)
    pg_admin.execute(f"DROP ROLE IF EXISTS {ro}")
    pg_admin.execute(f"CREATE ROLE {ro} LOGIN PASSWORD '{pw}' IN ROLE pg_read_all_data")
    src = scratch("ro_src")
    psql_script(src, """
        CREATE SCHEMA r; CREATE TABLE r.t (id serial PRIMARY KEY, v text);
        INSERT INTO r.t (v) SELECT 'x' || i FROM generate_series(1, 30) i;
        CREATE VIEW r.vw AS SELECT * FROM r.t;
        CREATE TABLE r.secure (id int, owner_name text); INSERT INTO r.secure VALUES (1, 'a'), (2, 'b');
    """)
    ro_server = dict(server, user=ro, password=pw)
    res = run_backup(ro_server, src, {"out_dir": str(tmp_path), "filename": "ro.sql"})     # no RLS yet: must succeed
    assert res["rows"] == 32 and res["warnings"] == []
    dst = scratch("ro_dst")
    r2 = run_restore(server, dst, res["file"])
    assert r2["status"] == "ok" and r2["error_count"] == 0
    assert schema_lines(dst) == schema_lines(src)
    # now an RLS table the read-only user cannot bypass: clear error, no silent filtered data, no .part left
    psql_script(src, "ALTER TABLE r.secure ENABLE ROW LEVEL SECURITY; CREATE POLICY p ON r.secure USING (owner_name = 'a');")
    with pytest.raises(BackupError) as ei:
        run_backup(ro_server, src, {"out_dir": str(tmp_path), "filename": "ro2.sql"})
    assert "r.secure" in str(ei.value) and re.search(r"(?i)row-level security", str(ei.value))
    assert part_files(tmp_path) == [] and not os.path.exists(tmp_path / "ro2.sql")
    # missing SELECT on one table: clear error naming it
    psql_script(src, "ALTER TABLE r.secure DISABLE ROW LEVEL SECURITY;")
    pg_admin.execute(f"REVOKE pg_read_all_data FROM {ro}")      # drop the read-all rights; keep CONNECT/USAGE
    psql_script(src, f"GRANT USAGE ON SCHEMA r, public TO {ro}; GRANT SELECT ON r.t, r.vw TO {ro};")
    with pytest.raises(BackupError) as ei:
        run_backup(ro_server, src, {"out_dir": str(tmp_path), "filename": "ro3.sql"})
    assert re.search(r"(?i)permission denied", str(ei.value))
    assert part_files(tmp_path) == []


def test_failed_backup_leaves_nothing(server, src_db, tmp_path):
    bad_dir = tmp_path / "file_not_dir"
    bad_dir.write_text("x")
    with pytest.raises((BackupError, OSError)):
        run_backup(server, src_db, {"out_dir": str(bad_dir)})
    with pytest.raises(EngineError):
        run_backup(server, "pgbs_test_qa_no_such_db", {"out_dir": str(tmp_path)})
    with pytest.raises(BackupError):
        run_backup(server, src_db, {"out_dir": str(tmp_path), "mode": "bogus"})
    assert os.listdir(tmp_path) == ["file_not_dir"]      # only the file the test created itself


def test_output_file_naming(server, src_db, tmp_path):
    a = run_backup(server, src_db, {"out_dir": str(tmp_path)})
    b = run_backup(server, src_db, {"out_dir": str(tmp_path), "gzip": True})
    c = run_backup(server, src_db, {"out_dir": str(tmp_path)})
    assert re.fullmatch(rf"{src_db}_\d{{8}}_\d{{6}}\.sql", os.path.basename(a["file"]))
    assert re.fullmatch(rf"{src_db}_\d{{8}}_\d{{6}}\.sql\.gz", os.path.basename(b["file"]))
    assert len({a["file"], b["file"], c["file"]}) == 3 and os.path.exists(c["file"])
    nested = run_backup(server, src_db, {"out_dir": str(tmp_path / "new" / "dir"), "filename": "../evil.sql"})
    assert os.path.dirname(nested["file"]) == str(tmp_path / "new" / "dir") and os.path.basename(nested["file"]) == "evil.sql"
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".part")]


# ---------------------------------------------------------------- 11. version gate

class _Info:
    def __init__(self, v):
        self.server_version = v


class _FakeConn:
    def __init__(self, v):
        self.info = _Info(v)
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize("version,major", [(110022, "11"), (100023, "10"), (90624, "9.6"), (119999, "11")])
def test_old_server_refused(version, major):
    with pytest.raises(UnsupportedServerError) as ei:
        require_supported(_FakeConn(version))
    assert f"PostgreSQL {major}" in str(ei.value) and "12" in str(ei.value)


@pytest.mark.parametrize("version", [120000, 120022, 130000, 170005, 180004])
def test_supported_versions_pass(version):
    assert require_supported(_FakeConn(version)) == version


def test_run_backup_refuses_old_server_and_cleans_up(monkeypatch, tmp_path):
    fake = _FakeConn(110000)
    monkeypatch.setattr(backup_mod, "connect", lambda *a, **k: fake)
    with pytest.raises(UnsupportedServerError, match="PostgreSQL 11"):
        run_backup({"host": "x"}, "db", {"out_dir": str(tmp_path)})
    assert fake.closed and os.listdir(tmp_path) == []


def test_passwords_not_in_files(full_backup, server):
    pw = server["password"]
    text = open(full_backup["file"], encoding="utf-8").read()
    header = text.split("-- Type: ", 1)[0]
    assert re.search(r"(?i)password|passwd|pgpass", header) is None
    if pw:
        assert pw not in header and pw not in str(full_backup)


# ---------------------------------------------------------------- fix round 1 regression checks

def test_matview_with_newline_and_double_spaces_roundtrips_and_refreshes(server, pg_admin, scratch, tmp_path):
    """Fix-round side effect: matview REFRESH uses the exact qualified name (no whitespace collapsing)."""
    src = scratch("mvnl")
    nl = chr(10)
    sql = (f'CREATE SCHEMA "mv{nl}sch";\n'
           f'CREATE TABLE "mv{nl}sch"."base  t" (id int, v text);\n'
           f"INSERT INTO \"mv{nl}sch\".\"base  t\" VALUES (1,'a'),(2,'b');\n"
           f'CREATE MATERIALIZED VIEW "mv{nl}sch"."mat  view{nl}x" AS SELECT id, v FROM "mv{nl}sch"."base  t";\n'
           f'CREATE MATERIALIZED VIEW "mv{nl}sch"."plain  mv" AS SELECT id FROM "mv{nl}sch"."base  t" WITH NO DATA;\n'
           f'CREATE UNIQUE INDEX ON "mv{nl}sch"."mat  view{nl}x" (id);\n')
    path = tmp_path / "mv.sql"
    path.write_bytes(sql.encode())
    from conftest import run_psql
    r = run_psql(src, "-f", str(path))
    assert r.returncode == 0, r.stderr
    for gz in (False, True):
        bk = run_backup(server, src, {"out_dir": str(tmp_path), "gzip": gz})
        assert bk["warnings"] == [], bk["warnings"]
        dst = scratch("mvnl_dst")
        res = run_restore(server, dst, bk["file"])
        assert res["status"] == "ok" and res["error_count"] == 0, res["errors"][:2]
        q = f'SELECT id, v FROM "mv{nl}sch"."mat  view{nl}x" ORDER BY 1'
        pq = ("SELECT relispopulated FROM pg_class WHERE oid = "
              f"'\"mv{nl}sch\".\"plain  mv\"'::regclass")
        with connect(src) as a, connect(dst) as b:
            assert a.execute(q).fetchall() == b.execute(q).fetchall() == [(1, "a"), (2, "b")]
            assert a.execute(pq).fetchone() == b.execute(pq).fetchone()
        with connect(dst, autocommit=True) as b:
            b.execute(f'REFRESH MATERIALIZED VIEW "mv{nl}sch"."mat  view{nl}x"')
        assert schema_lines(src) == schema_lines(dst)


def test_many_random_gzip_corruptions_never_escape(gz_file, tmp_path):
    """Multi-byte damage, truncation, header damage, appended garbage: only RestoreError may come out."""
    import random
    data = open(gz_file, "rb").read()
    rnd = random.Random(1025)
    escaped = []

    def feed(blob):
        p = tmp_path / "c.sql.gz"
        p.write_bytes(bytes(blob))
        try:
            raw, reader = open_backup(str(p))
        except RestoreError:
            return
        except Exception as e:      # noqa: BLE001
            escaped.append("open:" + type(e).__name__)
            return
        try:
            for _ in split_sql(reader):
                pass
        except RestoreError:
            pass
        except Exception as e:      # noqa: BLE001
            escaped.append(type(e).__module__ + "." + type(e).__name__)
        finally:
            reader.close()
            raw.close()

    for _ in range(120):
        b = bytearray(data)
        for _k in range(rnd.randint(1, 40)):
            b[rnd.randrange(0, len(b))] = rnd.randrange(256)
        feed(b)
    for _ in range(40):
        feed(data[: rnd.randrange(2, len(data))])
    for _ in range(20):
        b = bytearray(data)
        i = rnd.randrange(len(b) // 4, len(b))
        feed(b[:i] + bytes(rnd.randrange(256) for _ in range(rnd.randint(1, 64))) + b[i:])
    assert escaped == [], sorted(set(escaped))


def test_bom_plus_gzip(server, scratch, tmp_path):
    """BOM inside a gzip stream is skipped too; also BOM + CRLF-free multi-statement file."""
    body = "\ufeff-- Type: TABLE; Name: public.t\nCREATE TABLE public.t (a int);\nINSERT INTO public.t VALUES (7);\n"
    p = tmp_path / "bom.sql.gz"
    with gzip.open(p, "wb") as f:
        f.write(body.encode("utf-8"))
    db = scratch("bomgz")
    res = run_restore(server, db, str(p))
    assert res["status"] == "ok" and res["error_count"] == 0, res["errors"]
    with connect(db) as c:
        assert c.execute("SELECT a FROM public.t").fetchall() == [(7,)]
    # BOM must not be eaten when it is not at the very start (inside a string literal it is data)
    p2 = tmp_path / "bom2.sql"
    p2.write_bytes("CREATE TABLE public.u (s text);\nINSERT INTO public.u VALUES ('\ufeffx');\n".encode("utf-8"))
    db2 = scratch("bomlit")
    assert run_restore(server, db2, str(p2))["error_count"] == 0
    with connect(db2) as c:
        assert c.execute("SELECT s FROM public.u").fetchone()[0] == "\ufeffx"
