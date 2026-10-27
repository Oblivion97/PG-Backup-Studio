"""Step 3: store.py (no database needed). ARCHITECTURE 10.9 items 1-5."""
import json
import os
import subprocess
import threading

import pytest

import store as store_mod
from engine.backup import BackupOptions
from store import Store, StoreError

SERVER = {"name": "Local 18", "host": "localhost", "port": 5432, "user": "postgres", "password": "secret"}


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path / "data")


def read_json(st: Store, name: str):
    return json.loads((st.dir / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. survive restart, passwords

def test_everything_survives_a_new_store(st):
    srv = st.save_server(dict(SERVER))
    st.save_settings({"output_dir": "D:/bk", "large_table_mb": 12.5})
    prof = BackupOptions(mode="schema", schemas=["app"]).to_profile()
    st.save_profile(srv["id"], "shop", prof)
    st.add_history({"id": "abc123abc123", "kind": "backup", "status": "success"})
    again = Store(st.dir)
    assert again.list_servers() == st.list_servers()
    assert again.get_server(srv["id"], with_password=True)["password"] == "secret"
    assert again.get_settings()["output_dir"] == "D:/bk" and again.get_settings()["large_table_mb"] == 12.5
    assert again.get_profile(srv["id"], "shop") == prof
    assert BackupOptions.from_dict(again.get_profile(srv["id"], "shop")["options"]).to_profile(
        saved_at=None)["options"] == prof["options"]
    assert again.get_history("abc123abc123")["status"] == "success"
    assert read_json(st, "servers.json")["version"] == 1


def test_public_views_never_contain_the_password(st):
    srv = st.save_server(dict(SERVER))
    assert "password" not in srv and srv["has_password"] is True
    assert all("password" not in s for s in st.list_servers())
    assert "password" not in st.get_server(srv["id"])
    assert len(srv["id"]) == 12 and int(srv["id"], 16) >= 0


def test_password_keep_replace_clear(st):
    sid = st.save_server(dict(SERVER))["id"]
    st.save_server({"id": sid, "host": "127.0.0.1"})                    # absent -> kept
    assert st.get_server(sid, with_password=True)["password"] == "secret"
    st.save_server({"id": sid, "password": None})                      # None -> kept
    assert st.get_server(sid, with_password=True)["password"] == "secret"
    st.save_server({"id": sid, "password": "new"})                     # replaced
    assert st.get_server(sid, with_password=True)["password"] == "new"
    rec = st.save_server({"id": sid, "remember_password": False})      # cleared
    assert rec["has_password"] is False and st.get_server(sid, with_password=True)["password"] == ""
    assert st.get_server(sid)["host"] == "127.0.0.1"


@pytest.mark.parametrize("change, words", [
    ({"name": " "}, "name"), ({"host": ""}, "host"), ({"port": 0}, "port"), ({"port": 70000}, "port"),
    ({"port": True}, "port"), ({"port": "x"}, "port"), ({"user": ""}, "user"), ({"sslmode": "maybe"}, "SSL"),
    ({"maintenance_db": 5}, "maintenance"), ({"remember_password": "yes"}, "Remember"), ({"password": 5}, "password"),
])
def test_server_validation(st, change, words):
    with pytest.raises(StoreError, match=words):
        st.save_server({**SERVER, **change})
    assert st.list_servers() == []


def test_server_defaults_and_duplicate_name(st):
    rec = st.save_server({"name": "a", "host": "h", "user": "u", "port": "5433", "maintenance_db": " "})
    assert (rec["port"], rec["sslmode"], rec["maintenance_db"], rec["remember_password"]) == (5433, "prefer", None, True)
    with pytest.raises(StoreError, match="already exists"):
        st.save_server({"name": "A", "host": "h", "user": "u"})
    st.save_server({"id": rec["id"], "name": "A"})                     # renaming itself is fine
    with pytest.raises(StoreError) as e:
        st.save_server({"id": "nope", "name": "b", "host": "h", "user": "u"})
    assert e.value.code == "not_found"


def test_delete_server_removes_its_profiles(st):
    a, b = st.save_server(dict(SERVER)), st.save_server({**SERVER, "name": "other"})
    prof = BackupOptions().to_profile()
    st.save_profile(a["id"], "x", prof)
    st.save_profile(b["id"], "x", prof)
    assert st.delete_server(a["id"]) is True and st.delete_server(a["id"]) is False
    assert st.get_profile(a["id"], "x") is None and st.get_profile(b["id"], "x") == prof
    assert [s["id"] for s in st.list_servers()] == [b["id"]]


# ---------------------------------------------------------------- 2. atomic + concurrent

def test_failed_replace_keeps_the_old_file(st, monkeypatch):
    st.save_server(dict(SERVER))
    before = (st.dir / "servers.json").read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        st.save_server({**SERVER, "name": "two"})
    assert (st.dir / "servers.json").read_bytes() == before
    assert not list(st.dir.glob(".*.tmp"))


def test_replace_retries_permission_errors(st, monkeypatch):
    real, calls = os.replace, []

    def flaky(a, b):
        calls.append(1)
        if len(calls) <= 3:
            raise PermissionError("held by antivirus")
        return real(a, b)
    monkeypatch.setattr(store_mod.os, "replace", flaky)
    st.save_server(dict(SERVER))
    assert len(calls) == 4 and len(st.list_servers()) == 1


def test_concurrent_history_writes_lose_nothing(st):
    st.save_settings({"history_limit": 5000})

    def worker(n):
        for i in range(50):
            st.add_history({"kind": "backup", "status": "success", "n": f"{n}-{i}"})
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    doc = read_json(st, "history.json")
    assert len(doc["entries"]) == 1000 and len({e["id"] for e in doc["entries"]}) == 1000


def test_two_store_objects_on_one_folder_share_the_lock(st):
    other = Store(st.dir)
    assert other.lock is st.lock
    other.add_history({"kind": "backup"})
    assert len(st.list_history()) == 1


def test_stale_temp_files_from_other_processes_are_removed(st):
    stale = st.dir / ".servers.json.999999.1.tmp"
    stale.write_bytes(b"x")
    Store(st.dir)
    assert not stale.exists()


# ---------------------------------------------------------------- 3. corrupt files and versions

@pytest.mark.parametrize("content", [b"\x00garbage{", b"[]", b'{"version": 1, "servers": {}}',
                                     '{"servers": [{"name": "caf\u00e9"}]}'.encode("latin-1"),
                                     b'{"version": "1", "servers": []}'])
def test_corrupt_file_is_moved_aside(st, content):
    (st.dir / "servers.json").write_bytes(content)
    assert st.list_servers() == []
    assert len(list(st.dir.glob("servers.corrupt-*.json"))) == 1
    assert st.warnings and "servers.json" in st.warnings[0]
    st.save_server(dict(SERVER))                                       # usable again
    assert len(Store(st.dir).list_servers()) == 1


def test_bad_items_are_skipped_but_the_file_is_kept(st):
    (st.dir / "servers.json").write_text(json.dumps({"version": 1, "servers": [
        {"name": "no id"}, "text", {**SERVER, "id": "aaaaaaaaaaaa"}]}), encoding="utf-8")
    assert [s["id"] for s in st.list_servers()] == ["aaaaaaaaaaaa"]
    assert st.warnings and not list(st.dir.glob("*.corrupt-*"))


def test_bom_is_accepted(st):
    (st.dir / "settings.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"version": 1, "settings": {"history_limit": 60}}).encode())
    assert st.get_settings()["history_limit"] == 60 and not st.warnings


def test_newer_version_is_read_but_never_written(st):
    (st.dir / "servers.json").write_text(json.dumps(
        {"version": 99, "servers": [{**SERVER, "id": "bbbbbbbbbbbb"}], "future": 1}), encoding="utf-8")
    assert st.list_servers()[0]["id"] == "bbbbbbbbbbbb"
    with pytest.raises(StoreError, match="newer") as e:
        st.save_server({**SERVER, "name": "x"})
    assert e.value.code == "readonly"
    assert read_json(st, "servers.json")["version"] == 99


# ---------------------------------------------------------------- 4. settings, history, profiles

def test_settings_defaults_invalid_values_and_unknown_keys(st):
    assert st.get_settings() == store_mod.DEFAULT_SETTINGS
    (st.dir / "settings.json").write_text(json.dumps({"version": 1, "settings": {
        "large_table_mb": -1, "large_table_rows": "x", "history_limit": 10, "output_dir": "", "junk": 1}}),
        encoding="utf-8")
    assert st.get_settings() == store_mod.DEFAULT_SETTINGS and len(st.warnings) == 4
    for bad in ({"junk": 1}, {"history_limit": 49}, {"history_limit": 100001}, {"history_limit": 60.0},
                {"large_table_mb": float("nan")}, {"large_table_mb": float("inf")}, {"large_table_rows": 0},
                {"output_dir": "  "}, {"large_table_mb": True}):
        with pytest.raises(StoreError):
            st.save_settings(bad)
    out = st.save_settings({"history_limit": 50, "large_table_rows": 10.0})
    assert out["history_limit"] == 50 and out["large_table_rows"] == 10
    assert "junk" not in read_json(st, "settings.json")["settings"]


def test_history_trim_keeps_running_entries_and_files(st, tmp_path):
    st.save_settings({"history_limit": 50})
    f = tmp_path / "keep.sql"
    f.write_text("x")
    st.add_history({"id": "run000000001", "kind": "backup", "status": "running"})
    st.add_history({"id": "old000000001", "kind": "backup", "status": "success", "file": str(f)})
    for i in range(60):
        st.add_history({"kind": "backup", "status": "success"})
    ids = [e["id"] for e in st.list_history()]
    assert len(ids) == 50 and "run000000001" in ids and "old000000001" not in ids
    assert f.exists()


def test_history_order_update_delete(st, tmp_path):
    a = st.add_history({"kind": "backup", "status": "running"})
    b = st.add_history({"kind": "restore", "status": "running"})
    assert [e["id"] for e in st.list_history()] == [b["id"], a["id"]]
    assert st.update_history(a["id"], {"status": "success", "id": "changed"})["status"] == "success"
    assert st.update_history("missing", {}) is None
    assert st.get_history(a["id"])["id"] == a["id"]
    f = tmp_path / "x.sql.gz"
    f.write_bytes(b"x")
    st.update_history(a["id"], {"file": str(f)})
    assert st.delete_history(a["id"], delete_file=True)["file_deleted"] is True and not f.exists()
    assert st.delete_history(a["id"]) is None


def test_delete_history_only_deletes_backup_sql_files(st, tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("x")
    src = tmp_path / "src.sql"
    src.write_text("x")
    e1 = st.add_history({"kind": "backup", "file": str(other)})
    e2 = st.add_history({"kind": "restore", "file": str(src)})
    e3 = st.add_history({"kind": "backup", "file": str(tmp_path / "gone.sql")})
    assert st.delete_history(e1["id"], delete_file=True)["file_deleted"] is False and other.exists()
    assert st.delete_history(e2["id"], delete_file=True)["file_deleted"] is False and src.exists()
    assert st.delete_history(e3["id"], delete_file=True)["file_deleted"] is False


def test_mark_interrupted_deletes_only_the_recorded_part_file(st, tmp_path):
    part, neighbour = tmp_path / "x.sql.part", tmp_path / "y.sql.part"
    part.write_bytes(b"x")
    neighbour.write_bytes(b"x")
    e = st.add_history({"kind": "backup", "status": "running", "part_file": str(part)})
    st.add_history({"kind": "backup", "status": "success"})
    changed = st.mark_interrupted()
    assert [c["id"] for c in changed] == [e["id"]]
    assert st.get_history(e["id"])["status"] == "interrupted" and st.get_history(e["id"])["part_file"] is None
    assert not part.exists() and neighbour.exists()
    assert st.mark_interrupted() == []


def test_profiles(st):
    with pytest.raises(StoreError):
        st.save_profile("s", "db", {"options": {}})
    prof = BackupOptions(tables=[{"schema": "app", "table": "t", "data": "schema"}]).to_profile()
    st.save_profile("s", "db.with.dots", prof)
    assert st.get_profile("s", "db.with.dots") == prof and st.get_profile("s", "db") is None
    assert st.delete_profile("s", "db.with.dots") is True and st.delete_profile("s", "db.with.dots") is False
    assert read_json(st, "backup_profiles.json")["profiles"] == {}


# ---------------------------------------------------------------- 5. permissions

@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_posix_permissions(st):
    st.save_server(dict(SERVER))
    assert (st.dir.stat().st_mode & 0o777) == 0o700
    assert ((st.dir / "servers.json").stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL")
def test_windows_acl_only_user_and_system(st):
    st.save_server(dict(SERVER))
    for target in (st.dir, st.dir / "servers.json"):
        out = subprocess.run(["icacls", str(target)], capture_output=True, text=True).stdout
        aces = [line.replace(str(target), "").strip() for line in out.splitlines()]
        aces = [a for a in aces if a and not a.startswith("Successfully")]
        user = os.environ["USERNAME"].lower()
        assert aces and all(user in a.lower() or "SYSTEM" in a for a in aces), out
        assert not any(w in a for a in aces for w in ("BUILTIN", "Administrators", "Everyone", "Authenticated")), out
