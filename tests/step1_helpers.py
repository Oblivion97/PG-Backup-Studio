"""QA helpers for the Step 1 engine tests (pg_dump comparison, state snapshots, scratch DBs).

Scratch databases are named pgbs_test_qa_*; roles pgbs_test_qa_*. Passwords come from PGPASSWORD only.
"""
import os
import re
import subprocess

from conftest import connect, drop_database, pg_settings, tool

QA_PREFIX = "pgbs_test_qa_"


def server_dict() -> dict:
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


def create_db(admin, suffix: str) -> str:
    name = QA_PREFIX + suffix
    drop_database(admin, name)
    admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
    return name


def drop_role(admin, role: str) -> None:
    assert role.startswith(QA_PREFIX) or role.startswith("pgbs_test_")
    admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def psql_script(dbname: str, sql_text: str):
    r = subprocess.run(
        [tool("psql"), "-X", "-q", "-h", pg_settings()["host"], "-p", str(pg_settings()["port"]),
         "-U", pg_settings()["user"], "-d", dbname, "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql_text, capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
    assert r.returncode == 0, r.stderr
    return r


def schema_lines(db: str, *extra: str) -> list[str]:
    """pg_dump -s, comments / \\restrict / blank lines removed, sorted."""
    s = pg_settings()
    r = subprocess.run([tool("pg_dump"), "-s", "-h", s["host"], "-p", str(s["port"]), "-U", s["user"], *extra, db],
                       capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())
    assert r.returncode == 0, r.stderr
    return sorted(ln for ln in r.stdout.splitlines()
                  if ln.strip() and not ln.startswith("--") and not ln.startswith("\\restrict ")
                  and not ln.startswith("\\unrestrict "))


def table_counts(db: str) -> dict:
    with connect(db) as c:
        rels = [r[0] for r in c.execute(
            "SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relkind = 'r' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'")]
        return {t: c.execute(f"SELECT count(*) FROM ONLY {t}").fetchone()[0] for t in rels}


def sequence_state(db: str) -> dict:
    with connect(db) as c:
        out = {}
        for (s,) in c.execute("SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n"
                              " ON n.oid = c.relnamespace WHERE c.relkind = 'S' AND n.nspname !~ '^pg_'").fetchall():
            out[s] = tuple(c.execute(f"SELECT last_value, is_called FROM {s}").fetchone())
        return out


def nextvals(db: str) -> dict:
    """nextval() of every sequence (consumes values: call last)."""
    with connect(db) as c:
        names = [r[0] for r in c.execute("SELECT c.oid::regclass::text FROM pg_class c JOIN pg_namespace n"
                                         " ON n.oid = c.relnamespace WHERE c.relkind = 'S'"
                                         " AND n.nspname !~ '^pg_'").fetchall()]
        return {n: c.execute("SELECT nextval(%s)", (n,)).fetchone()[0] for n in names}


MD5_TABLES = (('"sales data".orders', "id, ordered_at"), ("app.edge", "id"), ("app.customers", "id"))


def md5s(db: str) -> dict:
    with connect(db) as c:
        return {t: c.execute(f"SELECT md5(string_agg(x::text, '|' ORDER BY {key})) FROM {t} x").fetchone()[0]
                for t, key in MD5_TABLES}


def trigger_states(db: str) -> str:
    with connect(db) as c:
        return c.execute("SELECT string_agg(c.oid::regclass::text || '.' || tgname || '=' || tgenabled::text, ','"
                         " ORDER BY c.oid::regclass::text, tgname) FROM pg_trigger g JOIN pg_class c"
                         " ON c.oid = g.tgrelid WHERE NOT tgisinternal").fetchone()[0]


def fixture_state(db: str) -> dict:
    """Everything the round-trip criteria compare (except nextval, which mutates)."""
    with connect(db) as c:
        matview = c.execute('SELECT count(*) FROM "sales data".monthly_totals').fetchone()[0]
        matview_md5 = c.execute('SELECT md5(string_agg(m::text, \'|\' ORDER BY month))'
                                ' FROM "sales data".monthly_totals m').fetchone()[0]
        populated = c.execute("SELECT relispopulated FROM pg_class WHERE oid = '\"sales data\".monthly_totals'::regclass"
                              ).fetchone()[0]
    return {"counts": table_counts(db), "md5": md5s(db), "seq": sequence_state(db), "triggers": trigger_states(db),
            "matview": (matview, matview_md5, populated)}


def strip_header_free_sql(text: str) -> str:
    """Everything after the file header settings."""
    return text.split("SET row_security = off;", 1)[1]


def read_backup_text(path: str) -> str:
    import gzip
    opener = gzip.open if open(path, "rb").read(2) == b"\x1f\x8b" else open
    with opener(path, "rb") as f:
        return f.read().decode("utf-8")


def make_name(suffix: str) -> str:
    assert re.fullmatch(r"[A-Za-z0-9_]+", suffix)
    return QA_PREFIX + suffix
