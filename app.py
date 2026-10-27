"""Flask app, JSON API, serves static/index.html; binds 127.0.0.1.

The API (ARCHITECTURE 12) wraps the store, the job manager and the engine. Every `/api/` request must
come from our own page (Host 127.0.0.1/localhost and header `X-PGBS: 1`, decision 007 §1); errors are
`{"error": "<sentence>", "code": "<code>"}` with the status from `STATUS`. Passwords that are not
stored are asked for once and kept in memory only (`Passwords`).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

import jobs
from engine import catalog, db
from engine.precheck import run_precheck
from engine.util import EngineError
from jobs import JobError, JobManager
from store import Store, StoreError

log = logging.getLogger(__name__)

APP_VERSION = "0.6.0"
STATIC_DIR = Path(__file__).resolve().parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
GUARD_HEADER = "X-PGBS"
STATUS = {"invalid": 400, "failed": 400, "password_required": 401, "protected": 403, "permission": 403,
          "forbidden": 403, "not_found": 404, "exists": 409, "in_use": 409, "conflict": 409, "busy": 409,
          "readonly": 409}
_AUTH_FAILED = re.compile(r"password authentication failed|no password supplied|password is required", re.I)


class ApiError(Exception):
    def __init__(self, message: str, code: str = "invalid", status: int | None = None):
        self.code, self.status = code, status or STATUS.get(code, 400)
        super().__init__(message)


class Passwords:
    """Passwords typed for servers that do not store one; memory only, until the app stops."""

    def __init__(self):
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, server_id: str) -> str | None:
        with self._lock:
            return self._data.get(server_id)

    def put(self, server_id: str, password: str) -> None:
        with self._lock:
            self._data[server_id] = password

    def forget(self, server_id: str) -> None:
        with self._lock:
            self._data.pop(server_id, None)


def _auth_failure(e: Exception) -> bool:
    cause = e.__cause__ or e
    return getattr(cause, "sqlstate", None) in ("28P01", "28000") or bool(_AUTH_FAILED.search(str(cause)))


def open_in_file_manager(path: str) -> str:
    """Show `path` in the system file manager (selected when it exists); returns what was opened."""
    folder = os.path.dirname(path)
    if os.path.isfile(path):
        target = path
        if os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{path}"])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", folder])
    elif os.path.isdir(folder):
        target = folder
        if os.name == "nt":
            os.startfile(folder)  # noqa: S606 - a folder recorded in history
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])
    else:
        raise ApiError(f"Neither the file nor its folder exists any more: {path}", "not_found")
    return target


def create_app(store: Store | None = None, manager: JobManager | None = None) -> Flask:
    """Build the Flask application and register its routes."""
    app = Flask(__name__, static_folder=None)
    app.json.sort_keys = False
    store = store or Store()
    manager = manager or jobs.init(store)
    passwords = Passwords()
    app.config.update(PGBS_STORE=store, PGBS_MANAGER=manager, PGBS_PASSWORDS=passwords)

    # ------------------------------------------------------------ helpers

    def body() -> dict:
        data = request.get_json(silent=True) if request.data else {}
        if not isinstance(data, dict):
            raise ApiError("The request body must be a JSON object.")
        return data

    def field(data: dict, key: str, kind=str, required: bool = True):
        value = data.get(key)
        if value is None:
            if required:
                raise ApiError(f"'{key}' is required.")
            return None
        if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
            raise ApiError(f"'{key}' has the wrong type.")
        return value

    def server_for(server_id: str, password: str | None = None) -> dict:
        """The stored server with the password to use: typed now, else remembered in memory, else stored."""
        rec = store.get_server(server_id, with_password=True)
        if rec is None:
            raise ApiError("This server no longer exists.", "not_found")
        g.server_id = server_id
        if password is not None:
            if not isinstance(password, str):
                raise ApiError("The password must be text.")
            rec["password"] = password
        elif passwords.get(server_id) is not None:
            rec["password"] = passwords.get(server_id)
        elif not rec.get("password") and not rec.get("remember_password", True):
            raise ApiError(f"Enter the password for {rec['name']}.", "password_required")
        return rec

    def public(rec: dict) -> dict:
        return {**rec, "password_cached": passwords.get(rec["id"]) is not None}

    def no_running_job(server_id: str, name: str, action: str) -> None:
        using = manager.jobs_using(server_id, name)
        if using:
            raise ApiError(f"Cannot {action} {name} while a backup or restore uses it (job {using[0]}); "
                           f"wait for it to finish or cancel it.", "conflict")

    def database_connection(server: dict, name: str):
        try:
            return db.connect(server, name, autocommit=True)
        except EngineError as e:
            if db._missing_database(e):
                raise ApiError(f"Database {name} does not exist.", "not_found") from e
            raise

    # ------------------------------------------------------------ guard and errors

    @app.before_request
    def guard():
        if not request.path.startswith("/api/"):
            return None
        if request.host.rsplit(":", 1)[0] not in LOCAL_HOSTS and request.host not in LOCAL_HOSTS:
            raise ApiError("Requests must be addressed to 127.0.0.1 or localhost.", "forbidden")
        if request.headers.get(GUARD_HEADER) != "1":
            raise ApiError("This API only answers PG Backup Studio's own page.", "forbidden")
        if request.data and request.mimetype != "application/json":
            raise ApiError("Send the request body as JSON (Content-Type: application/json).")
        return None

    def error(message: str, code: str, status: int | None = None):
        return jsonify({"error": message, "code": code}), status or STATUS.get(code, 400)

    @app.errorhandler(ApiError)
    def on_api_error(e: ApiError):
        return error(str(e), e.code, e.status)

    @app.errorhandler(StoreError)
    @app.errorhandler(JobError)
    def on_coded(e):
        return error(str(e), e.code)

    @app.errorhandler(EngineError)
    def on_engine(e: EngineError):
        if _auth_failure(e):
            sid = g.get("server_id")
            if sid:
                passwords.forget(sid)
            return error(f"The server rejected the password: {e}", "password_required")
        return error(str(e), getattr(e, "code", "failed"))

    @app.errorhandler(HTTPException)
    def on_http(e: HTTPException):
        if not request.path.startswith("/api/"):
            return e
        return error(e.description or e.name, "not_found" if e.code == 404 else "invalid", e.code)

    @app.errorhandler(Exception)
    def on_unexpected(e: Exception):
        log.exception("unexpected error in %s %s", request.method, request.path)
        return error(f"Unexpected error: {type(e).__name__}", "unexpected", 500)

    # ------------------------------------------------------------ page and app

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/app")
    def app_info():
        return {"version": APP_VERSION, "warnings": list(store.warnings)}

    # ------------------------------------------------------------ servers

    @app.get("/api/servers")
    def servers_list():
        return jsonify([public(s) for s in store.list_servers()])

    @app.post("/api/servers")
    def servers_save():
        data = body()
        rec = store.save_server(data)
        passwords.forget(rec["id"])
        return jsonify(public(rec)), 200 if data.get("id") else 201

    @app.delete("/api/servers/<sid>")
    def servers_delete(sid):
        if not store.delete_server(sid):
            raise ApiError("This server no longer exists.", "not_found")
        passwords.forget(sid)
        return {"deleted": True}

    @app.post("/api/servers/test")
    def servers_test():
        data = body()
        old = None
        if data.get("id"):
            old = store.get_server(data["id"], with_password=True)
            if old is None:
                raise ApiError("This server no longer exists.", "not_found")
            if data.get("password") is None and passwords.get(data["id"]) is not None:
                old["password"] = passwords.get(data["id"])
        rec = Store._clean_server({"name": "test", **data, "remember_password": True}, old, [])
        try:
            conn = db.connect_maintenance(rec)
        except EngineError as e:
            return {"ok": False, "error": str(e)}
        try:
            info = db.server_info(conn)
        finally:
            conn.close()
        return {"ok": True, "version": info["version"], "user": info["user"], "is_superuser": info["is_superuser"]}

    def info_of(server: dict) -> dict:
        conn = db.connect_maintenance(server)
        try:
            info = db.server_info(conn)
            info["can_create_db"] = info["is_superuser"] or bool(conn.execute(
                "SELECT rolcreatedb FROM pg_catalog.pg_roles WHERE rolname = current_user").fetchone()[0])
            return info
        finally:
            conn.close()

    @app.post("/api/servers/<sid>/connect")
    def servers_connect(sid):
        password = body().get("password")
        server = server_for(sid, password)
        info = info_of(server)
        if password is not None and password != (store.get_server(sid, with_password=True) or {}).get("password"):
            passwords.put(sid, password)
        return info

    @app.get("/api/servers/<sid>/info")
    def servers_info(sid):
        return info_of(server_for(sid))

    # ------------------------------------------------------------ databases

    @app.get("/api/servers/<sid>/databases")
    def databases_list(sid):
        rows = db.list_databases(server_for(sid))
        for r in rows:
            r["busy"] = manager.jobs_using(sid, r["name"])
        return jsonify(rows)

    @app.get("/api/servers/<sid>/roles")
    def roles_list(sid):
        return jsonify(db.list_roles(server_for(sid)))

    @app.get("/api/servers/<sid>/templates")
    def templates_list(sid):
        return jsonify(db.list_templates(server_for(sid)))

    @app.post("/api/servers/<sid>/databases")
    def databases_create(sid):
        data = body()
        server = server_for(sid)
        name = field(data, "name")
        owner = field(data, "owner", required=False) or None
        source = field(data, "clone_from", required=False)
        if source:
            no_running_job(sid, source, "clone")
            row = db.clone_database(server, source, name, owner=owner)
        else:
            row = db.create_database(server, name, owner=owner,
                                     template=field(data, "template", required=False) or None,
                                     encoding=field(data, "encoding", required=False) or None)
        return jsonify(row), 201

    @app.delete("/api/servers/<sid>/databases/<name>")
    def databases_drop(sid, name):
        confirm = body().get("confirm")
        server = server_for(sid)
        no_running_job(sid, name, "drop")
        result = db.drop_database(server, name, confirm=confirm)
        store.delete_profile(sid, name)
        return result

    @app.post("/api/servers/<sid>/databases/<name>/rename")
    def databases_rename(sid, name):
        new_name = field(body(), "new_name")
        server = server_for(sid)
        no_running_job(sid, name, "rename")
        return db.rename_database(server, name, new_name)

    @app.post("/api/servers/<sid>/databases/<name>/kill")
    def databases_kill(sid, name):
        server = server_for(sid)
        no_running_job(sid, name, "end the sessions on")
        return db.kill_connections(server, name)

    @app.get("/api/servers/<sid>/databases/<name>/sessions")
    def databases_sessions(sid, name):
        server = server_for(sid)
        if name not in {r["name"] for r in db.list_databases(server)}:
            raise ApiError(f"Database {name} does not exist.", "not_found")
        return {"sessions": db.database_sessions(server, name)}

    @app.get("/api/servers/<sid>/databases/<name>/contents")
    def databases_contents(sid, name):
        return db.database_contents(server_for(sid), name)

    @app.get("/api/servers/<sid>/databases/<name>/schemas")
    def databases_schemas(sid, name):
        conn = database_connection(server_for(sid), name)
        try:
            return jsonify([s["name"] for s in catalog.resolve_schemas(conn, None)])
        finally:
            conn.close()

    @app.post("/api/servers/<sid>/databases/<name>/check")
    def databases_check(sid, name):
        data = body()
        server = server_for(sid)
        schemas = data.get("schemas")
        if schemas is not None and not (isinstance(schemas, list) and all(isinstance(s, str) for s in schemas)):
            raise ApiError("'schemas' must be a list of names.")
        tables = data.get("tables")
        if tables is not None and not isinstance(tables, list):
            raise ApiError("'tables' must be a list.")
        mode = data.get("mode") or "full"
        if mode not in ("full", "schema", "data"):
            raise ApiError("'mode' must be full, schema or data.")
        database_connection(server, name).close()
        return run_precheck(server, name, schemas=schemas, mode=mode, tables=tables,
                            thresholds=store.get_settings())

    @app.get("/api/servers/<sid>/databases/<name>/profile")
    def profile_get(sid, name):
        return {"profile": store.get_profile(sid, name)}

    @app.delete("/api/servers/<sid>/databases/<name>/profile")
    def profile_delete(sid, name):
        return {"deleted": store.delete_profile(sid, name)}

    # ------------------------------------------------------------ jobs

    @app.post("/api/backup")
    def backup_start():
        data = body()
        sid = field(data, "server_id")
        server = server_for(sid, data.get("password"))
        job_id = manager.start_backup(sid, field(data, "database"), data.get("options"),
                                      password=server.get("password"))
        return {"job_id": job_id}, 202

    @app.post("/api/restore")
    def restore_start():
        data = body()
        sid = field(data, "server_id")
        server = server_for(sid, data.get("password"))
        history_id = field(data, "history_id", required=False)
        if history_id:
            entry = store.get_history(history_id)
            if entry is None or entry.get("kind") != "backup" or entry.get("status") != "success":
                raise ApiError("That history entry is not a finished backup.", "not_found")
            path = entry.get("file")
        else:
            path = field(data, "file")
        job_id = manager.start_restore(sid, field(data, "database"), path, data.get("options"),
                                       password=server.get("password"), source_history_id=history_id,
                                       target=data.get("target"))
        return {"job_id": job_id}, 202

    @app.get("/api/jobs")
    def jobs_list():
        return jsonify(manager.list_jobs())

    @app.get("/api/jobs/<job_id>")
    def jobs_get(job_id):
        log_after = request.args.get("log_after", type=int)
        snap = manager.get_job(job_id, log_after)
        if snap is None:
            raise ApiError("This job is not known (it may have finished long ago; see History).", "not_found")
        return snap

    @app.post("/api/jobs/<job_id>/cancel")
    def jobs_cancel(job_id):
        if manager.get_job(job_id, log_after=10 ** 9) is None:
            raise ApiError("This job is not known.", "not_found")
        return {"cancelled": manager.cancel_job(job_id)}

    # ------------------------------------------------------------ history and settings

    @app.get("/api/history")
    def history_list():
        return jsonify(store.list_history())

    @app.delete("/api/history/<entry_id>")
    def history_delete(entry_id):
        delete_file = request.args.get("delete_file", "").lower() in ("1", "true", "yes")
        entry = store.get_history(entry_id)
        if entry is None:
            raise ApiError("This history entry no longer exists.", "not_found")
        if entry.get("status") == "running":
            raise ApiError("This job is still running; cancel it first.", "conflict")
        return store.delete_history(entry_id, delete_file=delete_file)

    @app.post("/api/history/<entry_id>/open-folder")
    def history_open_folder(entry_id):
        entry = store.get_history(entry_id)
        if entry is None or not isinstance(entry.get("file"), str):
            raise ApiError("This history entry has no file.", "not_found")
        return {"opened": open_in_file_manager(entry["file"])}

    @app.get("/api/settings")
    def settings_get():
        return store.get_settings()

    @app.post("/api/settings")
    def settings_save():
        return store.save_settings(body())

    return app


def main() -> None:
    """Run the server on 127.0.0.1 (port from PORT, default 8000)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), threaded=True)


if __name__ == "__main__":
    main()
