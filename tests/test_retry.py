"""Decision 014: retry a failed / cancelled / interrupted backup or restore, TCP keepalives, and the file dialogs.

Fake engines only (no database); the Windows dialog itself is checked by hand (it needs a person to click).
"""
import secrets
import threading
import time

import pytest

import native_dialogs
from app import create_app
from engine import db
from jobs import JobError, JobManager
from store import Store

PW = secrets.token_hex(8)
SERVER = {"name": "Local", "host": "localhost", "port": 5432, "user": "postgres", "password": PW}
RESULT = {"file": "x.sql", "size": 10, "objects": 1, "tables": 1, "rows": 3, "warnings": [], "partial": False,
          "partial_tables": []}
RESTORE_RESULT = {"status": "ok", "error_count": 0, "warning_count": 0, "errors": [], "warnings": [], "rows": 3,
                  "objects": 1}
NETWORK = "server closed the connection unexpectedly"


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def sid(st):
    return st.save_server(dict(SERVER))["id"]


class Engine:
    """Fake engine: fails the first `fail` calls with a connection error, then succeeds. Records each call."""

    def __init__(self, result, fail=1, wait: threading.Event | None = None):
        self.result, self.fail, self.wait, self.calls = result, fail, wait, []

    def __call__(self, server, db_name, *rest, progress=None, cancel=None):
        self.calls.append((server, db_name, rest))
        if self.wait is not None:
            while not self.wait.wait(0.02):
                pass
        if len(self.calls) <= self.fail:
            raise db.EngineError(NETWORK)
        return self.result


def done(mgr, job_id, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = mgr.get_job(job_id)
        if snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_backup_retry_reuses_settings_and_links_entries(st, sid, tmp_path):
    eng = Engine(RESULT)
    mgr = JobManager(st, backup_fn=eng)
    opts = {"out_dir": str(tmp_path), "gzip": True, "schemas": ["public"], "filename": "fixed.sql", "lock_timeout_s": 7}
    first = done(mgr, mgr.start_backup(sid, "shop", opts))
    assert first["status"] == "failed" and NETWORK in first["error"]
    entry = st.get_history(first["id"])
    assert entry["retry"]["options"]["gzip"] is True and "filename" not in entry["retry"]["options"]
    again = done(mgr, mgr.retry(first["id"]))
    assert again["status"] == "success" and again["retried_from"] == first["id"]
    used = eng.calls[1][2][0]
    assert (used.gzip, used.schemas, used.lock_timeout_s, used.out_dir, used.filename) == \
        (True, ["public"], 7, str(tmp_path), None)   # a fresh timestamped file, not the fixed name
    assert eng.calls[1][0]["password"] == PW
    assert st.get_history(again["id"])["retry"]["from"] == first["id"]


def test_only_failed_cancelled_or_interrupted_backups_and_restores(st, sid, tmp_path):
    mgr = JobManager(st, backup_fn=Engine(RESULT, fail=0))
    ok = done(mgr, mgr.start_backup(sid, "shop", {"out_dir": str(tmp_path)}))
    with pytest.raises(JobError, match="failed, cancelled or interrupted"):
        mgr.retry(ok["id"])
    with pytest.raises(JobError) as e:
        mgr.retry("nope")
    assert e.value.code == "not_found"
    old = {**st.get_history(ok["id"]), "id": "oldentry0001", "status": "failed"}
    old.pop("retry")
    st.add_history(old)
    with pytest.raises(JobError, match="older version"):
        mgr.retry("oldentry0001")
    st.add_history({**old, "id": "transfer0001", "kind": "transfer", "retry": {}})
    with pytest.raises(JobError, match="Only a backup or a restore"):
        mgr.retry("transfer0001")


def test_interrupted_entry_is_retried_after_restart(st, sid, tmp_path):
    gate = threading.Event()
    mgr = JobManager(st, backup_fn=Engine(RESULT, fail=0, wait=gate))
    jid = mgr.start_backup(sid, "shop", {"out_dir": str(tmp_path)})
    # the app "ends" while it runs: a new manager on the same data marks it interrupted
    mgr2 = JobManager(st, backup_fn=Engine(RESULT, fail=0))
    assert st.get_history(jid)["status"] == "interrupted"
    assert done(mgr2, mgr2.retry(jid))["status"] == "success"
    gate.set()


def test_second_retry_while_one_runs_is_refused(st, sid, tmp_path):
    gate = threading.Event()
    eng = Engine(RESULT, fail=1)
    mgr = JobManager(st, backup_fn=eng)
    first = done(mgr, mgr.start_backup(sid, "shop", {"out_dir": str(tmp_path)}))
    eng.wait = gate
    jid = mgr.retry(first["id"])
    with pytest.raises(JobError, match="already running") as e:
        mgr.retry(first["id"])
    assert e.value.code == "conflict"
    gate.set()
    assert done(mgr, jid)["status"] == "success"


def test_restore_retry_target_choices(st, sid, tmp_path):
    f = tmp_path / "b.sql"
    f.write_text("SELECT 1;\n")
    eng = Engine(RESTORE_RESULT, fail=1)
    mgr = JobManager(st, restore_fn=eng, prepare_fn=lambda job: None)
    first = done(mgr, mgr.start_restore(sid, "copy", str(f), {"on_error": "stop"},
                                        target={"mode": "create", "owner": "alice"}))
    assert first["status"] == "failed" and first["target_mode"] == "create"
    saved = st.get_history(first["id"])["retry"]["target"]
    assert saved == {"mode": "create", "owner": "alice", "allow_non_empty": False} and "confirm" not in saved
    with pytest.raises(JobError, match="Type the database name"):
        mgr.retry(first["id"], target={"mode": "recreate"})
    again = done(mgr, mgr.retry(first["id"], target={"mode": "recreate", "confirm": "copy"}))
    assert again["status"] == "success" and again["target_mode"] == "recreate"
    job_target = st.get_history(again["id"])["retry"]["target"]
    assert job_target == {"mode": "recreate", "owner": "alice", "allow_non_empty": False}   # owner kept
    assert eng.calls[1][2][1].on_error == "stop"
    eng.fail = 3
    third = done(mgr, mgr.retry(first["id"], target={"mode": "existing", "allow_non_empty": True}))
    assert third["status"] == "failed" and third["target_mode"] == "existing"


def test_retry_api_and_password_prompt(st, tmp_path):
    rec = st.save_server({**SERVER, "password": "", "remember_password": False})
    eng = Engine(RESULT)
    mgr = JobManager(st, backup_fn=eng)
    c = create_app(st, mgr).test_client()
    h = {"X-PGBS": "1"}
    first = done(mgr, mgr.start_backup(rec["id"], "shop", {"out_dir": str(tmp_path)}, password=PW))
    r = c.post(f"/api/history/{first['id']}/retry", json={}, headers=h)
    assert r.status_code == 401 and r.get_json()["code"] == "password_required"
    r = c.post(f"/api/history/{first['id']}/retry", json={"password": PW, "server_id": rec["id"]}, headers=h)
    assert r.status_code == 202
    assert done(mgr, r.get_json()["job_id"])["status"] == "success" and eng.calls[1][0]["password"] == PW
    assert c.post("/api/history/missing/retry", json={}, headers=h).status_code == 404
    assert c.post(f"/api/history/{first['id']}/retry", json={"target": "x"}, headers=h).status_code in (400, 401)


def test_keepalives_are_set(monkeypatch):
    seen = {}

    def fake_connect(**kw):
        seen.update(kw)
        raise db.psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    with pytest.raises(db.EngineError):
        db.connect({"host": "h", "password": PW})
    assert seen["keepalives"] == 1 and seen["keepalives_idle"] == db.KEEPALIVE_IDLE_S
    assert seen["keepalives_interval"] * seen["keepalives_count"] + seen["keepalives_idle"] <= 120


def test_dialog_routes_use_the_system_dialog_in_a_browser(st, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(native_dialogs, "pick_folder", lambda start: calls.append(("folder", start)) or str(tmp_path))
    monkeypatch.setattr(native_dialogs, "pick_file", lambda start: calls.append(("file", start)) or None)
    monkeypatch.setattr(native_dialogs, "available", lambda: True)
    st.save_settings({"output_dir": str(tmp_path)})
    c = create_app(st, JobManager(st)).test_client()   # no desktop window: browser
    h = {"X-PGBS": "1"}
    assert c.get("/api/app", headers=h).get_json()["dialogs"] is True
    assert c.post("/api/pick-folder", json={"start": "~/x"}, headers=h).get_json() == {"path": str(tmp_path)}
    assert c.post("/api/pick-file", json={}, headers=h).get_json() == {"path": None}
    assert calls == [("folder", "~/x"), ("file", str(tmp_path))]   # the file dialog starts in the backup folder
    assert c.post("/api/pick-file", json={"start": 5}, headers=h).status_code == 400

    def busy(start):
        raise native_dialogs.DialogError("A file dialog is already open")
    monkeypatch.setattr(native_dialogs, "pick_folder", busy)
    r = c.post("/api/pick-folder", json={}, headers=h)
    assert r.status_code == 400 and "already open" in r.get_json()["error"]


def test_start_dir_falls_back_to_nearest_existing_folder(tmp_path):
    f = tmp_path / "a.sql"
    f.write_text("x")
    assert native_dialogs.start_dir(str(f)) == str(tmp_path)
    assert native_dialogs.start_dir(str(tmp_path / "missing" / "deeper")) == str(tmp_path)
    assert native_dialogs.start_dir("") and native_dialogs.start_dir(None)


def test_new_default_folder_in_settings_replaces_remembered_folders(st, sid, tmp_path):
    """User report 2026-11-19: the folder changed in Settings was ignored because each database's backup profile
    remembered the folder of its last backup."""
    st.save_profile(sid, "shop", {"saved_at": "2026-11-17T11:04:23+06:00", "options": {"mode": "full", "out_dir": "~/old"}})
    st.save_profile(sid, "crm", {"saved_at": "2026-11-17T11:04:23+06:00", "options": {"mode": "full"}})
    c = create_app(st, JobManager(st)).test_client()
    h = {"X-PGBS": "1"}
    new = tmp_path / "new backups"
    new.mkdir()
    assert c.post("/api/settings", json={"large_table_mb": 600}, headers=h).status_code == 200
    assert st.get_profile(sid, "shop")["options"]["out_dir"] == "~/old"     # folder unchanged: nothing forgotten
    assert c.post("/api/settings", json={"output_dir": str(new)}, headers=h).get_json()["output_dir"] == str(new)
    assert "out_dir" not in st.get_profile(sid, "shop")["options"]
    assert st.get_profile(sid, "shop")["options"]["mode"] == "full" and st.get_profile(sid, "crm")
