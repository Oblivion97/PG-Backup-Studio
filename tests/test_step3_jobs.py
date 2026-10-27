"""Step 3: jobs.py. Fake-engine tests need no database; the rest run against real PostgreSQL.

ARCHITECTURE 10.9 items 6-12.
"""
import os
import threading
import time

import pytest

import jobs as jobs_mod
from conftest import connect, pg_settings
from engine.util import BackupBlockedError, Cancelled, LockTimeoutError
from jobs import JobError, JobManager
from store import Store

NO_PREPARE = lambda job: None  # noqa: E731 - fake engine tests need no database
SERVER = {"name": "Local", "host": "localhost", "port": 5432, "user": "postgres", "password": "pw"}


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def sid(st):
    return st.save_server(dict(SERVER))["id"]


def wait_done(mgr, job_id, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = mgr.get_job(job_id)
        if snap["status"] != "running":
            return snap
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {mgr.get_job(job_id)}")


class Script:
    """A fake engine: replays events, pausing at each `gate` until the test calls `step()`."""

    def __init__(self, events, result=None, raises=None):
        self.events, self.result, self.raises = events, result, raises
        self.go, self.at = threading.Semaphore(0), threading.Event()
        self.seen_server = None

    def step(self):
        self.go.release()

    def __call__(self, server, db, *rest, progress=None, cancel=None):
        self.seen_server = server
        for ev in self.events:
            if ev == "gate":
                self.at.set()
                while not self.go.acquire(timeout=0.02):
                    if cancel.is_set():
                        raise Cancelled("Cancelled by user")
                self.at.clear()
                continue
            progress(*ev)
        if self.raises:
            raise self.raises
        return self.result


def gated(mgr, script):
    """Advance a gated script one gate and return the snapshot taken while it waits at the next gate."""
    script.step()
    time.sleep(0.05)
    assert script.at.wait(2)


BACKUP_RESULT = {"file": "x.sql", "size": 10, "objects": 4, "tables": 2, "rows": 30, "warnings": ["w"],
                 "partial": True, "partial_tables": [{"table": "t", "data": "schema"}]}


# ---------------------------------------------------------------- 7. formulas (fake engine)

def test_backup_percent_weights_and_cap(st, sid, tmp_path):
    s = Script(["gate",
                ("phase", {"name": "catalog"}), "gate",
                ("totals", {"objects": 10, "tables": 2, "est_rows": 100, "data_bytes": 1000}),
                ("phase", {"name": "write"}),
                ("object", {"objects_done": 5, "bytes_written": 50}), "gate",
                ("table_start", {"name": "a", "est_rows": 50, "size": 600}),
                ("table_progress", {"name": "a", "rows": 25, "bytes_copied": 300, "bytes_written": 400}), "gate",
                ("table_progress", {"name": "a", "rows": 99, "bytes_copied": 5000, "bytes_written": 900}), "gate",
                ("table_done", {"name": "a", "rows": 50, "bytes_written": 1000}), "gate",
                ("object", {"objects_done": 10}), "gate",
                ("table_start", {"name": "b", "est_rows": 50, "size": 400}),
                ("table_done", {"name": "b", "rows": 50}), ("phase", {"name": "done"})],
               result={**BACKUP_RESULT, "file": str(tmp_path / "x.sql")})
    mgr = JobManager(st, backup_fn=s)
    jid = mgr.start_backup(sid, "db", {"out_dir": str(tmp_path)})
    assert s.at.wait(2)
    seen = [mgr.get_job(jid)["percent"]]                                   # connecting
    for _ in range(6):
        gated(mgr, s)
        seen.append(mgr.get_job(jid)["percent"])
    # catalog 0; 5/10 objects -> 10; + 300/600 of 600 bytes -> 10+24=34; capped 0.99 -> 10+47.52; done a -> 58;
    # all objects -> 68
    assert seen == [0.0, 0.0, 10.0, 34.0, 57.5, 58.0, 68.0]
    snap = mgr.get_job(jid)
    assert snap["progress"]["rows"] == 50 and snap["progress"]["tables_done"] == 1
    s.step()
    end = wait_done(mgr, jid)
    assert end["status"] == "success" and end["percent"] == 100.0 and end["eta_s"] is None


@pytest.mark.parametrize("totals, after, expected", [
    ({"objects": 4, "tables": 0, "est_rows": 0, "data_bytes": 0}, [("object", {"objects_done": 1})], 25.0),
    ({"objects": 0, "tables": 1, "est_rows": 0, "data_bytes": 100},
     [("table_start", {"name": "t", "size": 100}), ("table_progress", {"bytes_copied": 40})], 40.0),
    ({"objects": 0, "tables": 0, "est_rows": 0, "data_bytes": 0}, [], 0.0),
])
def test_backup_percent_edge_weights(st, sid, totals, after, expected):
    s = Script([("totals", totals), *after, "gate"], result=BACKUP_RESULT)
    mgr = JobManager(st, backup_fn=s)
    jid = mgr.start_backup(sid, "db")
    assert s.at.wait(2)
    assert mgr.get_job(jid)["percent"] == expected
    s.step()
    wait_done(mgr, jid)


def test_restore_percent_from_position_and_zero_size(st, sid, tmp_path):
    f = tmp_path / "f.sql"
    f.write_text("SELECT 1;")
    s = Script([("phase", {"name": "restore"}), ("totals", {"file_size": 200}),
                ("position", {"pos": 50, "rows_loaded": 7, "errors": 0, "warnings": 0}), "gate",
                ("position", {"pos": 20, "rows_loaded": 8, "errors": 0, "warnings": 0}), "gate"],
               result={"status": "ok", "objects": 1, "rows": 8, "errors": [], "error_count": 0, "warning_count": 0})
    mgr = JobManager(st, restore_fn=s, prepare_fn=NO_PREPARE)
    jid = mgr.start_restore(sid, "db", str(f))
    assert s.at.wait(2)
    assert mgr.get_job(jid)["percent"] == 25.0 and mgr.get_job(jid)["progress"]["rows"] == 7
    gated(mgr, s)
    assert mgr.get_job(jid)["percent"] == 25.0                            # never decreases
    s.step()
    assert wait_done(mgr, jid)["percent"] == 100.0

    s0 = Script([("totals", {"file_size": 0}), ("position", {"pos": 0}), "gate"], result={"status": "ok"})
    mgr0 = JobManager(st, restore_fn=s0, prepare_fn=NO_PREPARE)
    jid0 = mgr0.start_restore(sid, "db2", str(f))
    assert s0.at.wait(2) and mgr0.get_job(jid0)["percent"] == 0.0
    s0.step()
    wait_done(mgr0, jid0)


def test_eta_needs_two_percent_and_two_seconds(st, sid):
    s = Script([("totals", {"objects": 100, "tables": 0, "est_rows": 0, "data_bytes": 0}),
                ("phase", {"name": "write"}), ("object", {"objects_done": 1}), "gate",
                ("object", {"objects_done": 50}), "gate"], result=BACKUP_RESULT)
    mgr = JobManager(st, backup_fn=s)
    jid = mgr.start_backup(sid, "db")
    assert s.at.wait(2)
    assert mgr.get_job(jid)["eta_s"] is None                              # 1 % < 2 %
    gated(mgr, s)
    assert mgr.get_job(jid)["eta_s"] is None                              # < 2 s
    mgr._jobs[jid].t_work -= 10
    assert 9 <= mgr.get_job(jid)["eta_s"] <= 11                           # 50 % after ~10 s
    s.step()
    wait_done(mgr, jid)


# ---------------------------------------------------------------- lifecycle (fake engine)

def test_success_writes_history_profile_and_output_dir(st, sid, tmp_path):
    out = str(tmp_path / "out")
    s = Script(["gate", ("file", {"path": "p.sql", "part": "p.sql.part"}), "gate"],
               result={**BACKUP_RESULT, "file": "p.sql"})
    mgr = JobManager(st, backup_fn=s)
    jid = mgr.start_backup(sid, "shop", {"out_dir": out, "tables": [{"schema": "app", "table": "t", "data": "schema"}]})
    assert s.at.wait(2)
    entry = st.get_history(jid)
    assert entry["status"] == "running" and entry["part_file"] is None and entry["server"]["name"] == "Local"
    gated(mgr, s)
    assert st.get_history(jid)["part_file"] == "p.sql.part"
    s.step()
    snap = wait_done(mgr, jid)
    entry = st.get_history(jid)          # saved before the status became final
    assert entry["status"] == "success" and entry["part_file"] is None and entry["partial"] is True
    assert entry["partial_tables"] == BACKUP_RESULT["partial_tables"] and entry["rows"] == 30
    assert entry["warning_count"] == 1 and entry["duration_s"] >= 0 and "password" not in str(entry)
    prof = st.get_profile(sid, "shop")
    assert prof["options"]["out_dir"] == out and prof["options"]["tables"][0]["data"] == "schema"
    assert "filename" not in prof["options"] and st.get_settings()["output_dir"] == out
    assert snap["result"]["rows"] == 30 and snap["log"][-1]["msg"].startswith("Backup finished (partial)")


@pytest.mark.parametrize("exc, text", [
    (LockTimeoutError("Table x is locked"), "Table x is locked"),
    (BackupBlockedError([{"message": "No SELECT on t."}]), "No SELECT on t."),
    (RuntimeError("boom"), "Unexpected error: RuntimeError: boom"),
])
def test_failure_records_error_and_no_profile(st, sid, exc, text):
    mgr = JobManager(st, backup_fn=Script([], raises=exc))
    snap = wait_done(mgr, mgr.start_backup(sid, "shop"))
    assert snap["status"] == "failed" and text in snap["error"] and snap["error_count"] == 1
    assert snap["percent"] < 100 and snap["log"][-1]["error"] is True
    entry = st.get_history(snap["id"])
    assert entry["status"] == "failed" and text in entry["error"] and entry["error_count"] == 1
    assert st.get_profile(sid, "shop") is None
    if isinstance(exc, BackupBlockedError):
        assert snap["result"]["problems"] == exc.problems


def test_cancel_and_second_cancel(st, sid):
    s = Script([("phase", {"name": "write"}), "gate", "gate"], result=BACKUP_RESULT)
    mgr = JobManager(st, backup_fn=s)
    jid = mgr.start_backup(sid, "db")
    assert s.at.wait(2)
    assert mgr.cancel_job(jid) is True and mgr.cancel_job(jid) is False
    assert mgr.get_job(jid)["phase"] in ("cancelling", "saving", "done")
    snap = wait_done(mgr, jid, 2)
    assert snap["status"] == "cancelled" and snap["error"] == "Cancelled by user"
    assert st.get_history(jid)["status"] == "cancelled" and st.get_profile(sid, "db") is None
    assert mgr.cancel_job(jid) is False and mgr.cancel_job("nope") is False


def test_start_validation(st, sid, tmp_path):
    mgr = JobManager(st, backup_fn=Script([], result=BACKUP_RESULT))
    cases = [(lambda: mgr.start_backup("missing", "db"), "not_found"),
             (lambda: mgr.start_backup(sid, ""), "invalid"),
             (lambda: mgr.start_backup(sid, "db", {"mode": "weird"}), "invalid"),
             (lambda: mgr.start_backup(sid, "db", "text"), "invalid"),
             (lambda: mgr.start_backup(sid, "db", {"lock_timeout_s": "x"}), "invalid"),
             (lambda: mgr.start_restore(sid, "db", str(tmp_path / "none.sql")), "invalid"),
             (lambda: mgr.start_restore(sid, "db", __file__, {"on_error": "x"}), "invalid")]
    for call, code in cases:
        with pytest.raises(JobError) as e:
            call()
        assert e.value.code == code
    assert st.list_history() == []


def test_password_override_stays_in_memory(st, sid):
    s = Script([], result=BACKUP_RESULT)
    mgr = JobManager(st, backup_fn=s)
    snap = wait_done(mgr, mgr.start_backup(sid, "db", password="typed"))
    assert s.seen_server["password"] == "typed"
    assert st.get_server(sid, with_password=True)["password"] == "pw"
    assert "typed" not in str(snap) and "typed" not in (st.dir / "history.json").read_text(encoding="utf-8")


def test_busy_and_restore_conflict(st, sid, tmp_path):
    f = tmp_path / "f.sql"
    f.write_text("SELECT 1;")
    scripts = []

    def factory(*a, **k):
        sc = Script(["gate"], result={"status": "ok"} if len(a) > 3 else BACKUP_RESULT)
        scripts.append(sc)
        return sc(*a, **k)
    mgr = JobManager(st, backup_fn=factory, restore_fn=factory, prepare_fn=NO_PREPARE, max_running=3)
    first = mgr.start_restore(sid, "target", str(f))
    with pytest.raises(JobError) as e:
        mgr.start_restore(sid, "target", str(f))
    assert e.value.code == "conflict"
    mgr.start_restore(sid, "other", str(f))
    mgr.start_backup(sid, "target")
    with pytest.raises(JobError) as e:
        mgr.start_backup(sid, "x")
    assert e.value.code == "busy"
    deadline = time.monotonic() + 2
    while len(scripts) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    for sc in scripts:
        sc.step()
    assert wait_done(mgr, first)["status"] == "success"


def test_snapshots_are_copies_and_log_after(st, sid):
    s = Script([("warning", {"level": "warning", "msg": "one"}), ("log", {"level": "info", "msg": "two\nlines"}),
                "gate"], result=BACKUP_RESULT)
    mgr = JobManager(st, backup_fn=s)
    jid = mgr.start_backup(sid, "db")
    assert s.at.wait(2)
    snap = mgr.get_job(jid)
    snap["progress"]["rows"] = 999
    snap["log"].clear()
    snap["server"]["name"] = "changed"
    again = mgr.get_job(jid)
    assert again["progress"]["rows"] == 0 and again["log"] and again["server"]["name"] == "Local"
    assert again["warning_count"] == 1 and any(x["msg"] == "two lines" for x in again["log"])
    last = again["log_seq"]
    assert mgr.get_job(jid, log_after=last)["log"] == []
    assert [x["seq"] for x in mgr.get_job(jid, log_after=last - 1)["log"]] == [last]
    assert "log" not in mgr.list_jobs()[0] and "_server" not in str(again)
    s.step()
    wait_done(mgr, jid)


def test_finished_jobs_are_pruned(st, sid):
    mgr = JobManager(st, backup_fn=Script([], result=BACKUP_RESULT), keep_finished=3)
    ids = []
    for _ in range(6):
        ids.append(mgr.start_backup(sid, "db"))
        wait_done(mgr, ids[-1])
    kept = [j["id"] for j in mgr.list_jobs()]
    assert kept[0] == ids[-1] and len(kept) <= 4 and mgr.get_job(ids[0]) is None
    assert len(st.list_history()) == 6


def test_shutdown_cancels_running_jobs(st, sid):
    s = Script(["gate"], result=BACKUP_RESULT)
    mgr = JobManager(st, backup_fn=s)
    jid = mgr.start_backup(sid, "db")
    assert s.at.wait(2)
    mgr.shutdown(timeout=5)
    snap = mgr.get_job(jid)
    assert snap["status"] == "cancelled" and snap["error"] == jobs_mod.CANCELLED_BY_CLOSE
    assert st.get_history(jid)["error"] == jobs_mod.CANCELLED_BY_CLOSE
    with pytest.raises(JobError) as e:
        mgr.start_backup(sid, "db")
    assert e.value.code == "busy"


def test_restart_marks_running_entries_interrupted(st, tmp_path):
    part, other = tmp_path / "x.sql.part", tmp_path / "y.sql.part"
    part.write_bytes(b"partial")
    other.write_bytes(b"other")
    st.add_history({"id": "aaaaaaaaaaaa", "kind": "backup", "status": "running", "part_file": str(part)})
    mgr = JobManager(Store(st.dir))
    assert mgr.recover_interrupted() == 0
    assert st.get_history("aaaaaaaaaaaa")["status"] == "interrupted"
    assert not part.exists() and other.exists()


# ---------------------------------------------------------------- real PostgreSQL

@pytest.fixture
def pg(tmp_path, pg_admin):
    s = pg_settings()
    st = Store(tmp_path / "data")
    sid = st.save_server({"name": "pg", "host": s["host"], "port": s["port"], "user": s["user"],
                          "password": s["password"]})["id"]
    return st, sid


def poll_until_done(mgr, jid, interval=0.05, timeout=120):
    snaps = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = mgr.get_job(jid)
        snaps.append(snap)
        if snap["status"] != "running":
            return snaps
        time.sleep(interval)
    raise AssertionError("job did not finish")


def test_real_backup_and_restore_progress_0_to_100(pg, src_db, restore_db, tmp_path):
    st, sid = pg
    mgr = JobManager(st)
    jid = mgr.start_backup(sid, src_db, {"out_dir": str(tmp_path / "out"), "speed_limit_mb_s": 1})
    snaps = poll_until_done(mgr, jid)
    pcts = [s["percent"] for s in snaps]
    assert pcts == sorted(pcts) and all(p < 100 for p in pcts[:-1]) and pcts[-1] == 100.0
    assert all(s["percent"] == 0 for s in snaps if s["phase"] in ("starting", "connecting", "catalog"))
    order = ["starting", "connecting", "catalog", "write", "saving", "done"]
    phases = [p for i, p in enumerate(s["phase"] for s in snaps) if i == 0 or snaps[i - 1]["phase"] != p]
    assert [order.index(p) for p in phases] == sorted(order.index(p) for p in phases)
    end = snaps[-1]
    assert end["status"] == "success", end["error"]
    assert end["progress"]["rows"] == end["result"]["rows"] and end["eta_s"] is None
    assert end["progress"]["tables_done"] == end["progress"]["tables_total"] == end["result"]["tables"]
    assert os.path.isfile(end["file"]) and not os.path.exists(end["file"] + ".part")
    entry = st.get_history(jid)
    assert entry["file"] == end["file"] and entry["size"] == os.path.getsize(end["file"])
    assert st.get_profile(sid, src_db)["options"]["speed_limit_mb_s"] == 1

    target = restore_db("s3job")
    with connect() as c:
        c.execute(f'CREATE DATABASE "{target}" TEMPLATE template0')
    rid = mgr.start_restore(sid, target, end["file"], source_history_id=jid)
    rsnaps = poll_until_done(mgr, rid)
    rp = [s["percent"] for s in rsnaps]
    assert rp == sorted(rp) and rp[-1] == 100.0
    rend = rsnaps[-1]
    assert rend["status"] == "success" and rend["progress"]["rows"] == rend["result"]["rows"] == end["result"]["rows"]
    rentry = st.get_history(rid)
    assert rentry["kind"] == "restore" and rentry["source_history_id"] == jid and rentry["status"] == "success"


def test_real_schema_only_backup_reaches_100(pg, src_db, tmp_path):
    st, sid = pg
    mgr = JobManager(st)
    snap = wait_done(mgr, mgr.start_backup(sid, src_db, {"out_dir": str(tmp_path), "mode": "schema"}))
    assert snap["status"] == "success" and snap["percent"] == 100.0


def test_real_cancel_mid_copy_cleans_up(pg, src_db, tmp_path):
    st, sid = pg
    out = tmp_path / "cancel"
    mgr = JobManager(st)
    jid = mgr.start_backup(sid, src_db, {"out_dir": str(out), "speed_limit_mb_s": 0.1})
    end = time.monotonic() + 60
    while mgr.get_job(jid)["progress"]["current"] is None and time.monotonic() < end:
        time.sleep(0.02)
    assert st.get_history(jid)["part_file"].endswith(".part")
    t = time.monotonic()
    assert mgr.cancel_job(jid)
    snap = wait_done(mgr, jid, 10)
    assert time.monotonic() - t < 2 and snap["status"] == "cancelled"
    assert list(out.iterdir()) == []
    assert st.get_history(jid)["status"] == "cancelled" and st.get_profile(sid, src_db) is None
    assert mgr.cancel_job(jid) is False
    time.sleep(0.5)
    with connect() as c:
        left = c.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'PG Backup Studio'"
                         " AND datname = %s", (src_db,)).fetchone()[0]
    assert left == 0


def test_real_three_backups_same_second_get_distinct_files(pg, src_db, tmp_path):
    st, sid = pg
    out = tmp_path / "three"
    mgr = JobManager(st)
    ids = [mgr.start_backup(sid, src_db, {"out_dir": str(out)}) for _ in range(3)]

    stop, errors = threading.Event(), []

    def poller():
        try:
            while not stop.is_set():
                for j in ids:
                    mgr.get_job(j, log_after=0)
                mgr.list_jobs()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    pollers = [threading.Thread(target=poller) for _ in range(5)]
    for p in pollers:
        p.start()
    snaps = [wait_done(mgr, j, 120) for j in ids]
    stop.set()
    for p in pollers:
        p.join()
    assert not errors
    files = [s["file"] for s in snaps]
    assert all(s["status"] == "success" for s in snaps) and len(set(files)) == 3
    sizes = {os.path.getsize(f) for f in files}
    assert len(sizes) == 1, "the three files should have identical content length"
    assert not list(out.glob("*.part"))


def test_real_lock_timeout_fails_without_profile(pg, src_db, tmp_path):
    st, sid = pg
    holder = connect(src_db, autocommit=False)
    try:
        holder.execute("LOCK TABLE app.customers IN ACCESS EXCLUSIVE MODE")
        mgr = JobManager(st)
        snap = wait_done(mgr, mgr.start_backup(sid, src_db, {"out_dir": str(tmp_path), "lock_timeout_s": 1}))
    finally:
        holder.rollback()
        holder.close()
    assert snap["status"] == "failed" and "locked" in snap["error"]
    assert st.get_history(snap["id"])["status"] == "failed" and st.get_profile(sid, src_db) is None


def test_real_restore_with_errors_caps_history_errors(pg, restore_db, tmp_path):
    st, sid = pg
    target = restore_db("s3err")
    with connect() as c:
        c.execute(f'CREATE DATABASE "{target}" TEMPLATE template0')
    f = tmp_path / "bad.sql"
    f.write_text("CREATE TABLE ok_t (a int);\n" + "".join(f"SELECT * FROM missing_{i};\n" for i in range(25)),
                 encoding="utf-8")
    mgr = JobManager(st)
    snap = wait_done(mgr, mgr.start_restore(sid, target, str(f)))
    assert snap["status"] == "completed_with_errors" and snap["error_count"] == 25
    entry = st.get_history(snap["id"])
    assert entry["status"] == "completed_with_errors" and entry["error_count"] == 25
    assert len(entry["errors"]) == 20 and set(entry["errors"][0]) == {"object", "sqlstate", "message", "line"}
