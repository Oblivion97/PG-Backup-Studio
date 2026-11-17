"""Removing a saved server: refused while a backup/restore runs on it; its query sessions are closed."""
from app import create_app
from engine.console import ConsoleManager
from jobs import JobManager
from store import Store

H = {"X-PGBS": "1"}


class _Job:
    def __init__(self, sid, db, status="running"):
        self.id, self.status, self.database, self._server = f"j-{sid}-{db}", status, db, {"id": sid}


def setup(tmp_path):
    st = Store(tmp_path / "data")
    jm, cm = JobManager(st), ConsoleManager()
    app = create_app(st, jm, cm)
    s = st.save_server({"name": "Prod", "host": "h", "user": "u"})
    return st, jm, cm, app.test_client(), s["id"]


def test_jobs_using_any_database(tmp_path):
    st, jm, _, _, sid = setup(tmp_path)
    jm._jobs["a"] = _Job(sid, "sales")
    jm._jobs["b"] = _Job(sid, "hr", status="done")
    jm._jobs["c"] = _Job("other", "sales")
    assert jm.jobs_using(sid) == [f"j-{sid}-sales"]
    assert jm.jobs_using(sid, "sales") == [f"j-{sid}-sales"] and jm.jobs_using(sid, "hr") == []


def test_remove_waits_for_running_job(tmp_path):
    st, jm, _, c, sid = setup(tmp_path)
    jm._jobs["a"] = _Job(sid, "sales")
    r = c.delete(f"/api/servers/{sid}", headers=H)
    assert r.status_code == 409 and "running on this server" in r.get_json()["error"]
    assert st.get_server(sid) is not None
    jm._jobs["a"].status = "done"
    assert c.delete(f"/api/servers/{sid}", headers=H).get_json() == {"deleted": True}
    assert st.get_server(sid) is None
    assert c.delete(f"/api/servers/{sid}", headers=H).status_code == 404


def test_remove_closes_the_servers_query_sessions(tmp_path, monkeypatch):
    st, jm, cm, c, sid = setup(tmp_path)
    closed = []
    monkeypatch.setattr(cm, "close_for", lambda server_id, database=None: closed.append(server_id) or 0)
    assert c.delete(f"/api/servers/{sid}", headers=H).status_code == 200
    assert closed == [sid]
