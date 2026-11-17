"""Server export / import: a .json file of saved servers to move to another PC (Settings → Servers)."""
import secrets

import pytest

from app import create_app
from engine.console import ConsoleManager
from jobs import JobManager
from store import SERVERS_EXPORT_FORMAT, Store, StoreError

H = {"X-PGBS": "1"}
SAVED_PW = "T-" + secrets.token_urlsafe(18)   # random per run: no fixed secrets in the code


def make_store(tmp_path, name="pc") -> Store:
    return Store(tmp_path / name)


def add(st, name, host="db.example", port=5432, user="app", password="", remember=True, mdb=None):
    return st.save_server({"name": name, "host": host, "port": port, "user": user, "password": password,
                           "remember_password": remember, "maintenance_db": mdb, "sslmode": "require"})


def test_export_leaves_out_ids_and_passwords_unless_asked(tmp_path):
    st = make_store(tmp_path)
    a = add(st, "Prod", password=SAVED_PW)
    add(st, "Dev", host="localhost")
    doc = st.export_servers()
    assert doc["format"] == SERVERS_EXPORT_FORMAT and doc["version"] == 1
    assert [s["name"] for s in doc["servers"]] == ["Prod", "Dev"]
    assert all("id" not in s and "password" not in s and "created_at" not in s for s in doc["servers"])
    assert doc["servers"][0]["sslmode"] == "require"
    with_pw = st.export_servers([a["id"]], include_passwords=True)
    assert [s["name"] for s in with_pw["servers"]] == ["Prod"] and with_pw["servers"][0]["password"] == SAVED_PW


def test_export_errors(tmp_path):
    st = make_store(tmp_path)
    with pytest.raises(StoreError, match="no servers"):
        st.export_servers()
    add(st, "Prod")
    with pytest.raises(StoreError, match="no longer exists"):
        st.export_servers(["nope"])
    with pytest.raises(StoreError, match="list"):
        st.export_servers("all")


def test_round_trip_to_another_pc_with_passwords(tmp_path):
    src = make_store(tmp_path, "old")
    add(src, "Prod", password=SAVED_PW, mdb="appdb")
    add(src, "Dev", host="localhost", remember=False)
    dst = make_store(tmp_path, "new")
    r = dst.import_servers(src.export_servers(include_passwords=True))
    assert r == {"added": ["Prod", "Dev"], "skipped": []}
    prod = next(s for s in dst.list_servers() if s["name"] == "Prod")
    assert dst.get_server(prod["id"], with_password=True)["password"] == SAVED_PW
    assert prod["maintenance_db"] == "appdb" and prod["sslmode"] == "require"
    dev = next(s for s in dst.list_servers() if s["name"] == "Dev")
    assert dev["has_password"] is False and dev["remember_password"] is False


def test_import_skips_servers_already_saved_and_renames_name_clashes(tmp_path):
    src = make_store(tmp_path, "old")
    add(src, "Prod")
    add(src, "Dev", host="localhost")
    dst = make_store(tmp_path, "new")
    add(dst, "Prod")                               # same connection: skipped
    add(dst, "Dev", host="other-host")             # same name, other server: imported as "Dev (2)"
    r = dst.import_servers(src.export_servers())
    assert r == {"added": ["Dev (2)"], "skipped": ["Prod"]}
    assert sorted(s["name"] for s in dst.list_servers()) == ["Dev", "Dev (2)", "Prod"]
    assert dst.import_servers(src.export_servers()) == {"added": [], "skipped": ["Prod", "Dev"]}


def test_import_rejects_bad_files_without_writing_anything(tmp_path):
    st = make_store(tmp_path)
    for bad in (None, [], {"servers": []}, {"format": "other", "version": 1, "servers": []}):
        with pytest.raises(StoreError, match="not a PG Backup Studio server export"):
            st.import_servers(bad)
    with pytest.raises(StoreError, match="newer version"):
        st.import_servers({"format": SERVERS_EXPORT_FORMAT, "version": 2, "servers": []})
    doc = {"format": SERVERS_EXPORT_FORMAT, "version": 1,
           "servers": [{"name": "Good", "host": "h", "user": "u"}, {"name": "Bad", "host": "h2", "user": "u", "port": 99999}]}
    with pytest.raises(StoreError, match="'Bad' in the file: The port"):
        st.import_servers(doc)
    assert st.list_servers() == []


def test_api_export_and_import(tmp_path):
    src = make_store(tmp_path, "old")
    add(src, "Prod", password=SAVED_PW)
    c_src = create_app(src, JobManager(src), ConsoleManager()).test_client()
    r = c_src.post("/api/servers/export", json={"include_passwords": True}, headers=H)
    assert r.status_code == 200 and r.get_json()["servers"][0]["password"] == SAVED_PW
    assert c_src.post("/api/servers/export", json={"include_passwords": "yes"}, headers=H).status_code == 400

    dst = make_store(tmp_path, "new")
    c_dst = create_app(dst, JobManager(dst), ConsoleManager()).test_client()
    r2 = c_dst.post("/api/servers/import", json={"document": r.get_json()}, headers=H)
    assert r2.status_code == 200 and r2.get_json() == {"added": ["Prod"], "skipped": []}
    listed = c_dst.get("/api/servers", headers=H).get_json()
    assert [s["name"] for s in listed] == ["Prod"] and listed[0]["has_password"] and "password" not in listed[0]
    assert c_dst.post("/api/servers/import", json={"document": {"x": 1}}, headers=H).status_code == 400
