"""file_dialog.py -- native Windows Open/Save file dialogs via raw ctypes
bindings to comdlg32.dll's classic common-dialog API (GetOpenFileNameW /
GetSaveFileNameW). Matches this project's established convention for OS
integration (see tray_icon.py, titlebar.py, window_select.py) -- no GUI
toolkit dependency, and deliberately not the heavier IFileDialog COM
interface.

These calls are blocking/modal by nature. That's fine here: both are only
ever triggered by a deliberate button click (Profiles panel's Export/Import),
never from the per-frame render loop.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Optional

if sys.platform != "win32":
    raise ImportError("file_dialog.py is Windows-only (uses comdlg32.dll via ctypes).")

_comdlg32 = ctypes.windll.comdlg32
_user32 = ctypes.windll.user32
# Un-annotated ctypes calls default to a 32-bit c_int return, which truncates
# a 64-bit HWND -- same gotcha titlebar.py's _hwnd() already guards against.
_user32.GetActiveWindow.restype = wintypes.HWND

_OFN_PATHMUSTEXIST = 0x00000800
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_OVERWRITEPROMPT = 0x00000002
_OFN_EXPLORER = 0x00080000
_OFN_NOCHANGEDIR = 0x00000008  # dialog navigation shouldn't change the process's cwd

_PATH_BUF_LEN = 32768  # long-path-aware; well beyond legacy 260-char MAX_PATH

_JSON_FILTER = "JSON Files\0*.json\0All Files\0*.*\0\0"


class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", ctypes.c_void_p),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


_comdlg32.GetOpenFileNameW.argtypes = (ctypes.POINTER(_OPENFILENAMEW),)
_comdlg32.GetOpenFileNameW.restype = wintypes.BOOL
_comdlg32.GetSaveFileNameW.argtypes = (ctypes.POINTER(_OPENFILENAMEW),)
_comdlg32.GetSaveFileNameW.restype = wintypes.BOOL


def _owner_hwnd() -> int:
    # Best-effort dialog owner -- a dialog with no owner still works (just
    # isn't modal-parented to the Companion window), so this never blocks
    # showing it even if GetActiveWindow comes back empty.
    try:
        return _user32.GetActiveWindow() or 0
    except OSError:
        return 0


def show_save_dialog(default_filename: str = "", title: str = "Export Profile") -> Optional[str]:
    """Blocking native Save File dialog, JSON-filtered. Returns the chosen
    path, or None if the user cancelled."""
    buf = ctypes.create_unicode_buffer(default_filename or "", _PATH_BUF_LEN)
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.hwndOwner = _owner_hwnd()
    ofn.lpstrFilter = _JSON_FILTER
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = _PATH_BUF_LEN
    ofn.lpstrDefExt = "json"
    ofn.lpstrTitle = title
    ofn.Flags = _OFN_EXPLORER | _OFN_OVERWRITEPROMPT | _OFN_NOCHANGEDIR

    if not _comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
        return None
    return buf.value or None


def show_open_dialog(title: str = "Import Profile") -> Optional[str]:
    """Blocking native Open File dialog, JSON-filtered. Returns the chosen
    path, or None if the user cancelled."""
    buf = ctypes.create_unicode_buffer(_PATH_BUF_LEN)
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.hwndOwner = _owner_hwnd()
    ofn.lpstrFilter = _JSON_FILTER
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = _PATH_BUF_LEN
    ofn.lpstrDefExt = "json"
    ofn.lpstrTitle = title
    ofn.Flags = _OFN_EXPLORER | _OFN_PATHMUSTEXIST | _OFN_FILEMUSTEXIST | _OFN_NOCHANGEDIR

    if not _comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return None
    return buf.value or None
