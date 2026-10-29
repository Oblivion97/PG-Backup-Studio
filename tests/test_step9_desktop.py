"""Step 9: desktop app — data folder, launch token, folder picker, import, close question, build files (decision 010)."""
import re
import sys
from pathlib import Path

import pytest

import store as store_mod
from app import TOKEN_COOKIE, create_app
from engine.console import ConsoleManager
from jobs import JobManager
from store import APP_NAME, Store, StoreError

ROOT = Path(__file__).resolve().parent.parent
H = {"X-PGBS": "1"}


class FakeDesktop:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def pick_folder(self, start):
        self.asked.append(start)
        return self.answer


def client_for(tmp_path, **kw):
    st = Store(tmp_path / "data")
    return st, create_app(st, JobManager(st), ConsoleManager(), **kw).test_client()


# ---------------------------------------------------------------- data folder

def test_data_folder_is_app_data_only_when_frozen(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert store_mod.default_data_dir() == store_mod.APP_DIR / "data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert store_mod.default_data_dir() == tmp_path / APP_NAME
    monkeypatch.delenv("PGBS_DATA_DIR", raising=False)
    assert Store().dir == (tmp_path / APP_NAME).resolve()


def test_import_copies_valid_files_only(tmp_path):
    old = Store(tmp_path / "old")
    old.save_server({"name": "Prod", "host": "db1", "port": 5432, "user": "u", "password": "pw"})
    old.save_settings({"history_limit": 77})
    new = Store(tmp_path / "new")
    assert sorted(new.import_from(old.dir)) == ["servers.json", "settings.json"]
    assert [s["name"] for s in new.list_servers()] == ["Prod"] and new.get_settings()["history_limit"] == 77
    with pytest.raises(StoreError, match="already uses"):
        new.import_from(new.dir)
    with pytest.raises(StoreError, match="No PG Backup Studio data files"):
        new.import_from(tmp_path / "empty")
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "servers.json").write_text('{"version": 1, "servers": []}', encoding="utf-8")
    (bad / "settings.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError, match="cannot be read"):
        new.import_from(bad)
    (bad / "settings.json").write_text('{"version": 1, "settings": []}', encoding="utf-8")
    with pytest.raises(StoreError, match="not a PG Backup Studio file"):
        new.import_from(bad)
    assert [s["name"] for s in new.list_servers()] == ["Prod"], "nothing is written when one file is bad"


# ---------------------------------------------------------------- launch token

def test_launch_token_guards_the_api(tmp_path):
    _, c = client_for(tmp_path, launch_token="s3cret-token")
    assert c.get("/api/app", headers=H).status_code == 403
    assert c.get("/launch/wrong").status_code == 404
    r = c.get("/launch/s3cret-token")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    cookie = r.headers["Set-Cookie"]
    assert f"{TOKEN_COOKIE}=s3cret-token" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert c.get("/api/app", headers=H).status_code == 200   # the test client keeps the cookie
    assert c.get("/").status_code == 200


def test_browser_mode_has_no_token_and_no_launch(tmp_path):
    _, c = client_for(tmp_path)
    info = c.get("/api/app", headers=H).get_json()
    assert info["desktop"] is False and info["data_dir"].endswith("data")
    assert c.get("/launch/anything").status_code == 404
    r = c.post("/api/pick-folder", json={}, headers=H)
    assert r.status_code == 400 and "desktop app" in r.get_json()["error"]


# ---------------------------------------------------------------- folder picker, settings, import

def test_pick_folder_uses_the_window(tmp_path):
    fake = FakeDesktop(str(tmp_path / "chosen"))
    _, c = client_for(tmp_path, desktop=fake)
    assert c.get("/api/app", headers=H).get_json()["desktop"] is True
    r = c.post("/api/pick-folder", json={"start": str(tmp_path)}, headers=H)
    assert r.get_json() == {"path": str(tmp_path / "chosen")} and fake.asked == [str(tmp_path)]
    c.post("/api/pick-folder", json={"start": str(tmp_path / "missing")}, headers=H)
    assert fake.asked[-1] == str(Path.home()), "a folder that does not exist starts at home"
    fake.answer = None
    assert c.post("/api/pick-folder", json={}, headers=H).get_json() == {"path": None}


def test_backup_folder_must_be_writable(tmp_path):
    st, c = client_for(tmp_path)
    target = tmp_path / "new" / "backups"
    r = c.post("/api/settings", json={"output_dir": str(target)}, headers=H)
    assert r.status_code == 200 and r.get_json()["output_dir"] == str(target) and target.is_dir()
    assert not list(target.iterdir()), "the write test leaves nothing behind"
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    r = c.post("/api/settings", json={"output_dir": str(a_file / "sub")}, headers=H)
    assert r.status_code == 400 and "cannot be written" in r.get_json()["error"]
    assert st.get_settings()["output_dir"] == str(target)


def test_import_through_the_api(tmp_path):
    old = Store(tmp_path / "old")
    old.save_server({"name": "Old", "host": "h", "port": 5432, "user": "u", "password": "pw"})
    st, c = client_for(tmp_path, desktop=FakeDesktop(None))
    r = c.post("/api/data-folder/import", json={"folder": str(old.dir)}, headers=H)
    assert r.status_code == 200 and r.get_json()["copied"] == ["servers.json"]
    assert [s["name"] for s in st.list_servers()] == ["Old"]
    assert b"pw" not in r.data
    assert c.post("/api/data-folder/import", json={"folder": str(tmp_path / "none")}, headers=H).status_code == 400


# ---------------------------------------------------------------- desktop launcher

def test_close_question():
    desktop = pytest.importorskip("desktop")

    class M:
        def __init__(self, jobs): self.jobs = jobs
        def list_jobs(self): return self.jobs

    class C:
        def __init__(self, n): self.n = n
        def open_transactions(self): return self.n

    assert desktop.close_question(M([{"status": "done"}]), C(0)) is None
    q = desktop.close_question(M([{"status": "running"}]), C(2))
    assert "1 backup/restore job is still running" in q and "2 query tabs have an open transaction" in q


def test_open_transactions_counts_nothing_without_sessions():
    assert ConsoleManager().open_transactions() == 0


# ---------------------------------------------------------------- build files agree

def test_build_files_agree():
    iss = (ROOT / "installer" / "pg_backup_studio.iss").read_text(encoding="utf-8")
    spec = (ROOT / "pg_backup_studio.spec").read_text(encoding="utf-8")
    desktop_src = (ROOT / "desktop.py").read_text(encoding="utf-8")
    mutex = re.search(r'MUTEX_NAME = "(.+?)"', desktop_src).group(1).replace("\\\\", "\\")
    assert f"AppMutex={mutex}" in iss
    assert "AppId={{517E3A83-B21F-4FE1-B0C7-4C99A438F80D}" in iss, "the AppId must never change (updates, uninstall)"
    assert '("static", "static")' in spec and "console=False" in spec and '"desktop.py"' in spec
    assert f'#define AppName "{APP_NAME}"' in iss and f'APP_NAME = "{APP_NAME}"' in spec
    assert (ROOT / "installer" / "app.ico").read_bytes()[:4] == b"\0\0\1\0"
    reqs = (ROOT / "requirements-desktop.txt").read_text(encoding="utf-8")
    assert "pywebview" in reqs and "pyinstaller" in reqs and "-r requirements.txt" in reqs
