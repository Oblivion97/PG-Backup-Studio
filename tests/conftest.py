"""Shared fixtures. Connection settings come from PG* environment variables only."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURE_SQL = Path(__file__).resolve().parent / "fixtures" / "sample_db.sql"
SRC_DB = "pgbs_test_src"
TEST_ROLE = "pgbs_test_reader"
PREFIX = "pgbs_test_"


def pg_settings() -> dict:
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "bin": os.environ.get("PG_BIN", r"C:\Program Files\PostgreSQL\18\bin"),
    }


def connect(dbname: str = "postgres", autocommit: bool = True):
    import psycopg
    s = pg_settings()
    return psycopg.connect(host=s["host"], port=s["port"], user=s["user"],
                           password=s["password"] or None, dbname=dbname,
                           autocommit=autocommit, connect_timeout=5)


def tool(name: str) -> str:
    exe = Path(pg_settings()["bin"]) / (name + (".exe" if os.name == "nt" else ""))
    return str(exe)


def run_psql(dbname: str, *args: str) -> subprocess.CompletedProcess:
    s = pg_settings()
    return subprocess.run(
        [tool("psql"), "-X", "-q", "-h", s["host"], "-p", str(s["port"]), "-U", s["user"],
         "-d", dbname, "-v", "ON_ERROR_STOP=1", *args],
        capture_output=True, text=True, encoding="utf-8", env=os.environ.copy())


def drop_database(admin, name: str) -> None:
    """Drop a database; refuses anything not named pgbs_test_*."""
    if not re.fullmatch(r"pgbs_test_[A-Za-z0-9_]*", name):
        raise RuntimeError(f"refusing to drop non-test database {name!r}")
    admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def pg_admin():
    try:
        conn = connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable ({type(e).__name__}); set PGHOST/PGPORT/PGUSER/PGPASSWORD")
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def src_db(pg_admin):
    drop_database(pg_admin, SRC_DB)
    pg_admin.execute(f"CREATE DATABASE \"{SRC_DB}\" TEMPLATE template0 ENCODING 'UTF8'")
    try:
        r = run_psql(SRC_DB, "-f", str(FIXTURE_SQL))
        assert r.returncode == 0 and not r.stderr.strip(), f"fixture load failed: {r.stderr}"
        yield SRC_DB
    finally:
        drop_database(pg_admin, SRC_DB)
        try:
            pg_admin.execute(f"DROP ROLE IF EXISTS {TEST_ROLE}")
        except Exception:  # noqa: BLE001 - role may still be referenced elsewhere; not fatal
            pass


@pytest.fixture
def restore_db(pg_admin):
    """Factory: restore_db('x') -> name 'pgbs_test_restore_x' (dropped now and at teardown)."""
    made = []

    def make(suffix: str = "a") -> str:
        assert re.fullmatch(r"[A-Za-z0-9_]+", suffix)
        name = f"{PREFIX}restore_{suffix}"
        made.append(name)
        drop_database(pg_admin, name)
        return name

    yield make
    for n in made:
        drop_database(pg_admin, n)
