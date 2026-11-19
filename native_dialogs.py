"""Native "choose a folder" / "choose a file" dialogs shown by the local server itself (decision 014).

The desktop window has its own dialogs (pywebview). When the app runs in a browser — browser mode of the desktop
app, or `python app.py` — the page cannot see the PC's folders, but the server runs on that same PC and can show the
operating system's dialog for it:

- Windows: the Explorer "Open" dialog (IFileOpenDialog) through ctypes, on its own STA thread, owned by a small
  top-most window so it appears above the browser;
- macOS: `osascript` (choose folder / choose file);
- Linux: `zenity` or `kdialog` when one is installed; otherwise `available()` is False and the page keeps the path
  field only.

`pick_folder(start)` / `pick_file(start)` return the chosen absolute path, or None when cancelled. One dialog at a
time: a second request while one is open raises DialogError.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

BACKUP_FILE_TYPES = (("Backup files (*.sql, *.sql.gz, *.gz)", "*.sql;*.sql.gz;*.gz"), ("All files (*.*)", "*.*"))
TIMEOUT_S = 30 * 60   # a dialog left open this long is given up (the request ends; the dialog closes with the app)

_busy = threading.Lock()


class DialogError(Exception):
    """The dialog could not be shown."""


def available() -> bool:
    if os.name == "nt":
        return True
    if sys.platform == "darwin":
        return shutil.which("osascript") is not None
    return _linux_tool() is not None


def pick_folder(start: str | None = None) -> str | None:
    return _run(True, start_dir(start))


def pick_file(start: str | None = None) -> str | None:
    return _run(False, start_dir(start))


def start_dir(start: str | None) -> str:
    path = os.path.expanduser(start or "")
    if path and os.path.isfile(path):
        path = os.path.dirname(path)
    while path and not os.path.isdir(path):   # the nearest folder that exists
        parent = os.path.dirname(path)
        path = "" if parent == path else parent
    return os.path.abspath(path) if path else os.path.expanduser("~")


def _run(folder: bool, start: str) -> str | None:
    if not _busy.acquire(blocking=False):
        raise DialogError("A file dialog is already open; finish or cancel it first (it may be behind this window).")
    try:
        if os.name == "nt":
            return _windows(folder, start)
        if sys.platform == "darwin":
            return _mac(folder, start)
        return _linux(folder, start)
    finally:
        _busy.release()


# ---------------------------------------------------------------- macOS / Linux

def _mac(folder: bool, start: str) -> str | None:
    what = "folder" if folder else "file"
    prompt = "Choose a folder" if folder else "Choose a backup file"
    script = (f'POSIX path of (choose {what} with prompt "{prompt}" '
              f'default location (POSIX file (system attribute "PGBS_START")))')
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=TIMEOUT_S, env={**os.environ, "PGBS_START": start})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DialogError(f"The dialog could not be shown: {e}") from e
    if r.returncode != 0:
        if "-128" in r.stderr or "cancel" in r.stderr.lower():
            return None
        raise DialogError(f"The dialog could not be shown: {r.stderr.strip() or r.returncode}")
    path = r.stdout.strip()
    return (path.rstrip("/") or "/") if path else None


def _linux_tool() -> str | None:
    for tool in ("zenity", "kdialog"):
        if shutil.which(tool):
            return tool
    return None


def _linux(folder: bool, start: str) -> str | None:
    tool = _linux_tool()
    if tool is None:
        raise DialogError("No file dialog is available (install zenity or kdialog); type the path instead.")
    if tool == "zenity":
        args = ["zenity", "--file-selection", "--filename", start.rstrip("/") + "/"]
        args += ["--directory", "--title", "Choose a folder"] if folder else \
            ["--title", "Choose a backup file", "--file-filter", "Backup files | *.sql *.sql.gz *.gz",
             "--file-filter", "All files | *"]
    else:
        args = ["kdialog", "--getexistingdirectory", start] if folder else \
            ["kdialog", "--getopenfilename", start, "*.sql *.sql.gz *.gz|Backup files"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DialogError(f"The dialog could not be shown: {e}") from e
    if r.returncode != 0:   # 1: cancelled (both tools)
        return None
    return r.stdout.strip() or None


# ---------------------------------------------------------------- Windows: IFileOpenDialog through ctypes

def _windows(folder: bool, start: str) -> str | None:
    out: dict = {}

    def work():
        try:
            out["path"] = _windows_dialog(folder, start)
        except Exception as e:   # noqa: BLE001 (reported to the page)
            out["error"] = e

    t = threading.Thread(target=work, name="pgbs-file-dialog", daemon=True)
    t.start()
    t.join(TIMEOUT_S)
    if "error" in out:
        raise DialogError(f"The dialog could not be shown: {out['error']}")
    return out.get("path")


def _windows_dialog(folder: bool, start: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    ole32, shell32, user32 = ctypes.windll.ole32, ctypes.windll.shell32, ctypes.windll.user32

    class GUID(ctypes.Structure):
        _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16), ("d3", ctypes.c_uint16),
                    ("d4", ctypes.c_ubyte * 8)]

    def guid(text: str) -> GUID:
        g = GUID()
        ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
        return g

    class FILTERSPEC(ctypes.Structure):
        _fields_ = [("name", ctypes.c_wchar_p), ("spec", ctypes.c_wchar_p)]

    HRESULT = ctypes.c_long
    S_OK, CANCELLED = 0, -2147023673   # HRESULT_FROM_WIN32(ERROR_CANCELLED) = 0x800704C7
    FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM, FOS_FILEMUSTEXIST, FOS_PATHMUSTEXIST = 0x20, 0x40, 0x1000, 0x800
    SIGDN_FILESYSPATH = -2147123200    # 0x80058000

    def method(obj, index: int, *argtypes):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])

    def check(hr: int, what: str) -> None:
        if hr != S_OK:
            raise OSError(f"{what} failed (0x{hr & 0xFFFFFFFF:08X})")

    def release(obj) -> None:
        if obj:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])(obj)

    ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED
    owner = dialog = folder_item = result = None
    try:
        # a hidden top-most owner keeps the dialog above the browser window
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                           wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_POPUP = 0x8, 0x80, 0x80000000
        owner = user32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW, "STATIC", "PG Backup Studio", WS_POPUP,
                                       0, 0, 0, 0, None, None, None, None)
        if owner:
            user32.SetForegroundWindow(owner)

        dialog = ctypes.c_void_p()
        check(ole32.CoCreateInstance(ctypes.byref(guid("{DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7}")), None, 1,
                                     ctypes.byref(guid("{d57c7288-d4ad-4768-be02-9d969532d960}")),
                                     ctypes.byref(dialog)), "Creating the dialog")
        # IFileDialog vtable: 3 Show, 4 SetFileTypes, 9 SetOptions, 10 GetOptions, 12 SetFolder, 17 SetTitle,
        # 20 GetResult
        opts = wintypes.DWORD()
        check(method(dialog, 10, ctypes.POINTER(wintypes.DWORD))(dialog, ctypes.byref(opts)), "GetOptions")
        flags = opts.value | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST
        flags |= FOS_PICKFOLDERS if folder else FOS_FILEMUSTEXIST
        check(method(dialog, 9, wintypes.DWORD)(dialog, flags), "SetOptions")
        if not folder:
            specs = (FILTERSPEC * len(BACKUP_FILE_TYPES))(*[FILTERSPEC(n, s) for n, s in BACKUP_FILE_TYPES])
            check(method(dialog, 4, wintypes.UINT, ctypes.c_void_p)(dialog, len(specs), specs), "SetFileTypes")
        method(dialog, 17, wintypes.LPCWSTR)(dialog, "Choose a folder" if folder else "Choose a backup file")
        folder_item = ctypes.c_void_p()
        if shell32.SHCreateItemFromParsingName(ctypes.c_wchar_p(start), None,
                                               ctypes.byref(guid("{43826d1e-e718-42ee-bc55-a1e261c37bfe}")),
                                               ctypes.byref(folder_item)) == S_OK:
            method(dialog, 12, ctypes.c_void_p)(dialog, folder_item)
        hr = method(dialog, 3, wintypes.HWND)(dialog, owner)
        if hr == CANCELLED:
            return None
        check(hr, "Showing the dialog")
        result = ctypes.c_void_p()
        check(method(dialog, 20, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(result)), "GetResult")
        name = ctypes.c_wchar_p()
        # IShellItem vtable: 5 GetDisplayName
        check(method(result, 5, ctypes.c_int, ctypes.POINTER(ctypes.c_wchar_p))(result, SIGDN_FILESYSPATH,
                                                                                ctypes.byref(name)), "GetDisplayName")
        path = name.value
        ole32.CoTaskMemFree(name)
        return path
    finally:
        for obj in (result, folder_item, dialog):
            release(obj)
        if owner:
            user32.DestroyWindow(owner)
        ole32.CoUninitialize()
