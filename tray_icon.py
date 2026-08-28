"""tray_icon.py -- system tray icon for the Companion window's
minimize-to-tray behavior.

Companion-window-only concern: this has nothing to do with the HUD overlay
(hud_overlay.py) and must never touch it -- the HUD overlay keeps
rendering/staying hidden purely per its own per-element enable toggles,
regardless of whether the Companion window is shown, minimized, or hidden to
tray (see hud_overlay.py's module docstring). This module only ever calls
Win32 APIs against the Companion window's own HWND and its own tray-icon
message window.

Same "own background thread + plain Win32 via ctypes" pattern as
window_select.py's focus-tracking thread and hud_overlay.py's render thread
-- no pywin32 (win32gui/win32api) despite it being in requirements.txt for
other reasons; nothing else in this codebase actually imports it either, so
raw ctypes bindings stay the one established pattern for Win32 calls here.

Runs its own message-only-style hidden window (RegisterClassW/
CreateWindowExW, same shape as hud_overlay.py's window creation) purely to
receive the Shell_NotifyIcon callback message and the popup menu's
WM_COMMAND -- it is never shown, never sized, and never receives real user
input from anywhere other than the tray icon itself.

Deliberately uses the classic (pre-NOTIFYICON_VERSION_4) callback shape --
`Shell_NotifyIconW`'s `uCallbackMessage` lParam is the raw mouse message
(WM_RBUTTONUP / WM_LBUTTONDBLCLK) rather than version 4's packed
icon-id-plus-message form. Version 4 also changes single-click/right-click
semantics in ways that are harder to reason about without live testing
(no GUI available in this environment -- see this module's own header
comment in the task that produced it); the classic shape is simpler, still
fully supported on modern Windows, and enough for a two-item menu.

Public API
----------
`tray_icon.start()`   -- create the tray icon + its message-pump thread.
`tray_icon.stop()`    -- remove the tray icon and tear the thread down.
`tray_icon.is_running()` -- whether the tray icon is currently live.

Quit goes through the same `RunnerParams.app_shall_exit` flag titlebar.py's
`_close()` uses, so the normal shutdown path (main.py's `before_exit`
callback -- tearing down hooks, the HUD overlay, the stats poller, this
module's own thread, etc.) still runs, same as a normal titlebar close. This
is a direct cross-thread bool assignment rather than a lock-guarded snapshot
-- consistent with `HudOverlay._running`'s own bare bool flag (set from the
main thread, read from the render thread, no lock) elsewhere in this
codebase: a single bool write needs nothing heavier.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from version import WINDOW_TITLE

_log = logging.getLogger("shattered_overlay.tray")

if not sys.platform.startswith("win"):
    raise ImportError("tray_icon.py is Windows-only (uses Shell_NotifyIcon via ctypes).")

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_shell32 = ctypes.windll.shell32

_ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.ico"

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

_WS_OVERLAPPED = 0x00000000
_WM_DESTROY = 0x0002
_WM_COMMAND = 0x0111
_WM_RBUTTONUP = 0x0205
_WM_LBUTTONDBLCLK = 0x0203
_WM_NULL = 0x0000
_WM_APP = 0x8000
_WM_TRAYICON = _WM_APP + 1

_NIM_ADD = 0x00000000
_NIM_DELETE = 0x00000002

_NIF_MESSAGE = 0x00000001
_NIF_ICON = 0x00000002
_NIF_TIP = 0x00000004

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x00000010

_SM_CXSMICON = 49
_SM_CYSMICON = 50

_SW_SHOW = 5
_SW_RESTORE = 9

_MF_STRING = 0x00000000
_MF_SEPARATOR = 0x00000800
_TPM_RIGHTBUTTON = 0x0002

_ID_TRAY_SHOW = 1001
_ID_TRAY_QUIT = 1002

_WPARAM = ctypes.c_uint64
_LPARAM = ctypes.c_int64
_LRESULT = ctypes.c_int64

_DefWindowProcW = _user32.DefWindowProcW
_DefWindowProcW.restype = _LRESULT
_DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]

_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)


class _WndClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


_shell32.Shell_NotifyIconW.argtypes = (wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW))
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL

# Explicit signatures for every call that returns or receives an HWND/HICON/
# HMENU handle -- same rigor titlebar.py uses for its own Companion-HWND
# calls, since ctypes' un-annotated argument guessing (plain c_int) can
# silently mishandle a 64-bit handle value.
_user32.FindWindowW.restype = wintypes.HWND
_user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
_user32.IsIconic.restype = wintypes.BOOL
_user32.IsIconic.argtypes = (wintypes.HWND,)
_user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DestroyWindow.argtypes = (wintypes.HWND,)
_user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.AppendMenuW.argtypes = (wintypes.HMENU, wintypes.UINT, ctypes.c_void_p, wintypes.LPCWSTR)
_user32.TrackPopupMenu.argtypes = (
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
)
_user32.DestroyMenu.argtypes = (wintypes.HMENU,)
_user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
_user32.LoadImageW.restype = wintypes.HANDLE
_user32.LoadImageW.argtypes = (
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT,
)
_user32.DestroyIcon.argtypes = (wintypes.HICON,)
_user32.GetSystemMetrics.argtypes = (ctypes.c_int,)


def _companion_hwnd() -> int:
    """Look the Companion window up by its exact title, same fallback
    `FindWindowW` path titlebar.py uses -- NOT `GetActiveWindow`, which is
    scoped to the *calling thread's* message queue and would always return 0
    here since this module's calls all come from the tray icon's own
    background thread, never the Companion window's own GUI thread."""
    return _user32.FindWindowW(None, WINDOW_TITLE)


def _restore_companion_window() -> None:
    hwnd = _companion_hwnd()
    if not hwnd:
        return
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, _SW_RESTORE)
    else:
        _user32.ShowWindow(hwnd, _SW_SHOW)
    _user32.SetForegroundWindow(hwnd)


class TrayIcon:
    """The tray icon + its own hidden message window/thread. See module
    docstring for the public start()/stop()/is_running() contract."""

    _CLASS_NAME = "ShatteredGamingOverlayTray"

    def __init__(self) -> None:
        self._hwnd = 0
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._icon_added = False
        self._hicon = 0
        self._wnd_proc_cb = None  # keep the ctypes callback alive for the window's lifetime
        self._ready = threading.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="SGO-TrayIcon")
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, _WM_DESTROY, 0, 0)
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._create_window()
            self._add_icon()
            self._ready.set()
            self._message_loop()
        except Exception:
            logging.exception("[TrayIcon] Fatal error in tray thread")
            self._ready.set()
        finally:
            self._teardown()

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_DESTROY:
            self._running = False
            _user32.PostQuitMessage(0)
            return 0
        if msg == _WM_TRAYICON:
            event = lparam & 0xFFFFFFFF
            if event == _WM_LBUTTONDBLCLK:
                _restore_companion_window()
                return 0
            if event == _WM_RBUTTONUP:
                self._show_context_menu()
                return 0
            return 0
        if msg == _WM_COMMAND:
            command_id = wparam & 0xFFFF
            if command_id == _ID_TRAY_SHOW:
                _restore_companion_window()
            elif command_id == _ID_TRAY_QUIT:
                self._quit()
            return 0
        return _DefWindowProcW(hwnd, msg, wparam, lparam)

    def _quit(self) -> None:
        # Same exit flag titlebar.py's own Close button sets -- lets the
        # normal Hello ImGui shutdown path (main.py's before_exit callback)
        # run instead of killing the process out from under it.
        try:
            from imgui_bundle import hello_imgui as hi
            hi.get_runner_params().app_shall_exit = True
        except Exception:
            logging.exception("[TrayIcon] Failed to set app_shall_exit -- falling back to os._exit")
            import os
            os._exit(0)

    def _show_context_menu(self) -> None:
        hmenu = _user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            _user32.AppendMenuW(hmenu, _MF_STRING, _ID_TRAY_SHOW, "Show")
            _user32.AppendMenuW(hmenu, _MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(hmenu, _MF_STRING, _ID_TRAY_QUIT, "Quit")

            pt = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))

            # Classic dance so the popup dismisses correctly when the user
            # clicks away from it (documented Shell_NotifyIcon/TrackPopupMenu
            # requirement, not optional flourish): the tray window must be
            # brought to the foreground before TrackPopupMenu, and sent a
            # harmless WM_NULL right after so its own message queue doesn't
            # get stuck thinking the menu is still the active popup owner.
            # TPM_NONOTIFY is deliberately NOT passed here -- that flag
            # suppresses the WM_COMMAND this window's _wnd_proc relies on to
            # learn which item was picked (it only makes sense paired with
            # TPM_RETURNCMD, which reads the choice off TrackPopupMenu's own
            # return value instead -- not used here).
            _user32.SetForegroundWindow(self._hwnd)
            _user32.TrackPopupMenu(hmenu, _TPM_RIGHTBUTTON, pt.x, pt.y, 0, self._hwnd, None)
            _user32.PostMessageW(self._hwnd, _WM_NULL, 0, 0)
        finally:
            _user32.DestroyMenu(hmenu)

    def _create_window(self) -> None:
        hinstance = _kernel32.GetModuleHandleW(None)
        self._wnd_proc_cb = _WNDPROC(self._wnd_proc)

        wc = _WndClass()
        wc.style = 0
        wc.lpfnWndProc = self._wnd_proc_cb
        wc.hInstance = hinstance
        wc.lpszClassName = self._CLASS_NAME

        _user32.RegisterClassW(ctypes.byref(wc))

        # Never shown -- this window exists only to own a message queue for
        # the Shell_NotifyIcon callback + popup menu, same idea as a
        # message-only window (HWND_MESSAGE isn't used here since
        # TrackPopupMenu/SetForegroundWindow behave more predictably against
        # an ordinary, if invisible, top-level window).
        hwnd = _user32.CreateWindowExW(
            0, self._CLASS_NAME, "SGO Tray", _WS_OVERLAPPED,
            0, 0, 0, 0, None, None, hinstance, None,
        )
        if not hwnd:
            _user32.UnregisterClassW(self._CLASS_NAME, hinstance)
            raise OSError(f"CreateWindowEx failed: {ctypes.GetLastError()}")
        self._hwnd = hwnd

    def _add_icon(self) -> None:
        cx = _user32.GetSystemMetrics(_SM_CXSMICON)
        cy = _user32.GetSystemMetrics(_SM_CYSMICON)
        hicon = 0
        if _ICON_PATH.exists():
            hicon = _user32.LoadImageW(None, str(_ICON_PATH), _IMAGE_ICON, cx, cy, _LR_LOADFROMFILE)
        self._hicon = hicon

        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = 1
        data.uFlags = _NIF_MESSAGE | _NIF_TIP | (_NIF_ICON if hicon else 0)
        data.uCallbackMessage = _WM_TRAYICON
        data.hIcon = hicon
        data.szTip = WINDOW_TITLE

        if _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data)):
            self._icon_added = True
        else:
            _log.error("[TrayIcon] Shell_NotifyIcon(NIM_ADD) failed")

    def _message_loop(self) -> None:
        msg = _MSG()
        while self._running:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def _teardown(self) -> None:
        if self._icon_added:
            data = _NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            data.hWnd = self._hwnd
            data.uID = 1
            _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
            self._icon_added = False
        if self._hicon:
            _user32.DestroyIcon(self._hicon)
            self._hicon = 0
        if self._hwnd:
            _user32.DestroyWindow(self._hwnd)
            _user32.UnregisterClassW(self._CLASS_NAME, _kernel32.GetModuleHandleW(None))
            self._hwnd = 0


# ---------------------------------------------------------------------------
# Process-wide singleton -- main.py starts/stops this alongside the
# Companion window's own lifecycle (see main.py's _post_init/_before_exit),
# same pattern as hud_overlay.hud_overlay / stats_poller's module-level
# instance.
# ---------------------------------------------------------------------------

tray_icon = TrayIcon()
