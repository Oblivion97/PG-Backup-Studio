"""QA: Step 1 round trip, schema-only, data-only, privileges off and schema filter against real PostgreSQL.

Scratch DBs: pgbs_test_qa_*. Backup files live under pytest's tmp dirs.
"""
import re

import pytest

from conftest import connect, drop_database
from engine.backup import HEADER_SETTINGS, run_backup
from engine.restore import run_restore
from step1_helpers import (create_db, fixture_state, nextvals, read_backup_text, schema_lines, sequence_state,
                           server_dict, strip_header_free_sql, table_counts, trigger_states)

LABEL_RE = re.compile(r"^-- Type: (?P<kind>[^;\n]+); Name: (?P<name>.*)$", re.M)


@pytest.fixture(scope="module")
def server():
    return server_dict()


@pytest.fixture(scope="module")
def targets(pg_admin):
    made = []

    def make(suffix):
        name = create_db(pg_admin, suffix)
        made.append(name)
        return name

    yield make
    for n in made:
        drop_database(pg_admin, n)


@pytest.fixture(scope="module")
def backups(server, src_db, tmp_path_factory):
    d = tmp_path_factory.mktemp("rt_backups")
    return {
        "plain": run_backup(server, src_db, {"out_dir": str(d), "filename": "plain.sql"}),
        "gzip": run_backup(server, src_db, {"out_dir": str(d), "gzip": True, "filename": "gz.sql.gz"}),
        "schema": run_backup(server, src_db, {"out_dir": str(d), "mode": "schema", "filename": "schema.sql"}),
        "data": run_backup(server, src_db, {"out_dir": str(d), "mode": "data", "filename": "data.sql"}),
    }


# ---------------------------------------------------------------- 1. full round trip

@pytest.fixture(scope="module", params=["plain", "gzip"])
def full_restore(request, server, src_db, backups, targets):
    db = targets("rt_" + request.param)
    res = run_restore(server, db, backups[request.param]["file"])
    return request.param, db, res


def test_full_restore_clean(full_restore):
    _, _, res = full_restore
    assert res["status"] == "ok"
    assert res["error_count"] == 0, res["errors"]
    assert res["warning_count"] == 0, res["warnings"]


def test_full_pg_dump_schema_identical(full_restore, src_db):
    _, db, _ = full_restore
    a, b = schema_lines(src_db), schema_lines(db)
    assert a == b, "only in source: %s ; only in restore: %s" % (sorted(set(a) - set(b))[:10], sorted(set(b) - set(a))[:10])


def test_full_counts_md5_sequences_triggers_matview(full_restore, src_db):
    _, db, _ = full_restore
    a, b = fixture_state(src_db), fixture_state(db)
    assert a["counts"] == b["counts"]
    assert sum(a["counts"].values()) > 5000
    assert a["md5"] == b["md5"]
    assert a["seq"] == b["seq"]
    assert a["triggers"] == b["triggers"] and "customers_audit_disabled=D" in b["triggers"]
    assert a["matview"] == b["matview"] and b["matview"][0] > 0 and b["matview"][2] is True


def test_full_nextval_equal(full_restore, src_db):
    # nextval consumes values: only compare from a pristine restore per format, against a fresh source read
    _, db, _ = full_restore
    with connect(src_db) as c:
        expected = {}
        for name, (last, called) in sequence_state(src_db).items():
            inc, mx, mn, cyc = c.execute("SELECT increment_by, max_value, min_value, cycle FROM pg_sequences"
                                         " WHERE format('%%I.%%I', schemaname, sequencename) = %s OR"
                                         " schemaname || '.' || sequencename = %s", (name, name)).fetchone()
            expected[name] = last + inc if called else last
    got = nextvals(db)
    assert got == expected


def test_full_functions_views_policies_work(full_restore):
    _, db, _ = full_restore
    with connect(db) as c:
        assert c.execute("SELECT app.add_sql(2, 3), app.add_plpgsql(2, 3), app.atomic_double(4)").fetchone() == (5, 5, 8)
        assert c.execute("SELECT app.sum_squares(id) FROM generate_series(1,3) id").fetchone()[0] == 14
        assert c.execute("SELECT app.customer_label(c) FROM app.customers c WHERE id = 1").fetchone()[0].startswith("1: ")
        assert c.execute("SELECT count(*) FROM app.view_b").fetchone()[0] > 0
        assert c.execute("SELECT count(*) FROM pg_policies WHERE policyname = 'secrets_owner'").fetchone()[0] == 1
        # rule + trigger behave: insert a note -> rule logs; insert customer -> trigger logs (enabled one)
        before = c.execute("SELECT count(*) FROM audit.log").fetchone()[0]
        c.execute("INSERT INTO app.notes (body) VALUES ('x')")
        assert c.execute("SELECT count(*) FROM audit.log").fetchone()[0] == before + 1
        c.execute("INSERT INTO app.customers (email, name) VALUES ('zz@x.org', 'zz')")
        assert c.execute("SELECT count(*) FROM audit.log").fetchone()[0] == before + 2
        c.execute("UPDATE app.customers SET name = 'zzz' WHERE email = 'zz@x.org'")  # disabled trigger: no new row
        assert c.execute("SELECT count(*) FROM audit.log").fetchone()[0] == before + 2
        # citext extension works; identity ALWAYS refuses explicit values
        assert c.execute("SELECT count(*) FROM app.customers WHERE email = 'user1@example.com'").fetchone()[0] == 1
        with pytest.raises(Exception):
            c.execute("INSERT INTO app.ident_always (id, v) VALUES (999, 'x')")


def test_file_format(backups, src_db):
    for kind, res in backups.items():
        text = read_backup_text(res["file"])
        assert "\r\n" not in text, kind
    plain = open(backups["plain"]["file"], "rb").read()
    assert b"\r\n" not in plain and b"\r" not in plain.replace(b"\\r", b"")
    assert open(backups["gzip"]["file"], "rb").read(2) == b"\x1f\x8b"


def test_gzip_and_plain_same_body(backups):
    def body(r):
        t = read_backup_text(r["file"])
        return re.sub(r"^-- (Created|Host):.*$", "", t, flags=re.M)
    assert body(backups["plain"]) == body(backups["gzip"])


def test_header_matches_spec(backups, src_db, server):
    text = read_backup_text(backups["plain"]["file"])
    head = text.split("\n\n", 1)[0]
    assert text.startswith("--\n-- PG Backup Studio backup\n")
    assert f"-- Database: {src_db}" in head
    assert re.search(r"^-- Server version: 18\.\d+ \(18\d{4}\)$", head, re.M)
    assert re.search(r"^-- Host: \S+:\d+$", head, re.M)
    assert re.search(r"^-- Created: \d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}$", head, re.M)
    assert "-- Mode: full" in head and "-- Schemas: " in head and '"sales data"' in head
    assert "-- Owners and grants: yes" in head
    assert HEADER_SETTINGS in text
    for needle in ("SET statement_timeout = 0;", "SET lock_timeout = 0;", "SET idle_in_transaction_session_timeout = 0;",
                   "SET client_encoding = 'UTF8';", "SET standard_conforming_strings = on;",
                   "SELECT pg_catalog.set_config('search_path', '', false);", "SET check_function_bodies = false;",
                   "SET xmloption = content;", "SET client_min_messages = warning;", "SET row_security = off;"):
        assert needle in text
    # settings come before the first object
    assert text.index("SET row_security = off;") < text.index("-- Type: ")


def test_every_object_has_label(backups):
    for kind in ("plain", "schema", "data"):
        res = backups[kind]
        text = read_backup_text(res["file"])
        body = strip_header_free_sql(text)
        assert body.lstrip().startswith("-- Type: "), "first thing after header must be a label"
        parts = re.split(r"^-- Type: .*$", body, flags=re.M)[1:]
        labels = LABEL_RE.findall(body)
        assert len(parts) == len(labels) == res["objects"] + res["tables"], (kind, len(labels), res["objects"], res["tables"])
        assert all(p.strip() for p in parts), "label without a statement"
    kinds = {k for k, _ in LABEL_RE.findall(read_backup_text(backups["plain"]["file"]))}
    for k in ("SCHEMA", "EXTENSION", "TYPE", "DOMAIN", "SEQUENCE", "FUNCTION", "PROCEDURE", "TABLE", "TABLE DATA",
              "SEQUENCE SET", "CONSTRAINT", "FK CONSTRAINT", "INDEX", "VIEW", "MATERIALIZED VIEW", "AGGREGATE",
              "TRIGGER", "RULE", "POLICY", "ROW SECURITY"):
        assert k in kinds, k


def test_object_order_follows_spec(backups):
    text = read_backup_text(backups["plain"]["file"])
    kinds = [k for k, _ in LABEL_RE.findall(text)]

    def first(k): return kinds.index(k)

    def last(k): return len(kinds) - 1 - kinds[::-1].index(k)
    assert first("SCHEMA") < first("EXTENSION") < first("TYPE") < first("SEQUENCE") < first("TABLE")
    assert last("TABLE") < first("TABLE DATA") <= last("TABLE DATA") < first("SEQUENCE SET")
    assert last("SEQUENCE SET") < first("CONSTRAINT") <= last("CONSTRAINT")
    assert last("CONSTRAINT") < first("INDEX")
    assert last("FK CONSTRAINT") < first("VIEW") and first("FK CONSTRAINT") > last("SEQUENCE SET")
    assert last("VIEW") < first("TRIGGER") and last("TRIGGER") < first("POLICY")


def test_result_dict_shape(backups):
    for kind, res in backups.items():
        for k in ("file", "size", "mode", "schemas", "objects", "tables", "rows", "duration_s", "server_version",
                  "warnings", "partial"):
            assert k in res, (kind, k)
        assert res["partial"] is False and res["warnings"] == []
    assert backups["plain"]["rows"] == sum(table_counts_of(backups["plain"]))
    assert backups["data"]["mode"] == "data" and backups["schema"]["tables"] == 0


def table_counts_of(res):
    """Data lines inside COPY blocks of the file (independent of the engine's splitter)."""
    total, inside = 0, False
    for line in read_backup_text(res["file"]).split("\n")[:-1]:
        if inside:
            if line == "\\.":
                inside = False
            else:
                total += 1
        elif line.startswith("COPY ") and line.endswith(" FROM stdin;"):
            inside = True
    return [total]


# ---------------------------------------------------------------- 2. schema only

def test_schema_only(server, src_db, backups, targets):
    db = targets("schema_only")
    res = run_restore(server, db, backups["schema"]["file"])
    assert res["status"] == "ok" and res["error_count"] == 0 and res["warning_count"] == 0, res["errors"]
    assert schema_lines(db) == schema_lines(src_db)
    counts = table_counts(db)
    assert counts and set(counts.values()) == {0}
    assert "FROM stdin" not in read_backup_text(backups["schema"]["file"])
    with connect(db) as c:   # matview created WITH NO DATA in schema-only
        assert c.execute("SELECT relispopulated FROM pg_class WHERE oid = '\"sales data\".monthly_totals'::regclass"
                         ).fetchone()[0] is False


# ---------------------------------------------------------------- 3. data only after schema only

def test_data_only_after_schema_only(server, src_db, backups, targets):
    db = targets("data_only")
    assert run_restore(server, db, backups["schema"]["file"])["status"] == "ok"
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM audit.log").fetchone()[0] == 0
    res = run_restore(server, db, backups["data"]["file"])
    assert res["status"] == "ok" and res["error_count"] == 0 and res["warning_count"] == 0, res["errors"]
    a, b = fixture_state(src_db), fixture_state(db)
    assert a["counts"] == b["counts"], {k: (a["counts"][k], b["counts"].get(k)) for k in a["counts"]
                                          if a["counts"][k] != b["counts"].get(k)}
    assert a["md5"] == b["md5"]
    assert a["seq"] == b["seq"]
    assert a["triggers"] == b["triggers"]
    assert b["matview"] == a["matview"], "data-only must refresh the populated matview"
    with connect(db) as c:   # trigger did not fire during load
        assert c.execute("SELECT count(*) FROM audit.log").fetchone()[0] == a["counts"]["audit.log"]
    assert trigger_states(db).count("customers_audit_disabled=D") == 1
    assert schema_lines(db) == schema_lines(src_db)


# ---------------------------------------------------------------- 4/5. privileges off, schema filter

def test_privileges_off(server, src_db, tmp_path, targets):
    res = run_backup(server, src_db, {"out_dir": str(tmp_path), "privileges": False})
    text = read_backup_text(res["file"])
    assert not re.search(r"^\s*(GRANT|REVOKE)\b", text, re.M | re.I)
    assert "OWNER TO" not in text and "-- Owners and grants: no" in text
    db = targets("priv_off")
    r = run_restore(server, db, res["file"])
    assert r["status"] == "ok" and r["error_count"] == 0 and r["warning_count"] == 0, r["errors"]
    with connect(db) as c:   # no explicit ACLs anywhere on user relations
        assert c.execute("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                         " WHERE n.nspname IN ('app','sales data') AND c.relacl IS NOT NULL").fetchone()[0] == 0


def test_privileges_on_has_grants(backups):
    text = read_backup_text(backups["plain"]["file"])
    assert re.search(r'^GRANT SELECT ON TABLE "app"\."customers" TO "pgbs_test_reader";$', text, re.M) or \
        'TO "pgbs_test_reader"' in text
    assert "REVOKE ALL ON TABLE" in text and "OWNER TO" in text


def test_schema_filter_app(server, src_db, tmp_path, targets):
    res = run_backup(server, src_db, {"out_dir": str(tmp_path), "schemas": ["app"]})
    text = read_backup_text(res["file"])
    body = strip_header_free_sql(text)
    assert "sales data" not in body
    assert 'CREATE EXTENSION IF NOT EXISTS "citext"' in body and 'CREATE EXTENSION IF NOT EXISTS "btree_gist"' in body
    assert res["schemas"] == ["app"]
    db = targets("filter_app")
    r = run_restore(server, db, res["file"])
    # app.customers' trigger and app.notes' rule use audit.log_customer / audit.log which are NOT in the
    # selection: expected, documented -> backup must WARN and restore reports them as errors (not silently).
    assert any("audit" in w for w in res["warnings"]), res["warnings"]
    msgs = " | ".join(e["message"] for e in r["errors"])
    allowed = all(("audit" in e["message"] or "audit" in e["sql"]) for e in r["errors"])
    assert allowed, r["errors"]
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM app.customers").fetchone()[0] == 50, msgs


def test_schema_filter_app_audit_zero_errors(server, src_db, tmp_path, targets):
    res = run_backup(server, src_db, {"out_dir": str(tmp_path), "schemas": ["app", "audit"]})
    assert 'sales data' not in strip_header_free_sql(read_backup_text(res["file"]))
    db = targets("filter_app_audit")
    r = run_restore(server, db, res["file"])
    assert r["status"] == "ok" and r["error_count"] == 0 and r["warning_count"] == 0, r["errors"]


def test_schema_filter_sales_only_warns_about_outside_fk(server, src_db, tmp_path):
    res = run_backup(server, src_db, {"out_dir": str(tmp_path), "schemas": ["sales data"]})
    assert any("not in the backup" in w and "app" in w for w in res["warnings"]), res["warnings"]
    text = read_backup_text(res["file"])
    assert "Warnings:" in text.split("SET statement_timeout", 1)[0]


def test_unselected_schema_unknown_name(server, src_db, tmp_path):
    from engine.util import EngineError
    with pytest.raises(EngineError):
        run_backup(server, src_db, {"out_dir": str(tmp_path), "schemas": ["no_such_schema"]})
    assert list(tmp_path.iterdir()) == []


def test_not_included_warnings(server, pg_admin, tmp_path, targets):
    db = create_db(pg_admin, "notinc")
    try:
        from step1_helpers import psql_script
        psql_script(db, """
            CREATE COLLATION public.qa_coll (provider = icu, locale = 'und');
            CREATE OPERATOR public.=== (LEFTARG = int, RIGHTARG = int, FUNCTION = int4eq);
            CREATE TEXT SEARCH CONFIGURATION public.qa_ts (COPY = pg_catalog.english);
            CREATE TABLE public.t (a int);
            CREATE PUBLICATION qa_pub FOR TABLE public.t;
        """)
        res = run_backup(server_dict(), db, {"out_dir": str(tmp_path)})
        joined = " | ".join(res["warnings"])
        for what in ("collation", "operator", "text search configuration", "publication"):
            assert what in joined, joined
        assert all(w.startswith("NOT INCLUDED") for w in res["warnings"])
        head = read_backup_text(res["file"]).split("SET statement_timeout", 1)[0]
        assert "NOT INCLUDED" in head and "collation" in head
    finally:
        drop_database(pg_admin, db)
