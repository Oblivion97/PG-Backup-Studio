"""Functions-only backup: one `.sql` file per function or procedure, in a new folder.

Each file is named after the function (`calc_total.sql`). When several functions share a name
(overloads, or the same name in two schemas; compared case-insensitively because Windows file
names are), the first keeps the plain name and the others get `calc_total1.sql`,
`calc_total2.sql`, ... The order is fixed (name, schema, signature), so the same database always
gives the same names. The folder is written as `<folder>.part` and renamed on success; it is
removed on failure or cancel. Nothing is written to the source database.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime

import psycopg

from . import catalog
from .backup import (_UNSAFE_FILENAME, BackupOptions, Catalog, _BackupRun, _message, _PlanBuilder, _Reporter)
from .db import cancel_on_event, connect, require_supported
from .util import BackupError, Cancelled, EngineError, check_cancel

log = logging.getLogger(__name__)

MAX_NAME = 120
_RESERVED = re.compile(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?")


def safe_stem(name: str) -> str:
    """A function name made usable as a file name on Windows, macOS and Linux."""
    stem = _UNSAFE_FILENAME.sub("_", name).strip().rstrip(". ")[:MAX_NAME].rstrip(". ")
    if not stem:
        stem = "function"
    if _RESERVED.fullmatch(stem):
        stem += "_"
    return stem


def file_names(functions: list[dict]) -> dict[int, str]:
    """{function oid: file name}. Unique names first keep the plain name; repeats get 1, 2, ..."""
    ordered = sorted(functions, key=lambda f: (f["name"], f["schema"], f["sig"], f["oid"]))
    used: set[str] = set()
    names: dict[int, str] = {}
    repeats = []
    for f in ordered:
        stem = safe_stem(f["name"])
        if stem.lower() in used:
            repeats.append((f, stem))
        else:
            used.add(stem.lower())
            names[f["oid"]] = stem + ".sql"
    for f, stem in repeats:
        n = 1
        while f"{stem}{n}".lower() in used:
            n += 1
        used.add(f"{stem}{n}".lower())
        names[f["oid"]] = f"{stem}{n}.sql"
    return names


def _target_folder(opts: BackupOptions, dbname: str) -> str:
    """Choose the output folder and create its `.part` folder (the caller removes it on failure)."""
    out_dir = os.path.abspath(os.path.expanduser(opts.out_dir))
    os.makedirs(out_dir, exist_ok=True)

    def reserve(path: str) -> bool:
        if os.path.exists(path):
            return False
        try:
            os.mkdir(path + ".part")
            return True
        except FileExistsError:
            return False

    if opts.filename:
        path = os.path.join(out_dir, os.path.basename(opts.filename))
        if not reserve(path):
            raise BackupError(f"The folder {path} already exists (or another backup is writing it); "
                              f"choose another name.")
        return path
    base = f"{_UNSAFE_FILENAME.sub('_', dbname)}_functions_{datetime.now():%Y%m%d_%H%M%S}"
    path, n = os.path.join(out_dir, base), 2
    while not reserve(path):
        path, n = os.path.join(out_dir, f"{base}_{n}"), n + 1
    return path


def _file_header(dbname: str, conn: psycopg.Connection, f: dict, kind: str) -> str:
    info = conn.info
    return "\n".join([
        "--", f"-- PG Backup Studio {kind.lower()} export", f"-- Database: {dbname}",
        f"-- Server version: {info.parameter_status('server_version')} ({info.server_version})",
        f"-- {kind.capitalize()}: {' '.join(f['sig'].split())}",
        f"-- Created: {datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}", "--", "",
        "SET client_encoding = 'UTF8';", "SET standard_conforming_strings = on;",
        "SET check_function_bodies = false;", "", ""])


def run_function_export(server: dict, dbname: str, options: BackupOptions | dict | None = None,
                        progress: Callable[[str, dict], None] | None = None,
                        cancel: threading.Event | None = None) -> dict:
    """Write every function and procedure of the selected schemas to its own file; returns the result dict.

    Same result keys as `run_backup` (`file` is the folder, `size` the total bytes), plus
    `functions`: [{name, file}] and `renamed`: how many files got a number.
    """
    opts = BackupOptions.from_value(options)
    rep = _Reporter(progress)
    started = time.monotonic()
    conn = connect(server, dbname)
    part = None
    try:
        with cancel_on_event(conn, cancel):
            version = require_supported(conn)
            rep.emit("phase", {"name": "catalog"})
            _BackupRun._start_snapshot(conn)
            selected = catalog.resolve_schemas(conn, opts.schemas)
            ctx = catalog.CatalogContext(version, [s["oid"] for s in selected])
            cat = Catalog(version_num=version, functions=catalog.get_functions(conn, ctx),
                          comments=catalog.get_comments(conn, ctx),
                          acls=catalog.get_acls(conn, ctx) if opts.privileges else [])
            check_cancel(cancel)
            if not cat.functions:
                raise BackupError("There are no functions or procedures in the selected schemas.")
            names = file_names(cat.functions)
            renamed = sum(1 for f in cat.functions if names[f["oid"]] != safe_stem(f["name"]) + ".sql")
            path = _target_folder(opts, dbname)
            part = path + ".part"
            rep.emit("file", {"path": path, "part": part})
            rep.emit("totals", {"objects": len(cat.functions), "tables": 0, "est_rows": 0, "data_bytes": 0})
            rep.emit("phase", {"name": "writing"})
            builder = _PlanBuilder(cat, opts)
            written, total = [], 0
            for f in sorted(cat.functions, key=lambda f: names[f["oid"]].lower()):
                check_cancel(cancel)
                kind = "PROCEDURE" if f["kind"] == "p" else "FUNCTION"
                data = (_file_header(dbname, conn, f, kind) + builder._function_sql(f, kind)).encode("utf-8")
                with open(os.path.join(part, names[f["oid"]]), "xb") as out:
                    out.write(data)
                total += len(data)
                written.append({"name": f["sig"], "file": names[f["oid"]]})
                rep.emit("object", {"kind": kind, "name": f["sig"], "objects_done": len(written),
                                    "bytes_written": total})
        check_cancel(cancel)
        conn.commit()
        os.replace(part, path)
        part = None
    except Cancelled:
        raise
    except Exception as e:
        if cancel is not None and cancel.is_set():
            raise Cancelled("Cancelled by user") from e
        if isinstance(e, EngineError):
            raise
        log.exception("function export failed")
        if isinstance(e, psycopg.Error):
            raise BackupError(f"Backup of the functions of {dbname} failed: {_message(e)}") from e
        if isinstance(e, OSError):
            raise BackupError(f"Could not write the function files: {e.strerror or e}") from e
        raise
    finally:
        conn.close()
        if part and os.path.isdir(part):
            shutil.rmtree(part, ignore_errors=True)
    rep.emit("phase", {"name": "done"})
    return {"file": path, "size": total, "mode": "functions", "schemas": [s["name"] for s in selected],
            "objects": len(written), "tables": 0, "rows": 0, "duration_s": round(time.monotonic() - started, 3),
            "server_version": version, "warnings": [], "partial": False, "partial_tables": [],
            "not_valid_fks": [], "skipped_fks": [], "functions": written, "renamed": renamed,
            "lock_timeout_s": opts.lock_timeout_s, "speed_limit_mb_s": opts.speed_limit_mb_s}
