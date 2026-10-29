"""Desktop app (Step 9, decision 010): the Flask app in a native window.

The server runs in a background thread on a free 127.0.0.1 port; the window (pywebview, Edge WebView2 on
Windows) opens `/launch/<token>`, which hands the page a per-launch secret cookie that every API call must
carry. Closing the window asks first when a backup / restore is running or a query tab has an open
transaction, then stops the jobs, closes every database connection and quits. One instance at a time.

Run from source with `python desktop.py`; the installed app is this file frozen by PyInstaller
(`pg_backup_studio.spec`), which keeps its data in %APPDATA%\\PG Backup Studio (store.default_data_dir).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import secrets
import sys
import threading

import webview
from werkzeug.serving import make_server

import app as web
from store import APP_NAME, Store

log = logging.getLogger("desktop")

MUTEX_NAME = "Local\\PGBackupStudio.SingleInstance"   # also in installer/pg_backup_studio.iss (AppMutex)
WINDOW_SIZE = (1440, 920)
MIN_SIZE = (980, 640)


class Bridge:
    """What the Flask app may ask of the window (passed to create_app as `desktop`)."""

    def __init__(self):
        self.window = None

    def pick_folder(self, start: str) -> str | None:
        """Native folder picker; None when cancelled."""
        if self.window is None:
            return None
        chosen = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        return chosen[0] if chosen else None


def single_instance() -> object | None:
    """A named mutex held for the life of the process; None when another instance already holds it."""
    if os.name != "nt":
        return object()
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return handle


def already_running_message() -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{APP_NAME} is already open.", APP_NAME, 0x40)   # MB_ICONINFORMATION
    else:
        print(f"{APP_NAME} is already open.", file=sys.stderr)


def setup_logging(data_dir) -> None:
    """The window has no console: log to <data>/logs/desktop.log (rotated), and to stderr when there is one."""
    folder = os.path.join(data_dir, "logs")
    os.makedirs(folder, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        os.path.join(folder, "desktop.log"), maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def close_question(manager, consoles) -> str | None:
    """Why closing needs a confirmation, or None."""
    running = [j for j in manager.list_jobs() if j["status"] == "running"]
    open_tx = consoles.open_transactions()
    parts = []
    if running:
        parts.append(f"{len(running)} backup/restore job{'s are' if len(running) > 1 else ' is'} still running "
                     "and will be cancelled.")
    if open_tx:
        parts.append(f"{open_tx} query tab{'s have' if open_tx > 1 else ' has'} an open transaction that will be "
                     "rolled back.")
    return " ".join(parts) or None


def main() -> int:
    mutex = single_instance()
    if mutex is None:
        already_running_message()
        return 0
    store = Store()
    setup_logging(str(store.dir))
    bridge = Bridge()
    token = secrets.token_urlsafe(32)
    flask_app = web.create_app(store, desktop=bridge, launch_token=token)
    manager = flask_app.config["PGBS_MANAGER"]
    consoles = flask_app.config["PGBS_CONSOLES"]
    server = make_server("127.0.0.1", 0, flask_app, threaded=True)
    port = server.server_port
    threading.Thread(target=server.serve_forever, name="flask", daemon=True).start()
    log.info("%s %s on 127.0.0.1:%s, data in %s", APP_NAME, web.APP_VERSION, port, store.dir)

    window = webview.create_window(APP_NAME, f"http://127.0.0.1:{port}/launch/{token}", width=WINDOW_SIZE[0],
                                   height=WINDOW_SIZE[1], min_size=MIN_SIZE, text_select=True, zoomable=True,
                                   background_color="#ffffff")
    bridge.window = window

    def on_closing():
        reason = close_question(manager, consoles)
        if reason and not window.create_confirmation_dialog(f"Close {APP_NAME}?", reason):
            return False   # stay open
        return True

    window.events.closing += on_closing
    try:
        # private_mode off + a storage folder: the page's own settings (theme, query tabs, history) survive restarts
        webview.start(private_mode=False, storage_path=os.path.join(str(store.dir), "webview"))
    finally:
        log.info("window closed; stopping")
        manager.shutdown()
        consoles.close_all()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
