"""Background threads, progress state, cancel, live log (Step 3).

A `JobManager` runs each backup or restore on a daemon thread. The engine reports raw counters
through its progress callback (ARCHITECTURE 8.8); the job turns them into a snapshot with percent,
ETA and a 200-line log that any number of pollers can read (10.5, 10.6). History is written when a
job starts (`running`) and again with the outcome; a backup that succeeds also saves its options as
the database's profile and its folder as the default output folder. Jobs live in memory only:
closing the app cancels them, and a job left `running` by a killed process is marked `interrupted`
at the next start (decision 005).
"""

from __future__ import annotations

import atexit
import copy
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

from engine import backup, db, restore
from engine.backup import BackupOptions
from engine.restore import RestoreOptions
from engine.util import BackupBlockedError, Cancelled, EngineError, check_cancel
from store import Store, StoreError, new_id

log = logging.getLogger(__name__)

LOG_LINES = 200
LOG_LINE_CHARS = 500
HISTORY_ERRORS = 20
SHUTDOWN_TIMEOUT_S = 15.0
ETA_MIN_PROGRESS = 0.02
ETA_MIN_SECONDS = 2.0
WEIGHT_OBJECTS, WEIGHT_DATA = 0.2, 0.8
CANCELLED_BY_USER = "Cancelled by user"
CANCELLED_BY_CLOSE = "Cancelled: the app was closed"
TARGET_MODES = ("existing", "create", "recreate")
_PHASE_TEXT = {"preparing": "Preparing the target database", "connecting": "Connecting to the server", "catalog": "Reading the catalog",
               "write": "Writing the backup file", "restore": "Running the backup file",
               "saving": "Saving the result"}


class JobError(Exception):
    """A job could not be started or found; `code`: not_found | invalid | conflict | busy."""

    def __init__(self, message: str, code: str = "invalid"):
        self.code = code
        super().__init__(message)


def _one_line(text, limit: int = LOG_LINE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class Job:
    """State of one job. Every field is changed and read under `lock`; `_server` holds the password."""

    def __init__(self, job_id: str, kind: str, server: dict, dbname: str, options, path: str | None = None,
                 target: dict | None = None):
        self.id, self.kind, self.database, self.options, self.target = job_id, kind, dbname, options, target
        self._server = server
        self.server_ref = {"id": server["id"], "name": server.get("name")}
        self.source = path
        self.status, self.phase = "running", "starting"
        self.cancel_event = threading.Event()
        self.cancel_requested = self.closing = False
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.started_wall = datetime.now().astimezone()
        self.t0, self.t_work, self.t_end = time.monotonic(), None, None
        self.log: deque[dict] = deque(maxlen=LOG_LINES)
        self.log_seq = 0
        self.error_count = self.warning_count = 0
        self.result = self.error = None
        self.file, self.part_file, self.history_error = None, None, None
        self._part_recorded = False
        self._max_pct = 0.0
        # backup counters
        self.totals_seen = False
        self.objects_total = self.objects_done = self.tables_total = self.tables_done = 0
        self.est_rows = self.rows_done = self.data_total = self.data_done = self.bytes_written = 0
        self.cur_name, self.cur_size, self.cur_est, self.cur_rows, self.cur_bytes = None, 0, 0, 0, 0
        # restore counters
        self.file_size = self.position = 0
        self.cur_object: dict | None = None

    def add_log(self, level: str, msg, error: bool = False) -> None:
        self.log_seq += 1
        self.log.append({"seq": self.log_seq, "t": round(time.monotonic() - self.t0, 1), "level": level,
                         "msg": _one_line(msg), "error": error})

    def set_phase(self, name: str) -> None:
        if name == self.phase:
            return
        self.phase = name
        if name in ("write", "restore") and self.t_work is None:
            self.t_work = time.monotonic()
        if name in _PHASE_TEXT:
            self.add_log("info", _PHASE_TEXT[name])

    # -------------------------------------------------- progress formulas (ARCHITECTURE 10.6)

    def fraction(self) -> float:
        if self.kind == "restore":
            return self.position / self.file_size if self.file_size > 0 else 0.0
        if not self.totals_seen:
            return 0.0
        objects, data = self.objects_total, self.data_total
        if objects == 0 and data == 0:
            return 0.0
        wo, wd = (1.0, 0.0) if data == 0 else (0.0, 1.0) if objects == 0 else (WEIGHT_OBJECTS, WEIGHT_DATA)
        current = min(self.cur_bytes / self.cur_size, 0.99) * self.cur_size if self.cur_size > 0 else 0
        p = 0.0
        if objects:
            p += wo * min(self.objects_done / objects, 1.0)
        if data:
            p += wd * min((self.data_done + current) / data, 1.0)
        return p

    def percent_and_eta(self) -> tuple[float, int | None]:
        if self.status == "success":
            return 100.0, None
        p = min(max(self.fraction(), self._max_pct), 0.999)
        self._max_pct = p
        eta = None
        if self.status == "running" and self.t_work is not None:
            t = time.monotonic() - self.t_work
            if p >= ETA_MIN_PROGRESS and t >= ETA_MIN_SECONDS:
                eta = round(t * (1 - p) / p)
        return round(p * 100, 1), eta

    # -------------------------------------------------- snapshot (ARCHITECTURE 10.5)

    def snapshot(self, log_after: int | None = None, with_log: bool = True) -> dict:
        with self.lock:
            percent, eta = self.percent_and_eta()
            end = self.t_end if self.t_end is not None else time.monotonic()
            if self.kind == "backup":
                current = (None if self.cur_name is None else
                           {"name": self.cur_name, "rows": self.cur_rows, "est_rows": self.cur_est})
                progress = {"objects_done": self.objects_done, "objects_total": self.objects_total,
                            "tables_done": self.tables_done, "tables_total": self.tables_total,
                            "rows": self.rows_done + self.cur_rows, "est_rows": self.est_rows,
                            "data_bytes_done": self.data_done + min(self.cur_bytes, self.cur_size),
                            "data_bytes_total": self.data_total, "bytes_written": self.bytes_written,
                            "current": current}
            else:
                progress = {"file_size": self.file_size, "position": self.position,
                            "objects_done": self.objects_done, "rows": self.rows_done,
                            "current": dict(self.cur_object) if self.cur_object else None}
            out = {"id": self.id, "kind": self.kind, "status": self.status, "phase": self.phase,
                   "cancel_requested": self.cancel_requested, "server": dict(self.server_ref),
                   "database": self.database, "started": self.started_wall.isoformat(timespec="seconds"),
                   "elapsed_s": round(end - self.t0, 1), "percent": percent, "eta_s": eta,
                   "file": self.file if self.kind == "backup" else self.source, "progress": progress,
                   "error_count": self.error_count, "warning_count": self.warning_count,
                   "log_seq": self.log_seq, "result": copy.deepcopy(self.result), "error": self.error,
                   "history_id": self.id, "history_error": self.history_error,
                   "mode": self.options.mode if self.kind == "backup" else None}
            if with_log:
                out["log"] = [dict(x) for x in self.log if log_after is None or x["seq"] > log_after]
            return out


class JobManager:
    """Starts, tracks and cancels jobs; see the module docstring."""

    def __init__(self, store: Store, *, backup_fn=None, restore_fn=None, prepare_fn=None, keep_finished: int = 50,
                 max_running: int = 4):
        self.store = store
        self._backup_fn = backup_fn
        self._restore_fn = restore_fn
        self._prepare_fn = prepare_fn
        self.keep_finished = keep_finished
        self.max_running = max_running
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._closed = False
        self.recover_interrupted()

    def recover_interrupted(self) -> int:
        """Mark history entries left `running` by an ended process as `interrupted`; returns how many."""
        try:
            return len(self.store.mark_interrupted())
        except (StoreError, OSError) as e:
            log.warning("could not mark interrupted jobs: %s", e)
            return 0

    # -------------------------------------------------- start

    def _server(self, server_id: str, password: str | None) -> dict:
        server = self.store.get_server(server_id, with_password=True)
        if server is None:
            raise JobError("This server no longer exists.", "not_found")
        if password is not None:
            server["password"] = password   # typed for this job only; never stored
        return server

    @staticmethod
    def _check_db(dbname) -> None:
        if not isinstance(dbname, str) or not dbname:
            raise JobError("Choose a database.")

    def start_backup(self, server_id: str, dbname: str, options=None, *, password: str | None = None) -> str:
        server = self._server(server_id, password)
        self._check_db(dbname)
        if options is not None and not isinstance(options, (dict, BackupOptions)):
            raise JobError("Backup options must be an object.")
        try:
            opts = BackupOptions.from_value(options)
        except (EngineError, TypeError, ValueError) as e:
            raise JobError(str(e)) from e
        job = self._register("backup", server, dbname, opts)
        self._add_history(job, {"file": None, "part_file": None, "size": None, "mode": opts.mode,
                                "gzip": bool(opts.gzip), "partial": False})
        return self._launch(job)

    def start_restore(self, server_id: str, dbname: str, path: str, options=None, *, password: str | None = None,
                      source_history_id: str | None = None, target: dict | None = None) -> str:
        """Restore `path` into `dbname`. `target`: {mode: existing | create | recreate, owner, allow_non_empty,
        confirm}; recreate needs `confirm` equal to `dbname` (ARCHITECTURE 11.6)."""
        server = self._server(server_id, password)
        self._check_db(dbname)
        target = self._clean_target(server, dbname, target)
        if not isinstance(path, str) or not path.strip():
            raise JobError("Choose a backup file to restore.")
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isfile(path):
            raise JobError(f"Backup file not found: {path}")
        if options is not None and not isinstance(options, (dict, RestoreOptions)):
            raise JobError("Restore options must be an object.")
        try:
            opts = RestoreOptions.from_value(options)
        except (EngineError, TypeError, ValueError) as e:
            raise JobError(str(e)) from e
        job = self._register("restore", server, dbname, opts, path, target)
        self._add_history(job, {"file": path, "size": os.path.getsize(path), "source_history_id": source_history_id,
                                "target_mode": target["mode"],
                                "options": {"on_error": opts.on_error,
                                            "single_transaction": opts.single_transaction},
                                "partial": False})
        return self._launch(job)

    @staticmethod
    def _clean_target(server: dict, dbname: str, target) -> dict:
        if target is None:
            target = {}
        if not isinstance(target, dict):
            raise JobError("The restore target must be an object.")
        mode = target.get("mode") or "existing"
        if mode not in TARGET_MODES:
            raise JobError(f"Unknown restore target {mode!r}; use existing, create or recreate.")
        owner = target.get("owner") or None
        if owner is not None and not isinstance(owner, str):
            raise JobError("The owner must be a role name.")
        allow = target.get("allow_non_empty", False)
        if not isinstance(allow, bool):
            raise JobError("'allow_non_empty' must be true or false.")
        if mode != "existing":
            try:
                db.check_name(dbname)
            except db.DbActionError as e:
                raise JobError(str(e)) from e
        if mode == "recreate":
            if target.get("confirm") != dbname:
                raise JobError(f"Type the database name ({dbname}) exactly to confirm that it is dropped and "
                               f"recreated.")
            if dbname in (*db.TEMPLATES, server.get("maintenance_db") or "postgres"):
                raise JobError(f"Database {dbname} cannot be dropped: it is a template or the maintenance database.")
        return {"mode": mode, "owner": owner, "allow_non_empty": allow}

    def jobs_using(self, server_id: str, dbname: str) -> list[str]:
        """Ids of running jobs that use `dbname` on that server (drop / rename / kill must wait for them)."""
        with self._lock:
            return [j.id for j in self._jobs.values()
                    if j.status == "running" and j._server["id"] == server_id and j.database == dbname]

    def _register(self, kind: str, server: dict, dbname: str, opts, path: str | None = None,
                  target: dict | None = None) -> Job:
        with self._lock:
            if self._closed:
                raise JobError("The app is closing; no new jobs can start.", "busy")
            running = [j for j in self._jobs.values() if j.status == "running"]
            if len(running) >= self.max_running:
                raise JobError(f"{self.max_running} jobs are already running; wait for one to finish.", "busy")
            same_db = [j for j in running if j._server["id"] == server["id"] and j.database == dbname]
            if kind == "restore" and any(j.kind == "restore" for j in same_db):
                raise JobError(f"Another restore into {dbname} is running; wait for it to finish.", "conflict")
            if target and target["mode"] == "recreate" and same_db:
                raise JobError(f"{dbname} is in use by another job; wait for it to finish before recreating it.",
                               "conflict")
            job = Job(new_id(set(self._jobs)), kind, server, dbname, opts, path, target)
            self._jobs[job.id] = job
            self._prune()
            return job

    def _prune(self) -> None:
        finished = [j for j in self._jobs.values() if j.status != "running"]
        for j in finished[:max(0, len(finished) - self.keep_finished)]:
            del self._jobs[j.id]

    def _add_history(self, job: Job, fields: dict) -> None:
        s = job._server
        entry = {"id": job.id, "kind": job.kind, "status": "running",
                 "server": {"id": s["id"], "name": s.get("name"), "host": s.get("host"), "port": s.get("port")},
                 "database": job.database, "started": job.started_wall.isoformat(timespec="seconds"),
                 "duration_s": None, "objects": None, "rows": None, "error_count": 0, "warning_count": 0,
                 "error": None, "errors": [], **fields}
        if job.kind == "backup":
            entry.update(tables=None, partial_tables=[])
        try:
            self.store.add_history(entry)
        except (StoreError, OSError) as e:
            job.history_error = f"History could not be saved: {e}"
            log.warning("history entry for job %s not saved: %s", job.id, e)

    def _launch(self, job: Job) -> str:
        job.thread = threading.Thread(target=self._run, args=(job,), name=f"pgbs-job-{job.id}", daemon=True)
        job.thread.start()
        return job.id

    # -------------------------------------------------- worker

    def _run(self, job: Job) -> None:
        outcome: dict = {}
        try:
            if job.kind == "backup":
                with job.lock:
                    job.set_phase("connecting")
                fn = self._backup_fn or backup.run_backup
                res = fn(job._server, job.database, job.options, progress=self._tracker(job), cancel=job.cancel_event)
                outcome = {"status": "success", "result": res}
            else:
                (self._prepare_fn or self._prepare_target)(job)
                with job.lock:
                    job.set_phase("connecting")
                fn = self._restore_fn or restore.run_restore
                res = fn(job._server, job.database, job.source, job.options, progress=self._tracker(job),
                         cancel=job.cancel_event)
                status = {"ok": "success"}.get(res.get("status"), res.get("status") or "failed")
                first = (res.get("errors") or [{}])[0].get("message")
                outcome = {"status": status, "result": res, "error": first if status == "failed" else None}
                if status == "cancelled":
                    outcome["error"] = CANCELLED_BY_CLOSE if job.closing else CANCELLED_BY_USER
        except Cancelled:
            outcome = {"status": "cancelled", "error": CANCELLED_BY_CLOSE if job.closing else CANCELLED_BY_USER}
        except BackupBlockedError as e:
            outcome = {"status": "failed", "error": str(e), "result": {"problems": e.problems}}
        except EngineError as e:
            outcome = {"status": "failed", "error": str(e)}
        except Exception as e:  # noqa: BLE001 - a job must always end with a status
            log.exception("job %s failed", job.id)
            outcome = {"status": "failed", "error": f"Unexpected error: {type(e).__name__}: {e}"}
        self._finish(job, outcome)

    @staticmethod
    def _prepare_target(job: Job) -> None:
        """Check, create or drop-and-recreate the restore target (phase `preparing`, ARCHITECTURE 11.6)."""
        server, name, target = job._server, job.database, job.target
        with job.lock:
            job.set_phase("preparing")
        if target["mode"] == "existing":
            contents = db.database_contents(server, name)
            if not contents["empty"] and not target["allow_non_empty"]:
                raise db.DbActionError(
                    f"Database {name} is not empty ({contents['tables']} tables/views/sequences, "
                    f"{contents['functions']} functions, {contents['types']} types). Choose \"drop and recreate\", "
                    f"or allow restoring into a non-empty database (expect \"already exists\" errors).")
            if not contents["empty"]:
                with job.lock:
                    job.add_log("warning", f"Restoring into non-empty database {name}")
            return
        if target["mode"] == "recreate":
            check_cancel(job.cancel_event)
            try:
                dropped = db.drop_database(server, name, confirm=name)
                with job.lock:
                    job.add_log("warning", f"Dropped database {name} ({dropped['terminated']} sessions ended)")
            except db.DbActionError as e:
                if e.code != "not_found":
                    raise
        check_cancel(job.cancel_event)
        db.create_database(server, name, owner=target["owner"], template="template0", encoding="UTF8")
        with job.lock:
            job.add_log("info", f"Created database {name} (template0, UTF8"
                        + (f", owner {target['owner']})" if target["owner"] else ")"))

    def _finish(self, job: Job, outcome: dict) -> None:
        status, res = outcome["status"], outcome.get("result")
        with job.lock:
            job.set_phase("saving")
            job.t_end = time.monotonic()
            duration = round(job.t_end - job.t0, 3)
            if job.kind == "backup" and status == "failed":
                job.error_count = max(job.error_count, 1)
            if job.kind == "restore" and res:
                job.error_count, job.warning_count = res.get("error_count", 0), res.get("warning_count", 0)
                job.rows_done = res.get("rows", job.rows_done)
            if status == "success" and job.kind == "backup":
                job.file = res.get("file", job.file)
            job.part_file = None
            error_count, warning_count = job.error_count, job.warning_count
        changes = self._history_fields(job, status, outcome.get("error"), res, duration, error_count, warning_count)
        history_error = None
        try:
            self.store.update_history(job.id, changes)
            if job.kind == "backup" and status == "success":
                opts: BackupOptions = job.options
                if self.store.get_server(job._server["id"]) is not None:   # deleted meanwhile: no orphan profile
                    self.store.save_profile(job._server["id"], job.database, opts.to_profile())
                self.store.save_settings({"output_dir": opts.out_dir})
        except (StoreError, OSError) as e:
            history_error = f"History or backup profile could not be saved: {e}"
            log.warning("job %s: %s", job.id, history_error)
        with job.lock:
            if history_error:
                job.history_error = history_error
                job.add_log("warning", history_error)
            job.add_log("error" if status == "failed" else "info", self._summary(job, status, outcome.get("error"), res),
                        error=status == "failed")
            job.result, job.error = res, outcome.get("error")
            job.phase = "done"
            job.status = status   # last: a poll that sees the end status can rely on history being saved

    @staticmethod
    def _history_fields(job: Job, status: str, error, res: dict | None, duration: float,
                        error_count: int, warning_count: int) -> dict:
        out = {"status": status, "duration_s": duration, "error": error, "error_count": error_count,
               "warning_count": warning_count, "part_file": None}
        res = res or {}
        if job.kind == "backup":
            if status == "success":
                out.update(file=res.get("file"), size=res.get("size"), objects=res.get("objects"),
                           tables=res.get("tables"), rows=res.get("rows"), partial=bool(res.get("partial")),
                           partial_tables=res.get("partial_tables") or [],
                           warning_count=len(res.get("warnings") or []))
            elif error:
                out["errors"] = [{"object": None, "sqlstate": None, "message": error, "line": None}]
        else:
            out.update(objects=res.get("objects"), rows=res.get("rows"),
                       errors=[{k: e.get(k) for k in ("object", "sqlstate", "message", "line")}
                               for e in (res.get("errors") or [])[:HISTORY_ERRORS]])
            if status == "failed" and error is None and out["errors"]:
                out["error"] = out["errors"][0]["message"]
        return out

    @staticmethod
    def _summary(job: Job, status: str, error, res: dict | None) -> str:
        res = res or {}
        if status in ("cancelled", "failed"):
            return error or status.capitalize()
        if job.kind == "backup" and res.get("mode") == "functions":
            renamed = f" ({res['renamed']:,} numbered because the name repeats)" if res.get("renamed") else ""
            return f"Functions backed up: {res.get('objects', 0):,} files{renamed} → {res.get('file')}"
        if job.kind == "backup":
            partial = " (partial)" if res.get("partial") else ""
            return (f"Backup finished{partial}: {res.get('tables', 0):,} tables, {res.get('rows', 0):,} rows, "
                    f"{res.get('size', 0):,} bytes → {res.get('file')}")
        text = f"Restore finished: {res.get('objects', 0):,} objects, {res.get('rows', 0):,} rows"
        if res.get("error_count"):
            text += f", {res['error_count']:,} errors"
        if res.get("warning_count"):
            text += f", {res['warning_count']:,} warnings"
        return text

    # -------------------------------------------------- tracker (engine progress callback)

    def _tracker(self, job: Job):
        def progress(event: str, data: dict) -> None:
            try:
                record_part = None
                with job.lock:
                    record_part = self._apply(job, event, data or {})
                if record_part:
                    self.store.update_history(job.id, {"file": record_part[0], "part_file": record_part[1]})
            except Exception:  # noqa: BLE001 - progress must never stop the engine
                log.exception("progress tracking failed for job %s", job.id)
        return progress

    @staticmethod
    def _apply(job: Job, event: str, d: dict):
        """Update the job from one engine event (under `job.lock`); returns (path, part) to record once."""
        if event == "phase":
            if not job.cancel_requested:
                job.set_phase("saving" if d.get("name") == "done" else d.get("name") or job.phase)
        elif event == "totals":
            if job.kind == "backup":
                job.totals_seen = True
                job.objects_total, job.tables_total = d.get("objects", 0), d.get("tables", 0)
                job.est_rows, job.data_total = d.get("est_rows", 0), d.get("data_bytes", 0)
                job.add_log("info", f"{job.objects_total:,} functions to back up" if job.options.mode == "functions"
                            else f"{job.objects_total:,} objects and {job.tables_total:,} tables to back up")
            else:
                job.file_size = d.get("file_size", 0)
        elif event == "file":
            job.file, job.part_file = d.get("path"), d.get("part")
            if not job._part_recorded:
                job._part_recorded = True
                return job.file, job.part_file
        elif event == "object":
            job.objects_done = d.get("objects_done", job.objects_done)
            job.bytes_written = d.get("bytes_written", job.bytes_written)
            if job.kind == "restore":
                job.cur_object = {"kind": d.get("kind"), "name": d.get("name")}
        elif event == "table_start":
            job.cur_name, job.cur_size, job.cur_est = d.get("name"), d.get("size") or 0, d.get("est_rows") or 0
            job.cur_rows = job.cur_bytes = 0
        elif event == "table_progress":
            job.cur_rows, job.cur_bytes = d.get("rows", 0), d.get("bytes_copied", 0)
            job.bytes_written = d.get("bytes_written", job.bytes_written)
        elif event == "table_done":
            rows = d.get("rows", 0)
            job.tables_done += 1
            job.rows_done += rows
            job.data_done += job.cur_size
            job.bytes_written = d.get("bytes_written", job.bytes_written)
            job.add_log("info", f"Copied {d.get('name') or job.cur_name}: {rows:,} rows")
            job.cur_name, job.cur_size, job.cur_est, job.cur_rows, job.cur_bytes = None, 0, 0, 0, 0
        elif event == "position":
            job.position = max(job.position, d.get("pos", 0))
            job.rows_done = d.get("rows_loaded", job.rows_done)
            job.error_count = d.get("errors", job.error_count)
            job.warning_count = d.get("warnings", job.warning_count)
        elif event in ("warning", "error", "log"):
            level = "error" if event == "error" else d.get("level") or event
            msg = d.get("msg") or d.get("message") or ""
            if d.get("object"):
                msg = f"{d['object']}: {msg}"
            if event == "error":
                job.error_count += 1
            elif event == "warning":
                job.warning_count += 1
            job.add_log(level, msg, error=event == "error")
        return None

    # -------------------------------------------------- queries and control

    def get_job(self, job_id: str, log_after: int | None = None) -> dict | None:
        job = self._jobs.get(job_id)
        return job.snapshot(log_after) if job else None

    def list_jobs(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.snapshot(with_log=False) for j in reversed(jobs)]

    def cancel_job(self, job_id: str) -> bool:
        """Ask a running job to stop; False when unknown, finished or already cancelling."""
        job = self._jobs.get(job_id)
        if job is None:
            return False
        with job.lock:
            if job.status != "running" or job.cancel_requested:
                return False
            job.cancel_requested = True
            job.phase = "cancelling"
            job.add_log("warning", "Cancel requested")
        job.cancel_event.set()
        return True

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT_S) -> None:
        """Cancel every running job and wait up to `timeout` seconds (registered with atexit by `init`)."""
        with self._lock:
            self._closed = True
            jobs = [j for j in self._jobs.values() if j.status == "running"]
        for job in jobs:
            with job.lock:
                job.closing = job.cancel_requested = True
                job.phase = "cancelling"
            job.cancel_event.set()
        deadline = time.monotonic() + timeout
        for job in jobs:
            if job.thread is not None:
                job.thread.join(max(0.0, deadline - time.monotonic()))


_manager: JobManager | None = None
_manager_guard = threading.Lock()


def init(store: Store) -> JobManager:
    """Create the app's JobManager and cancel its jobs when the process exits normally."""
    global _manager
    with _manager_guard:
        _manager = JobManager(store)
        atexit.register(_manager.shutdown)
        return _manager


def get_manager() -> JobManager:
    if _manager is None:
        from store import default_store
        return init(default_store())
    return _manager
