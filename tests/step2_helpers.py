"""QA helpers for the Step 2 tests (scratch DBs / roles, lock holders, threads, counters).

Scratch databases and roles are named pgbs_test_qa_*; everything is dropped by the `env` fixture.
Passwords come from PGPASSWORD only (never written to a file).
"""
import os
import secrets
import threading
import time

import psycopg

from conftest import connect, drop_database, pg_settings
from step1_helpers import QA_PREFIX


def server_dict() -> dict:
    s = pg_settings()
    return {"host": s["host"], "port": s["port"], "user": s["user"], "password": s["password"]}


def files(path) -> list:
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


def connect_as(role: str, password: str, dbname: str, autocommit: bool = True):
    s = pg_settings()
    return psycopg.connect(host=s["host"], port=s["port"], user=role, password=password, dbname=dbname,
                           autocommit=autocommit, connect_timeout=5)


def hold(db: str, table: str, mode: str = "ACCESS EXCLUSIVE"):
    """Open a session that holds `LOCK TABLE table IN mode MODE` (caller rolls back + closes)."""
    h = connect(db, autocommit=False)
    h.execute(f"LOCK TABLE {table} IN {mode} MODE")
    return h


def release(h) -> None:
    try:
        h.rollback()
    finally:
        h.close()


def in_thread(fn):
    """Run fn() in a thread; returns (thread, out) where out gets result / error / elapsed."""
    out = {}

    def go():
        t0 = time.monotonic()
        try:
            out["result"] = fn()
        except BaseException as e:  # noqa: BLE001
            out["error"] = e
        out["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=go, daemon=True)
    th.start()
    return th, out


def app_backends(db: str) -> int:
    """Sessions of PG Backup Studio still connected to db."""
    with connect("postgres") as c:
        return c.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s"
                         " AND application_name = 'PG Backup Studio'", (db,)).fetchone()[0]


def counters(db: str) -> dict:
    """Write counters of pg_stat_database and pg_stat_user_tables for db (fresh stats snapshot)."""
    with connect(db) as c:
        c.execute("SELECT pg_stat_clear_snapshot()")
        d = c.execute("SELECT tup_inserted, tup_updated, tup_deleted, temp_files, temp_bytes, xact_commit"
                      " FROM pg_stat_database WHERE datname = current_database()").fetchone()
        t = c.execute("SELECT coalesce(sum(n_tup_ins),0), coalesce(sum(n_tup_upd),0), coalesce(sum(n_tup_del),0)"
                      " FROM pg_stat_user_tables").fetchone()
    return {"tup_inserted": d[0], "tup_updated": d[1], "tup_deleted": d[2], "temp_files": d[3], "temp_bytes": d[4],
            "xact_commit": d[5], "tables": tuple(int(x) for x in t)}


class Env:
    """Creates scratch databases and roles and removes them again."""

    def __init__(self, admin):
        self.admin = admin
        self.dbs: list[str] = []
        self.roles: list[str] = []

    def db(self, suffix: str, script: str | None = None) -> str:
        name = f"{QA_PREFIX}s2_{suffix}"
        drop_database(self.admin, name)
        self.admin.execute(f"CREATE DATABASE \"{name}\" TEMPLATE template0 ENCODING 'UTF8'")
        self.dbs.append(name)
        if script:
            with connect(name) as c:
                c.execute(script)
        return name

    def role(self, suffix: str, extra: str = "") -> tuple[str, str]:
        name = f"{QA_PREFIX}s2_{suffix}"
        pw = secrets.token_hex(8)
        self.admin.execute(f'DROP ROLE IF EXISTS "{name}"')
        self.admin.execute(f"CREATE ROLE \"{name}\" LOGIN PASSWORD '{pw}' {extra}")
        self.roles.append(name)
        return name, pw

    def close(self) -> None:
        for n in self.dbs:
            drop_database(self.admin, n)
        for r in self.roles:
            self.admin.execute(f'DROP ROLE IF EXISTS "{r}"')
