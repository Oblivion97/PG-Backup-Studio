"""Desktop app (Step 9, decision 010): the Flask app in a native window.

The server runs in a background thread on a free 127.0.0.1 port; the window (pywebview, Edge WebView2 on
Windows) opens `/launch/<token>`, which hands the page a per-launch secret cookie that every API call must
carry. Closing the window asks first when a backup / restore is running or a query tab has an open
transaction, then stops the jobs, closes every database connection and quits. One instance at a time.

If WebView2 cannot start (the window would stay blank), the window is closed and the same launch URL opens in the
default browser; a tray icon (Open / Quit) keeps the app running until Quit is chosen there, even if the tab is
closed. Launching the app again then reopens the tab.

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


MB_YESNO_WARN, IDYES = 0x34, 6
OPEN_EVENT_NAME = "Local\\PGBackupStudio.OpenInBrowser"   # a second launch sets it: the running app reopens the tab


def quit_allowed(manager, consoles) -> bool:
    """Tray → Quit: fine when nothing is running, otherwise ask (like closing the window)."""
    reason = close_question(manager, consoles)
    return not reason or message_box(f"Quit {APP_NAME}?\n\n{reason}", MB_YESNO_WARN) == IDYES


def signal_running_instance() -> bool:
    """Second launch: ask the app already running in browser mode to reopen its tab. False when it isn't in that mode."""
    if os.name != "nt":
        return False
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenEventW(0x0002, False, OPEN_EVENT_NAME)   # EVENT_MODIFY_STATE
    if not handle:
        return False
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    return True


def listen_for_second_launch(open_browser) -> None:
    """Browser mode: reopen the tab whenever another launch (Start menu, desktop icon) signals the open event."""
    if os.name != "nt":
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateEventW(None, False, False, OPEN_EVENT_NAME)   # auto-reset; held for the life of the app

    def wait():
        while kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:   # INFINITE → WAIT_OBJECT_0
            open_browser()

    threading.Thread(target=wait, name="second-launch", daemon=True).start()


def tray_icon_image():
    """The app icon for the tray: the exe's own icon when installed, installer/app.ico from source."""
    from System.Drawing import Icon, Size, SystemIcons
    try:
        if getattr(sys, "frozen", False):
            return Icon.ExtractAssociatedIcon(sys.executable)
        ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "installer", "app.ico")
        if os.path.exists(ico):
            return Icon(ico, Size(16, 16))
    except Exception:
        log.exception("could not load the tray icon image")
    return SystemIcons.Application


def run_tray(open_browser, may_quit) -> None:
    """Notification-area icon (WinForms NotifyIcon through pythonnet, which pywebview already loads).

    Click: open the app in the browser. Right-click: Open / Quit. Blocks until Quit is chosen and allowed; closing
    the browser tab does not stop the app.
    """
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    from System.Drawing import Font, FontStyle
    from System.Windows.Forms import (Application, ContextMenuStrip, MouseButtons, NotifyIcon, ToolStripMenuItem,
                                      ToolStripSeparator)

    icon = NotifyIcon()
    icon.Icon = tray_icon_image()
    icon.Text = f"{APP_NAME} (running in your browser)"   # Windows allows up to 63 characters

    def on_click(sender, args):
        if args.Button == MouseButtons.Left:
            open_browser()

    def on_quit(sender, args):
        if may_quit():
            icon.Visible = False
            Application.ExitThread()

    open_item = ToolStripMenuItem(f"Open {APP_NAME}")
    open_item.Font = Font(open_item.Font, FontStyle.Bold)
    open_item.Click += lambda sender, args: open_browser()
    quit_item = ToolStripMenuItem("Quit")
    quit_item.Click += on_quit
    menu = ContextMenuStrip()
    menu.Items.Add(open_item)
    menu.Items.Add(ToolStripSeparator())
    menu.Items.Add(quit_item)
    icon.ContextMenuStrip = menu
    icon.MouseClick += on_click
    icon.Visible = True
    try:
        Application.Run()
    finally:
        icon.Visible = False
        icon.Dispose()


def run_in_browser(url: str, manager, consoles) -> None:
    """Browser fallback: open the app in the default browser and keep the server up until Quit in the tray icon.

    The launch URL sets the per-launch secret cookie in that browser, exactly as it would in the window; it is used
    for every reopen too, so the app still works after the browser has been closed and its session cookie is gone.
    """
    import webbrowser
    log.warning("window could not start; running in the default browser with a tray icon instead")

    def open_browser():
        webbrowser.open(url)

    open_browser()
    listen_for_second_launch(open_browser)
    run_tray(open_browser, lambda: quit_allowed(manager, consoles))
    log.info("quit from the tray icon")


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
        if not signal_running_instance():   # running in the browser: it reopens its tab; in a window: say so
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

    # downloads (server export, grid CSV/JSON/... exports) get a Save dialog; pywebview cancels them by default
    webview.settings["ALLOW_DOWNLOADS"] = True
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
