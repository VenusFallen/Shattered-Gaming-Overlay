"""window_select.py -- process/window enumeration and OS-foreground-focus
tracking for the Target Window feature.

Companion counterpart to panels/window_select.py (same split as
input_hooks.py/input_inject.py vs their panel files): this module owns the
actual psutil + win32 API calls; the panel only renders app_state.py's
WindowSelectState.

Pure user-mode, read-only Win32 calls only -- EnumWindows/GetWindowText/
GetWindowThreadProcessId/GetForegroundWindow via ctypes, plus psutil for the
exe name of a pid. No driver, no target-process memory access, no DLL
injection -- see .claude/agents/engine-agent.md's hard rule. This module
never touches SendInput or the input hooks at all.

Public API
----------
`enumerate_target_windows()` -- one-shot, does the actual (comparatively
heavier) EnumWindows pass + psutil lookups, returns a fresh list of
app_state.ProcessInfo.

`foreground_pid()` -- one-shot, cheap: which pid currently owns OS
foreground focus.

`refresh_if_stale(state)` -- the function the UI layer actually calls, once
per frame, without needing to know about either of the above or their
relative cost: always re-checks foreground focus (cheap), but only
re-enumerates `state.available` if `_REFRESH_INTERVAL_SEC` has elapsed since
the last enumeration (comparatively heavy -- EnumWindows over every top-level
window on the system plus a psutil.Process() call per surviving candidate).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import List

import psutil

from app_state import ProcessInfo, WindowSelectState

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# ---------------------------------------------------------------------------
# Win32 constants / bindings
# ---------------------------------------------------------------------------

GW_OWNER = 4
DWMWA_CLOAKED = 14  # Win8+: true for DWM-cloaked windows (e.g. suspended/
# off-screen UWP apps) -- these pass IsWindowVisible but aren't anything a
# user could sensibly click on to target, so they're filtered out below.

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL

user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL

user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int

user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int

user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetWindow.restype = wintypes.HWND

user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

user32.GetForegroundWindow.argtypes = ()
user32.GetForegroundWindow.restype = wintypes.HWND

dwmapi.DwmGetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    if hr != 0:  # call failed (e.g. pre-Win8) -- treat as not cloaked
        return False
    return bool(cloaked.value)


def _pid_for_hwnd(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enumerate_target_windows() -> List[ProcessInfo]:
    """Enumerate top-level, visible, unowned, non-cloaked windows that have a
    real title -- i.e. the same rough "what could I Alt+Tab to" set R9Tools'
    Window Filter enumerated, not a raw psutil.process_iter() dump (which is
    mostly windowless/background noise the user could never target anyway).

    One entry per pid (first window EnumWindows hands back for that pid
    wins -- EnumWindows walks top-level windows in z-order, so that's
    normally the process's actual main window). Returns a fresh list every
    call; callers that want this throttled should go through
    `refresh_if_stale` instead of calling this directly every frame.
    """
    results: List[ProcessInfo] = []
    seen_pids: set[int] = set()

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindow(hwnd, GW_OWNER):
            return True  # owned window (dialog/tooltip/popup) -- skip
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        if _is_cloaked(hwnd):
            return True
        pid = _pid_for_hwnd(hwnd)
        if not pid or pid in seen_pids:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if not title:
            return True
        try:
            exe_name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
        seen_pids.add(pid)
        results.append(ProcessInfo(pid=pid, exe_name=exe_name, window_title=title))
        return True  # keep enumerating

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    results.sort(key=lambda p: p.exe_name.lower())
    return results


def foreground_pid() -> int:
    """The pid that currently owns real OS foreground focus, or 0 if none
    (e.g. transient state right at focus-switch time)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    return _pid_for_hwnd(hwnd)


_REFRESH_INTERVAL_SEC = 2.0
# Module-level rather than stored on WindowSelectState: the Companion window
# only ever runs one instance of that state for the life of the process, and
# WindowSelectState is a plain (unhashable, non-frozen) dataclass not meant
# to carry engine-internal bookkeeping fields -- see app_state.py's own note
# that its dataclasses are "pure data, no I/O".
_last_refresh_monotonic = 0.0


def refresh_if_stale(state: WindowSelectState) -> None:
    """Cheap per-frame entry point for the UI layer -- call this once at the
    top of panels/window_select.py::render_section every frame.

    Always updates `state.selected_has_focus` against real OS foreground
    focus (a single GetForegroundWindow + GetWindowThreadProcessId call --
    cheap enough to do unconditionally, no throttling). Only re-enumerates
    `state.available` (the actual EnumWindows pass + a psutil.Process() call
    per candidate window) if `_REFRESH_INTERVAL_SEC` has elapsed since the
    last enumeration.
    """
    global _last_refresh_monotonic
    now = time.monotonic()
    if (now - _last_refresh_monotonic) >= _REFRESH_INTERVAL_SEC:
        _last_refresh_monotonic = now
        try:
            state.available = enumerate_target_windows()
        except OSError:
            # Best-effort -- leave the previous `available` list in place
            # rather than blanking the UI out over a transient Win32 error.
            pass

    if state.selected is not None:
        state.selected_has_focus = foreground_pid() == state.selected.pid
    else:
        state.selected_has_focus = False
