"""Functions-only backup (one .sql file per function) and the "all tables" backup panel.

File naming rules are tested without a database; the round trip needs PostgreSQL (PGPASSWORD).
"""
import os
import re
from pathlib import Path

import pytest

from app import create_app
from conftest import PREFIX, connect, drop_database, pg_settings
from engine.backup import BackupOptions, run_backup
from engine.functions import file_names, safe_stem
from engine.restore import run_restore
from engine.util import BackupError
from jobs import JobManager
from store import Store

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
FN_DB = f"{PREFIX}functions"


def fn(oid, name, schema="public", sig=None):
    return {"oid": oid, "name": name, "schema": schema, "sig": sig or f"{schema}.{name}()"}


# ---------------------------------------------------------------- file names (no database)

def test_unique_names_keep_the_plain_name():
    assert file_names([fn(1, "calc_total"), fn(2, "audit")]) == {1: "calc_total.sql", 2: "audit.sql"}


def test_repeated_names_get_1_2_in_a_fixed_order():
    funcs = [fn(30, "calc", sig="public.calc(text)"), fn(10, "calc", sig="public.calc(integer)"),
             fn(20, "calc", schema="other", sig="other.calc()")]
    names = file_names(funcs)
    # order: name, schema, signature -> other.calc(), public.calc(integer), public.calc(text)
    assert names == {20: "calc.sql", 10: "calc1.sql", 30: "calc2.sql"}
    assert file_names(list(reversed(funcs))) == names


def test_repeats_are_case_insensitive_like_windows_file_names():
    names = file_names([fn(1, "Calc"), fn(2, "calc")])
    assert sorted(names.values(), key=str.lower) == ["Calc.sql", "calc1.sql"]


def test_a_real_function_named_like_a_number_suffix_keeps_its_name():
    names = file_names([fn(1, "calc", sig="public.calc(integer)"), fn(2, "calc", sig="public.calc(text)"), fn(3, "calc1")])
    assert names[3] == "calc1.sql"
    assert names[1] == "calc.sql" and names[2] == "calc2.sql"


def test_unsafe_and_reserved_names_become_safe_file_names():
    assert safe_stem('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j"
    assert safe_stem("con") == "con_" and safe_stem("LPT1") == "LPT1_"
    assert safe_stem("trailing. ") == "trailing"
    assert safe_stem("...") == "function"
    assert len(safe_stem("x" * 300)) == 120


def test_functions_is_a_valid_backup_mode():
    BackupOptions.from_value({"mode": "functions"})
    with pytest.raises(BackupError):
        BackupOptions.from_value({"mode": "nope"})


# ---------------------------------------------------------------- store, API (no database)

def test_deleting_a_functions_entry_removes_its_files_and_folder(tmp_path):
    st = Store(tmp_path / "data")
    folder = tmp_path / "shop_functions_x"
    folder.mkdir()
    (folder / "a.sql").write_text("select 1;")
    (folder / "b.sql").write_text("select 2;")
    entry = st.add_history({"kind": "backup", "status": "success", "mode": "functions", "file": str(folder),
                            "database": "shop", "started": "2026-10-28T10:00:00+00:00"})
    res = st.delete_history(entry["id"], delete_file=True)
    assert res["file_deleted"] is True
    assert not folder.exists()


def test_deleting_keeps_files_it_did_not_write(tmp_path):
    st = Store(tmp_path / "data")
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "a.sql").write_text("select 1;")
    (folder / "notes.txt").write_text("mine")
    entry = st.add_history({"kind": "backup", "status": "success", "mode": "functions", "file": str(folder),
                            "database": "shop", "started": "2026-10-28T10:00:00+00:00"})
    st.delete_history(entry["id"], delete_file=True)
    assert (folder / "notes.txt").exists() and not (folder / "a.sql").exists()


def test_a_functions_backup_cannot_be_restored_from_history(tmp_path):
    st = Store(tmp_path / "data")
    sid = st.save_server({"name": "L", "host": "localhost", "port": 5432, "user": "postgres", "password": "pw"})["id"]
    entry = st.add_history({"kind": "backup", "status": "success", "mode": "functions", "file": str(tmp_path),
                            "database": "shop", "started": "2026-10-28T10:00:00+00:00"})
    client = create_app(st, JobManager(st)).test_client()
    r = client.post("/api/restore", json={"server_id": sid, "database": "x", "history_id": entry["id"]},
                    headers={"X-PGBS": "1"})
    assert r.status_code == 400
    assert "one file at a time" in r.get_json()["error"]


# ---------------------------------------------------------------- UI (static)

@pytest.fixture(scope="module")
def script() -> str:
    # the inline script plus backup.js, which holds the backup panel's table list
    inline = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)[0]
    return inline + "\n" + (PAGE.parent / "backup.js").read_text(encoding="utf-8")


def test_ui_offers_functions_mode_and_skips_the_table_check(script):
    assert "['functions', 'Functions only'" in script
    assert "if (b.mode === 'functions') { refreshBackup(); return null; }" in script
    assert "h.mode !== 'functions'" in script   # not offered as a restore source


def test_ui_lists_every_table_with_pages_and_bulk_options(script):
    assert "largeOnly" not in script            # no more "large tables only" by default
    assert "show: 'all'" in script
    assert "const TABLE_PAGE = 100;" in script
    assert "Select all ${fmt.num(matched.length)} matching" in script
    assert "setMany(b.sel, v)" in script


def test_ui_table_list_has_list_and_grid_views(script):
    assert "[['list', 'List'], ['grid', 'Grid']]" in script
    assert "store('tableView', v)" in script         # the chosen view is remembered per browser
    assert "function sizeBounds(tables)" in script and "tableTile(b, t, schemaMode, draw)" in script


# ---------------------------------------------------------------- round trip (PostgreSQL)

FUNCTIONS_SQL = """
CREATE SCHEMA other;
CREATE FUNCTION public.calc(a integer) RETURNS integer LANGUAGE sql AS 'SELECT a * 2';
CREATE FUNCTION public.calc(a text) RETURNS text LANGUAGE sql AS 'SELECT a || a';
CREATE FUNCTION other.calc() RETURNS integer LANGUAGE sql AS 'SELECT 42';
CREATE FUNCTION public.calc1() RETURNS integer LANGUAGE sql AS 'SELECT 1';
CREATE FUNCTION public."Weird/Name"() RETURNS integer LANGUAGE plpgsql AS $$BEGIN RETURN 7; END$$;
CREATE PROCEDURE public.do_work(n integer) LANGUAGE plpgsql AS $$BEGIN PERFORM n; END$$;
COMMENT ON FUNCTION public.calc(integer) IS 'doubles';
CREATE TABLE public.t (id int PRIMARY KEY);
"""


@pytest.fixture
def fn_db(pg_admin):
    drop_database(pg_admin, FN_DB)
    pg_admin.execute(f'CREATE DATABASE "{FN_DB}" TEMPLATE template0')
    with connect(FN_DB) as c:
        c.execute(FUNCTIONS_SQL)
    yield FN_DB
    drop_database(pg_admin, FN_DB)


def server() -> dict:
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


def test_functions_backup_writes_one_file_per_function(fn_db, tmp_path):
    events = []
    res = run_backup(server(), fn_db, {"mode": "functions", "out_dir": str(tmp_path)},
                     progress=lambda e, d: events.append(e))
    folder = Path(res["file"])
    assert folder.is_dir() and folder.parent == tmp_path and "_functions_" in folder.name
    assert not list(tmp_path.glob("*.part"))
    files = sorted(p.name for p in folder.iterdir())
    assert files == ["Weird_Name.sql", "calc.sql", "calc1.sql", "calc2.sql", "calc3.sql", "do_work.sql"]
    assert res["objects"] == 6 and res["renamed"] == 2 and res["mode"] == "functions"
    assert res["size"] == sum(p.stat().st_size for p in folder.iterdir())
    by_file = {f["file"]: f["name"] for f in res["functions"]}
    assert by_file["calc.sql"] == "other.calc()"
    assert by_file["calc1.sql"] == "public.calc1()"      # the real calc1 keeps its name
    assert by_file["calc2.sql"].startswith("public.calc(integer")
    assert "CREATE OR REPLACE PROCEDURE public.do_work" in (folder / "do_work.sql").read_text(encoding="utf-8")
    assert "COMMENT ON FUNCTION" in (folder / "calc2.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE" not in "".join(p.read_text(encoding="utf-8") for p in folder.iterdir())
    assert {"file", "totals", "object"} <= set(events)


def test_function_files_restore_on_their_own(fn_db, tmp_path, pg_admin, restore_db):
    res = run_backup(server(), fn_db, {"mode": "functions", "out_dir": str(tmp_path), "privileges": False})
    target = restore_db("fn")
    pg_admin.execute(f'CREATE DATABASE "{target}" TEMPLATE template0')
    with connect(target) as c:
        c.execute("CREATE SCHEMA other")
    for f in Path(res["file"]).iterdir():
        out = run_restore(server(), target, str(f))
        assert out["status"] == "ok" and not out["errors"], (f.name, out["errors"])
    with connect(target) as c:
        assert c.execute("SELECT public.calc(21), public.calc('ab'), other.calc()").fetchone() == (42, "abab", 42)


def test_schema_filter_and_folder_name(fn_db, tmp_path):
    res = run_backup(server(), fn_db, {"mode": "functions", "out_dir": str(tmp_path), "schemas": ["other"],
                                       "filename": "only_other"})
    assert Path(res["file"]) == tmp_path / "only_other"
    assert sorted(os.listdir(res["file"])) == ["calc.sql"]
    with pytest.raises(BackupError, match="already exists"):
        run_backup(server(), fn_db, {"mode": "functions", "out_dir": str(tmp_path), "filename": "only_other"})


def test_no_functions_is_an_error_and_leaves_nothing(pg_admin, restore_db, tmp_path):
    empty = restore_db("nofn")
    pg_admin.execute(f'CREATE DATABASE "{empty}" TEMPLATE template0')
    with pytest.raises(BackupError, match="no functions"):
        run_backup(server(), empty, {"mode": "functions", "out_dir": str(tmp_path)})
    assert not list(tmp_path.iterdir())
