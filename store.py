"""JSON files in ./data with atomic write + lock (Step 3).

Four files (ARCHITECTURE 10.2): `servers.json`, `settings.json`, `history.json` and
`backup_profiles.json`. Nothing is cached: every change is "lock → read file → change → write a
temp file → fsync → os.replace", so a crash leaves either the old or the new file and two `Store`
objects on one folder stay consistent. A file that cannot be read is renamed
`<name>.corrupt-<time>.json` and the app continues with it empty (see `warnings`). Passwords are
plain text as the user approved; the folder is restricted to the current user (decision 005 §2).
"""

from __future__ import annotations

import getpass
import json
import logging
import math
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
CURRENT = 1
# {file name: {from_version: function(doc) -> doc at from_version + 1}}; none needed yet.
MIGRATIONS: dict[str, dict[int, object]] = {}
SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_SETTINGS = {"output_dir": "~/pg_backups", "large_table_mb": 500, "large_table_rows": 1_000_000,
                    "history_limit": 1000}
HISTORY_LIMIT_RANGE = (50, 100_000)
REPLACE_RETRIES = 10
REPLACE_RETRY_DELAY = 0.05

SERVERS, SETTINGS, HISTORY, PROFILES = "servers.json", "settings.json", "history.json", "backup_profiles.json"
_CONTAINERS = {SERVERS: ("servers", list), SETTINGS: ("settings", dict), HISTORY: ("entries", list),
               PROFILES: ("profiles", dict)}
_SERVER_FIELDS = ("name", "host", "port", "user", "password", "remember_password", "sslmode", "maintenance_db")

_dir_locks: dict[str, threading.RLock] = {}
_dir_locks_guard = threading.Lock()
_restricted: set[str] = set()


class StoreError(ValueError):
    """A change was refused; the message is a sentence for the user. `code`: invalid | not_found | readonly."""

    def __init__(self, message: str, code: str = "invalid"):
        self.code = code
        super().__init__(message)


def now_iso() -> str:
    """Local time with offset, seconds precision (the format of every stored time)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id(taken=()) -> str:
    """12 hex characters, re-drawn when already in `taken`."""
    while True:
        value = secrets.token_hex(6)
        if value not in taken:
            return value


def _lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _dir_locks_guard:
        return _dir_locks.setdefault(key, threading.RLock())


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _delete_function_folder(path: str) -> bool:
    """Delete the `.sql` files of a functions backup, then the folder when nothing else is left in it."""
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            if name.lower().endswith(".sql") and os.path.isfile(full):
                os.remove(full)
        if not os.listdir(path):
            os.rmdir(path)
    except OSError as e:
        raise StoreError(f"Could not delete {path}: {e.strerror or e}") from e
    return True


class Store:
    """All four JSON files of one data folder; thread-safe (one lock per folder in this process)."""

    def __init__(self, data_dir: str | os.PathLike | None = None):
        self.dir = Path(data_dir or os.environ.get("PGBS_DATA_DIR") or APP_DIR / "data").expanduser().resolve()
        self.lock = _lock_for(self.dir)
        self.warnings: list[str] = []
        self.dir.mkdir(parents=True, exist_ok=True)
        self._restrict_folder()
        self._remove_stale_temp_files()

    # ------------------------------------------------------------ files

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            log.warning(message)

    def _restrict_folder(self) -> None:
        """Only the current user (and SYSTEM on Windows) may open the folder; failure is only a warning."""
        key = os.path.normcase(str(self.dir))
        if key in _restricted:
            return
        _restricted.add(key)
        try:
            if os.name != "nt":
                os.chmod(self.dir, 0o700)
                return
            domain, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME") or getpass.getuser()
            account = f"{domain}\\{user}" if domain else user
            done = subprocess.run(
                ["icacls", str(self.dir), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F",
                 "*S-1-5-18:(OI)(CI)F"], capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode != 0:
                raise OSError((done.stderr or done.stdout).strip() or f"icacls exit code {done.returncode}")
        except (OSError, subprocess.SubprocessError) as e:
            self._warn(f"Could not restrict access to {self.dir} to the current user: {e}")

    def _remove_stale_temp_files(self) -> None:
        """Temp files left by a crashed process (this process's own may be in use by another Store)."""
        mine = f".{os.getpid()}."
        for p in self.dir.glob(".*.tmp"):
            if mine not in p.name:
                try:
                    p.unlink()
                except OSError:
                    pass

    def _read(self, name: str) -> dict:
        """The file as a dict with a valid container; corrupt files are moved aside (never raises)."""
        key, kind = _CONTAINERS[name]
        path = self.dir / name
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return {"version": CURRENT, key: kind()}
        try:
            doc = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(doc, dict):
                raise ValueError("the top level is not an object")
            version = doc.get("version", 1)
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValueError("bad version")
            doc.setdefault(key, kind())
            if not isinstance(doc[key], kind):
                raise ValueError(f"{key!r} has the wrong type")
        except (ValueError, UnicodeDecodeError) as e:
            self._quarantine(path, e)
            return {"version": CURRENT, key: kind()}
        for v in range(version, CURRENT):
            doc = MIGRATIONS[name][v](doc)
            doc["version"] = v + 1
        return doc

    def _quarantine(self, path: Path, reason: Exception) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target, n = path.with_name(f"{path.stem}.corrupt-{stamp}.json"), 2
        while target.exists():
            target, n = path.with_name(f"{path.stem}.corrupt-{stamp}-{n}.json"), n + 1
        try:
            os.replace(path, target)
            where = f"saved as {target.name}"
        except OSError as e:
            where = f"could not be moved aside ({e})"
        self._warn(f"{path.name} was unreadable ({reason}); {where} and started empty.")

    def _write(self, name: str, doc: dict) -> None:
        """Atomic replace; refuses to overwrite a file written by a newer version."""
        if doc.get("version", CURRENT) > CURRENT:
            raise StoreError(f"{name} was written by a newer PG Backup Studio; it is read-only for this version.",
                             "readonly")
        doc["version"] = CURRENT
        data = json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")
        path = self.dir / name
        tmp = self.dir / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(REPLACE_RETRIES):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    # Antivirus or the search indexer can hold the file for a moment on Windows.
                    if attempt == REPLACE_RETRIES - 1:
                        raise
                    time.sleep(REPLACE_RETRY_DELAY)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------ servers

    @staticmethod
    def public_server(rec: dict) -> dict:
        """The record without its password, plus `has_password` (what the browser may see)."""
        out = {k: v for k, v in rec.items() if k != "password"}
        out["has_password"] = bool(rec.get("password"))
        return out

    def _servers(self, doc: dict) -> list[dict]:
        good = []
        for item in doc["servers"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                good.append(item)
            else:
                self._warn("servers.json contains an entry without an id; it was skipped.")
        return good

    def list_servers(self) -> list[dict]:
        with self.lock:
            return [self.public_server(s) for s in self._servers(self._read(SERVERS))]

    def get_server(self, server_id: str, *, with_password: bool = False) -> dict | None:
        with self.lock:
            for s in self._servers(self._read(SERVERS)):
                if s["id"] == server_id:
                    return dict(s) if with_password else self.public_server(s)
        return None

    def save_server(self, data: dict) -> dict:
        """Create (no `id`) or update a server; returns the public view. Raises `StoreError`."""
        if not isinstance(data, dict):
            raise StoreError("Server details must be an object.")
        with self.lock:
            doc = self._read(SERVERS)
            servers = self._servers(doc)
            server_id = data.get("id")
            old = None
            if server_id is not None:
                old = next((s for s in servers if s["id"] == server_id), None)
                if old is None:
                    raise StoreError("This server no longer exists.", "not_found")
            rec = self._clean_server(data, old, [s for s in servers if s is not old])
            stamp = now_iso()
            if old is None:
                rec.update(id=new_id({s["id"] for s in servers}), created_at=stamp, updated_at=stamp)
                doc["servers"].append(rec)
            else:
                rec.update(id=old["id"], created_at=old.get("created_at", stamp), updated_at=stamp)
                doc["servers"][doc["servers"].index(old)] = rec
            self._write(SERVERS, doc)
            return self.public_server(rec)

    @staticmethod
    def _clean_server(data: dict, old: dict | None, others: list[dict]) -> dict:
        merged = {k: (old or {}).get(k) for k in _SERVER_FIELDS}
        merged.update({k: data[k] for k in _SERVER_FIELDS if k in data and k != "password"})
        name = merged["name"].strip() if isinstance(merged["name"], str) else ""
        if not name:
            raise StoreError("Enter a name for the server.")
        if any(str(s.get("name", "")).strip().casefold() == name.casefold() for s in others):
            raise StoreError(f"A server named {name!r} already exists; choose another name.")
        host = merged["host"].strip() if isinstance(merged["host"], str) else ""
        if not host:
            raise StoreError("Enter the server host (for example localhost).")
        port = merged["port"] if merged["port"] is not None else 5432
        if isinstance(port, str) and port.strip().isdigit():
            port = int(port.strip())
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise StoreError("The port must be a whole number between 1 and 65535.")
        user = merged["user"].strip() if isinstance(merged["user"], str) else ""
        if not user:
            raise StoreError("Enter the user name to log in with.")
        sslmode = merged["sslmode"] or "prefer"
        if sslmode not in SSLMODES:
            raise StoreError(f"Unknown SSL mode {sslmode!r}; use one of: {', '.join(SSLMODES)}.")
        maintenance_db = merged["maintenance_db"]
        if isinstance(maintenance_db, str):
            maintenance_db = maintenance_db.strip() or None
        if maintenance_db is not None and not isinstance(maintenance_db, str):
            raise StoreError("The maintenance database must be a name or empty.")
        remember = merged["remember_password"]
        remember = True if remember is None else remember
        if not isinstance(remember, bool):
            raise StoreError("'Remember password' must be true or false.")
        password = data.get("password")
        if password is None:
            password = (old or {}).get("password") or ""
        elif not isinstance(password, str):
            raise StoreError("The password must be text.")
        return {"name": name, "host": host, "port": port, "user": user,
                "password": password if remember else "", "remember_password": remember,
                "sslmode": sslmode, "maintenance_db": maintenance_db}

    def delete_server(self, server_id: str) -> bool:
        """Remove a server and its backup profiles; history keeps its name snapshot."""
        with self.lock:
            doc = self._read(SERVERS)
            keep = [s for s in doc["servers"] if not (isinstance(s, dict) and s.get("id") == server_id)]
            if len(keep) == len(doc["servers"]):
                return False
            doc["servers"] = keep
            self._write(SERVERS, doc)
            profiles = self._read(PROFILES)
            if profiles["profiles"].pop(server_id, None) is not None:
                self._write(PROFILES, profiles)
            return True

    # ------------------------------------------------------------ settings

    def get_settings(self) -> dict:
        """Defaults merged with the file; invalid stored values fall back to the default (with a warning)."""
        with self.lock:
            stored = self._read(SETTINGS)["settings"]
        out = dict(DEFAULT_SETTINGS)
        for key in DEFAULT_SETTINGS:
            if key in stored:
                try:
                    out[key] = self._clean_setting(key, stored[key])
                except StoreError:
                    self._warn(f"settings.json: invalid value for {key!r}; using the default "
                               f"{DEFAULT_SETTINGS[key]!r}.")
        return out

    def save_settings(self, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise StoreError("Settings must be an object.")
        unknown = sorted(set(changes) - set(DEFAULT_SETTINGS))
        if unknown:
            raise StoreError(f"Unknown setting(s): {', '.join(map(str, unknown))}.")
        clean = {k: self._clean_setting(k, v) for k, v in changes.items()}
        with self.lock:
            doc = self._read(SETTINGS)
            doc["settings"] = {k: v for k, v in doc["settings"].items() if k in DEFAULT_SETTINGS}
            doc["settings"].update(clean)
            self._write(SETTINGS, doc)
        return self.get_settings()

    @staticmethod
    def _clean_setting(key: str, value):
        if key == "output_dir":
            if not isinstance(value, str) or not value.strip():
                raise StoreError("The output folder must not be empty.")
            return value.strip()
        if key == "history_limit":
            low, high = HISTORY_LIMIT_RANGE
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise StoreError(f"The history limit must be a whole number between {low} and {high}.")
            return value
        if not _number(value) or value <= 0:
            raise StoreError(f"{key} must be a number greater than 0.")
        if key == "large_table_rows":
            return int(value)
        return value

    # ------------------------------------------------------------ history

    @staticmethod
    def _entries(doc: dict) -> list[dict]:
        return [e for e in doc["entries"] if isinstance(e, dict) and isinstance(e.get("id"), str)]

    def add_history(self, entry: dict) -> dict:
        """Append an entry (an `id` is drawn when missing), then trim the oldest finished entries."""
        if not isinstance(entry, dict):
            raise StoreError("A history entry must be an object.")
        limit = self.get_settings()["history_limit"]
        with self.lock:
            doc = self._read(HISTORY)
            rec = dict(entry)
            taken = {e["id"] for e in self._entries(doc)}
            if not isinstance(rec.get("id"), str) or rec["id"] in taken:
                rec["id"] = new_id(taken)
            doc["entries"].append(rec)
            extra = len(doc["entries"]) - limit
            if extra > 0:
                kept = []
                for e in doc["entries"]:
                    if extra > 0 and e is not rec and not (isinstance(e, dict) and e.get("status") == "running"):
                        extra -= 1
                    else:
                        kept.append(e)
                doc["entries"] = kept
            self._write(HISTORY, doc)
            return dict(rec)

    def update_history(self, entry_id: str, changes: dict) -> dict | None:
        with self.lock:
            doc = self._read(HISTORY)
            for e in self._entries(doc):
                if e["id"] == entry_id:
                    e.update({k: v for k, v in changes.items() if k != "id"})
                    self._write(HISTORY, doc)
                    return dict(e)
        return None

    def get_history(self, entry_id: str) -> dict | None:
        with self.lock:
            return next((dict(e) for e in self._entries(self._read(HISTORY)) if e["id"] == entry_id), None)

    def list_history(self) -> list[dict]:
        """Newest first."""
        with self.lock:
            return [dict(e) for e in reversed(self._entries(self._read(HISTORY)))]

    def delete_history(self, entry_id: str, *, delete_file: bool = False) -> dict | None:
        """Remove an entry; with `delete_file` also the backup file it points to (only `.sql`/`.sql.gz`),
        or the `.sql` files of a functions backup folder (the folder too once it is empty)."""
        with self.lock:
            doc = self._read(HISTORY)
            entry = next((e for e in self._entries(doc) if e["id"] == entry_id), None)
            if entry is None:
                return None
            deleted = False
            path = entry.get("file")
            if (delete_file and entry.get("kind") == "backup" and isinstance(path, str)
                    and path.lower().endswith((".sql", ".sql.gz")) and os.path.isfile(path)):
                try:
                    os.remove(path)
                    deleted = True
                except OSError as e:
                    raise StoreError(f"Could not delete {path}: {e.strerror or e}") from e
            elif (delete_file and entry.get("kind") == "backup" and entry.get("mode") == "functions"
                    and isinstance(path, str) and os.path.isdir(path)):
                deleted = _delete_function_folder(path)
            doc["entries"].remove(entry)
            self._write(HISTORY, doc)
            return {**entry, "file_deleted": deleted}

    def mark_interrupted(self) -> list[dict]:
        """Turn every `running` entry into `interrupted` and delete its recorded `.part` file."""
        with self.lock:
            doc = self._read(HISTORY)
            changed = []
            for e in self._entries(doc):
                if e.get("status") != "running":
                    continue
                part = e.get("part_file")
                if isinstance(part, str) and part.endswith(".part") and os.path.isfile(part):
                    try:
                        os.remove(part)
                    except OSError as err:
                        log.warning("could not remove %s: %s", part, err)
                e.update(status="interrupted", part_file=None,
                         error="The app ended while this job was running.")
                changed.append(dict(e))
            if changed:
                self._write(HISTORY, doc)
            return changed

    # ------------------------------------------------------------ backup profiles

    def get_profile(self, server_id: str, db: str) -> dict | None:
        with self.lock:
            prof = self._read(PROFILES)["profiles"].get(server_id)
        prof = prof.get(db) if isinstance(prof, dict) else None
        if isinstance(prof, dict) and isinstance(prof.get("saved_at"), str) and isinstance(prof.get("options"), dict):
            return prof
        return None

    def save_profile(self, server_id: str, db: str, profile: dict) -> dict:
        if not (isinstance(profile, dict) and isinstance(profile.get("saved_at"), str)
                and isinstance(profile.get("options"), dict)):
            raise StoreError("A backup profile needs 'saved_at' (text) and 'options' (an object).")
        with self.lock:
            doc = self._read(PROFILES)
            per_server = doc["profiles"].get(server_id)
            if not isinstance(per_server, dict):
                per_server = doc["profiles"][server_id] = {}
            per_server[db] = profile
            self._write(PROFILES, doc)
        return profile

    def delete_profile(self, server_id: str, db: str) -> bool:
        with self.lock:
            doc = self._read(PROFILES)
            per_server = doc["profiles"].get(server_id)
            if not isinstance(per_server, dict) or db not in per_server:
                return False
            del per_server[db]
            if not per_server:
                del doc["profiles"][server_id]
            self._write(PROFILES, doc)
            return True


_default: Store | None = None
_default_guard = threading.Lock()


def default_store() -> Store:
    """The app's Store on `PGBS_DATA_DIR` or `<app dir>/data`, created on first use."""
    global _default
    with _default_guard:
        if _default is None:
            _default = Store()
        return _default
