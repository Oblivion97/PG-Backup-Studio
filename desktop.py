"""Desktop app (Step 9, decision 010): the Flask app in a native window.

The server runs in a background thread on a free 127.0.0.1 port; the window (pywebview, Edge WebView2 on
Windows) opens `/launch/<token>`, which hands the page a per-launch secret cookie that every API call must
carry. Closing the window asks first when a backup / restore is running or a query tab has an open
transaction, then stops the jobs, closes every database connection and quits. One instance at a time.

If WebView2 cannot start (the window would stay blank), the window is closed and the same launch URL opens in the
default browser; a small message box keeps the app running until the user clicks OK to quit.

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
        self.in_browser = False   # True after the browser fallback: there is no window to show a dialog on

    def pick_folder(self, start: str) -> str | None:
        """Native folder picker; None when cancelled."""
        if self.in_browser:
            raise web.ApiError("PG Backup Studio is running in your browser, which cannot show a folder dialog; "
                               "type the path instead.", "invalid")
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


WEBVIEW2_FAILED = "WebView2 initialization failed"   # pywebview logs this (edgechromium.py) and leaves the window blank


class WebViewFailure(logging.Handler):
    """Notices when the window's browser engine (Edge WebView2) cannot start.

    pywebview has no failure callback: it logs an error and keeps showing an empty window. This handler watches the
    `pywebview` logger for that error and calls `on_fail` once (on pywebview's UI thread).
    """

    def __init__(self, on_fail):
        super().__init__(level=logging.ERROR)
        self.on_fail, self.failed = on_fail, False

    def emit(self, record: logging.LogRecord) -> None:
        if self.failed or WEBVIEW2_FAILED not in record.getMessage():
            return
        self.failed = True
        self.on_fail()


def message_box(text: str, flags: int) -> int:
    """A native message box (Windows); returns the button pressed. Elsewhere: print and answer OK/Yes."""
    if os.name == "nt":
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | 0x10000)   # MB_SETFOREGROUND
    print(text, file=sys.stderr)
    return 1


MB_OK_INFO, MB_YESNO_WARN, IDYES = 0x40, 0x34, 6


def run_in_browser(url: str, manager, consoles) -> None:
    """Browser fallback: open the app in the default browser and keep the server up until the user quits.

    The launch URL sets the per-launch secret cookie in that browser, exactly as it would in the window.
    """
    import webbrowser
    log.warning("window could not start; opening the app in the default browser instead")
    webbrowser.open(url)
    home = url.split("/launch/")[0]
    while True:
        message_box(f"{APP_NAME} could not open its own window because Microsoft Edge WebView2 failed to start, "
                    f"so it has opened in your web browser instead.\n\n"
                    f"Address (works in that browser): {home}\n\n"
                    f"Leave this box open while you work. Click OK to quit {APP_NAME}.", MB_OK_INFO)
        reason = close_question(manager, consoles)
        if not reason or message_box(f"Quit {APP_NAME}?\n\n{reason}", MB_YESNO_WARN) == IDYES:
            return


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

    url = f"http://127.0.0.1:{port}/launch/{token}"
    window = webview.create_window(APP_NAME, url, width=WINDOW_SIZE[0],
                                   height=WINDOW_SIZE[1], min_size=MIN_SIZE, text_select=True, zoomable=True,
                                   background_color="#ffffff")
    bridge.window = window

    def on_closing():
        reason = close_question(manager, consoles)
        if reason and not window.create_confirmation_dialog(f"Close {APP_NAME}?", reason):
            return False   # stay open
        return True

    window.events.closing += on_closing

    def close_blank_window():
        # called on the UI thread from inside pywebview's callback: close from another thread, skipping the question
        window.events.closing -= on_closing
        threading.Thread(target=window.destroy, name="close-blank-window", daemon=True).start()

    watch = WebViewFailure(close_blank_window)
    logging.getLogger("pywebview").addHandler(watch)
    try:
        try:
            # private_mode off + a storage folder: the page's own settings (theme, query tabs, history) survive restarts
            webview.start(private_mode=False, storage_path=os.path.join(str(store.dir), "webview"))
        except Exception:
            log.exception("the window could not be created")
            watch.failed = True
        if watch.failed:
            bridge.window, bridge.in_browser = None, True
            run_in_browser(url, manager, consoles)
    finally:
        log.info("window closed; stopping")
        manager.shutdown()
        consoles.close_all()
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
