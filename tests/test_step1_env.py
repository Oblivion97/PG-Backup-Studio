"""QA: Step 1 environment / robustness cases (encodings, odd names, db-level settings, disk full, big rows).

Fixtures are shared with test_step1_restore.py. Scratch DBs pgbs_test_qa_*, roles pgbs_test_qa_*.
"""
import os
import re
import threading
import time

import pytest

from conftest import connect, drop_database
from engine import backup as backup_mod
from engine.backup import run_backup
from engine.restore import run_restore
from engine.util import BackupError, Cancelled, EngineError
from step1_helpers import (QA_PREFIX, create_db, psql_script, read_backup_text, schema_lines, table_counts)
from test_step1_restore import exotic, mod_scratch, part_files, scratch, server  # noqa: F401  (fixtures)


def test_empty_schema_selection_is_refused(server, src_db, tmp_path):
    """BUG-4 (minor): schemas=[] yields a 'successful' backup with zero objects."""
    with pytest.raises(EngineError):
        res = run_backup(server, src_db, {"out_dir": str(tmp_path), "schemas": []})
        assert res["objects"] > 0, "an empty backup was reported as success"


def test_backup_cancel_while_waiting_for_table_lock(server, pg_admin, scratch, tmp_path):
    src = scratch("lockcancel")
    psql_script(src, "CREATE TABLE public.t (a int); INSERT INTO public.t VALUES (1);")
    holder = connect(src, autocommit=False)
    holder.execute("LOCK TABLE public.t IN ACCESS EXCLUSIVE MODE")
    ev, out = threading.Event(), {}

    def go():
        try:
            run_backup(server, src, {"out_dir": str(tmp_path)}, None, ev)
            out["r"] = "finished"
        except Cancelled:
            out["r"] = "cancelled"
        except Exception as e:      # noqa: BLE001
            out["r"] = repr(e)

    th = threading.Thread(target=go)
    th.start()
    try:
        time.sleep(1.5)
        ev.set()
        th.join(6)
        alive = th.is_alive()
    finally:
        holder.rollback()
        holder.close()
        th.join(30)
    assert not alive, "backup still blocked on the lock 6 s after Cancel"
    assert out["r"] == "cancelled" and part_files(tmp_path) == []


def test_disk_full_during_backup_leaves_no_part(server, src_db, tmp_path, monkeypatch):
    calls = {"n": 0}
    real = backup_mod._Output.write

    def failing(self, data):
        calls["n"] += 1
        if calls["n"] == 40:
            raise OSError(28, "No space left on device")
        return real(self, data)

    monkeypatch.setattr(backup_mod._Output, "write", failing)
    with pytest.raises(BackupError, match="(?i)write|space"):
        run_backup(server, src_db, {"out_dir": str(tmp_path)})
    assert os.listdir(tmp_path) == []


def test_database_name_with_odd_characters(server, pg_admin, tmp_path):
    name = 'pgbs_test_qa_odd "name"\nx y'
    quoted = '"' + name.replace('"', '""') + '"'
    pg_admin.execute(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
    pg_admin.execute(f"CREATE DATABASE {quoted} TEMPLATE template0 ENCODING 'UTF8'")
    dst = None
    try:
        psql_script(name, "CREATE TABLE public.t (a int); INSERT INTO public.t VALUES (1), (2);")
        res = run_backup(server, name, {"out_dir": str(tmp_path)})
        assert os.path.dirname(res["file"]) == str(tmp_path) and res["rows"] == 2
        assert not re.search(r'[<>:"/\\|?*\x00-\x1f]', os.path.basename(res["file"]))
        head = read_backup_text(res["file"]).split("\n\n", 1)[0]
        assert all(ln.startswith("--") for ln in head.split("\n")), "header line broke out of its comment"
        dst = create_db(pg_admin, "odd_dst")
        assert run_restore(server, dst, res["file"])["status"] == "ok"
        assert table_counts(dst) == {"t": 2}
    finally:
        pg_admin.execute(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
        if dst:
            drop_database(pg_admin, dst)


def test_huge_row_and_all_byte_values(server, pg_admin, scratch, tmp_path):
    src = scratch("huge")
    psql_script(src, r"""
        CREATE TABLE public.h (id int PRIMARY KEY, big text, b bytea);
        INSERT INTO public.h SELECT 1, repeat('x;\''"', 3000000),
          (SELECT decode(string_agg(lpad(to_hex(i), 2, '0'), ''), 'hex') FROM generate_series(0, 255) i);
        INSERT INTO public.h SELECT 2, NULL, (SELECT decode(string_agg(lpad(to_hex(255 - i), 2, '0'), ''), 'hex')
          FROM generate_series(0, 255) i);
    """)
    bk = run_backup(server, src, {"out_dir": str(tmp_path), "gzip": True})
    dst = scratch("huge_dst")
    res = run_restore(server, dst, bk["file"])
    assert res["status"] == "ok" and res["error_count"] == 0, res["errors"]
    q = "SELECT md5(string_agg(h::text, '|' ORDER BY id)), sum(length(big)), sum(length(b)) FROM public.h h"
    with connect(src) as a, connect(dst) as b:
        assert a.execute(q).fetchone() == b.execute(q).fetchone()


def _make_encoded_db(admin, suffix, encoding):
    name = QA_PREFIX + suffix
    drop_database(admin, name)
    admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING '{encoding}' LC_COLLATE 'C' LC_CTYPE 'C'")
    return name


def test_latin1_source_to_utf8_target_and_back(server, pg_admin, scratch, tmp_path):
    src = _make_encoded_db(pg_admin, "latin_src", "LATIN1")
    dst2 = None
    try:
        psql_script(src, "SET client_encoding = 'UTF8'; CREATE TABLE public.t (id int, s text, \"café\" int);"
                         "INSERT INTO public.t VALUES (1, 'café üñ', 1), (2, E'tab\\there', NULL);"
                         "COMMENT ON TABLE public.t IS 'déjà vu';")
        bk = run_backup(server, src, {"out_dir": str(tmp_path)})
        assert "café" in read_backup_text(bk["file"])          # file is UTF-8 whatever the source encoding
        dst = scratch("latin_to_utf8")
        res = run_restore(server, dst, bk["file"])
        assert res["status"] == "ok", res["errors"]
        with connect(dst) as c:
            assert c.execute("SELECT s FROM public.t WHERE id = 1").fetchone()[0] == "café üñ"
            assert c.execute("SELECT obj_description('public.t'::regclass)").fetchone()[0] == "déjà vu"
        dst2 = _make_encoded_db(pg_admin, "latin_dst", "LATIN1")      # UTF8 backup into a LATIN1 target
        res2 = run_restore(server, dst2, bk["file"])
        assert res2["status"] == "ok", res2["errors"]
        with connect(dst2) as c:
            assert c.execute("SELECT s FROM public.t WHERE id = 1").fetchone()[0] == "café üñ"
    finally:
        drop_database(pg_admin, src)
        if dst2:
            drop_database(pg_admin, dst2)


def test_unrepresentable_characters_in_target_encoding_are_loud(server, pg_admin, src_db, tmp_path):
    dst = _make_encoded_db(pg_admin, "latin_loud", "LATIN1")
    try:
        bk = run_backup(server, src_db, {"out_dir": str(tmp_path), "schemas": ["app", "audit"]})   # has CJK / emoji
        res = run_restore(server, dst, bk["file"])
        assert res["status"] == "completed_with_errors" and res["error_count"] >= 1
        assert any(e["sqlstate"] in ("22P05", "22021") for e in res["errors"]), res["errors"][:3]
    finally:
        drop_database(pg_admin, dst)


def test_target_database_level_settings_do_not_alter_restore(server, pg_admin, exotic, scratch):
    """Header resets session settings: hostile database-level defaults must not change the result."""
    src, bks = exotic
    db = scratch("dbsettings")
    for setting in ("standard_conforming_strings = off", "statement_timeout = '1ms'", "lock_timeout = '1ms'",
                    "datestyle = 'German, DMY'", "intervalstyle = 'iso_8601'", "search_path = nowhere",
                    "row_security = on", "client_min_messages = debug1", "default_transaction_read_only = off",
                    "check_function_bodies = on", "xmloption = document", "bytea_output = 'escape'",
                    "extra_float_digits = -3"):
        pg_admin.execute(f'ALTER DATABASE "{db}" SET {setting}')
    res = run_restore(server, db, bks["full"]["file"])
    assert res["status"] == "ok" and res["error_count"] == 0, res["errors"][:3]
    pg_admin.execute(f'ALTER DATABASE "{db}" RESET ALL')      # pg_dump echoes db-level settings: compare on defaults
    assert schema_lines(db) == schema_lines(src)
    with connect(db) as a, connect(src) as b:
        for c in (a, b):
            c.execute("SET datestyle = 'ISO, MDY'")
            c.execute("SET intervalstyle = 'postgres'")
            c.execute("SET standard_conforming_strings = on")
            c.execute("SET bytea_output = 'hex'")
            c.execute("SET extra_float_digits = 3")
            c.execute("SET search_path = pg_catalog")
        for t in ("ex.types", "ex.defaults"):
            q = f"SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM {t} x"
            assert a.execute(q).fetchone() == b.execute(q).fetchone(), t
        for q in ("SELECT obj_description('ex.defaults'::regclass)",
                  "SELECT obj_description('ex.defaults'::regclass), col_description('ex.defaults'::regclass, 2)"):
            assert a.execute(q).fetchone() == b.execute(q).fetchone()


def test_attached_partition_with_other_column_order(server, scratch, tmp_path):
    """Documented limitation: column order may change; the data must still land in the right columns."""
    src = scratch("attach_src")
    psql_script(src, """
        CREATE TABLE public.p (id int, kind text, v text) PARTITION BY LIST (kind);
        CREATE TABLE public.p_a (v text, kind text, id int);
        ALTER TABLE public.p ATTACH PARTITION public.p_a FOR VALUES IN ('a');
        CREATE TABLE public.p_b PARTITION OF public.p FOR VALUES IN ('b');
        INSERT INTO public.p VALUES (1, 'a', 'va'), (2, 'b', 'vb'), (3, 'a', 'vc');
    """)
    bk = run_backup(server, src, {"out_dir": str(tmp_path)})
    dst = scratch("attach_dst")
    res = run_restore(server, dst, bk["file"])
    assert res["status"] == "ok" and res["error_count"] == 0, res["errors"]
    q = "SELECT id, kind, v FROM public.p ORDER BY id"
    with connect(src) as a, connect(dst) as b:
        assert a.execute(q).fetchall() == b.execute(q).fetchall() == [(1, "a", "va"), (2, "b", "vb"), (3, "a", "vc")]
        q2 = "SELECT id, kind, v FROM ONLY public.p_a ORDER BY id"
        assert a.execute(q2).fetchall() == b.execute(q2).fetchall()


def test_not_included_more_kinds(server, pg_admin, scratch, tmp_path):
    src = scratch("notinc2")
    role = scratch.role("pgbs_test_qa_dp")
    pg_admin.execute(f"DROP ROLE IF EXISTS {role}")
    pg_admin.execute(f"CREATE ROLE {role} NOLOGIN")
    psql_script(src, f"""
        CREATE FOREIGN DATA WRAPPER qa_fdw; CREATE SERVER qa_srv FOREIGN DATA WRAPPER qa_fdw;
        CREATE FOREIGN TABLE public.ft (a int) SERVER qa_srv;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {role};
        SELECT lo_create(0);
        CREATE FUNCTION public.qa_evt() RETURNS event_trigger LANGUAGE plpgsql AS 'BEGIN NULL; END';
        CREATE EVENT TRIGGER qa_evt ON ddl_command_start EXECUTE FUNCTION public.qa_evt();
    """)
    res = run_backup(server, src, {"out_dir": str(tmp_path)})
    joined = " | ".join(res["warnings"])
    for what in ("foreign tables", "default privileges", "large objects", "event triggers", "foreign servers"):
        assert what in joined, (what, joined)
    head = read_backup_text(res["file"]).split("SET statement_timeout", 1)[0]
    assert "NOT INCLUDED" in head and "large objects" in head


def test_non_superuser_restore_owner_statements(server, pg_admin, src_db, scratch, tmp_path):
    """Restoring as a plain (non-superuser) owner: data restores; OWNER TO <other role> errors are loud, and
    a backup made with owners & grants off restores with zero errors."""
    app = scratch.role("pgbs_test_qa_app")
    pg_admin.execute(f"DROP ROLE IF EXISTS {app}")
    pw = __import__("secrets").token_hex(8)
    pg_admin.execute(f"CREATE ROLE {app} LOGIN CREATEDB PASSWORD '{pw}'")
    srv = dict(server, user=app, password=pw)
    for privileges in (False, True):
        bk = run_backup(server, src_db, {"out_dir": str(tmp_path), "privileges": privileges,
                                         "filename": f"p{privileges}.sql"})
        dst = scratch(f"nonsuper_{privileges}")
        pg_admin.execute(f'ALTER DATABASE "{dst}" OWNER TO {app}')
        psql_script(dst, f"GRANT ALL ON SCHEMA public TO {app}")
        res = run_restore(srv, dst, bk["file"])
        assert sum(table_counts(dst).values()) == sum(table_counts(src_db).values())
        if not privileges:
            assert res["status"] == "ok" and res["error_count"] == 0, res["errors"][:3]
        else:
            assert res["error_count"] > 0 and {e["sqlstate"] for e in res["errors"]} == {"42501"}
            assert all("OWNER TO" in e["sql"] for e in res["errors"]), res["errors"][:3]
