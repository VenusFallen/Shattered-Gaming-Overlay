"""Process/window enumeration and OS-foreground-focus tracking for the Target Window feature.
Companion counterpart to panels/window_select.py: this module owns the psutil + win32 calls;
the panel only renders app_state.py's WindowSelectState. Pure user-mode, read-only Win32 calls
only -- no driver, no target-process memory access, no DLL injection, no SendInput/hooks here.

A background thread (`start_focus_tracking()`) polls foreground focus and the live target pid
independently of the Companion window's render loop, since Hello ImGui's `show_gui` callback
(which drives `refresh_if_stale()`) doesn't fire while the window is minimized.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import List, Optional

import psutil

from app_state import ProcessInfo, WindowSelectState

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# ---------------------------------------------------------------------------
# Win32 constants / bindings
# ---------------------------------------------------------------------------

GW_OWNER = 4
DWMWA_CLOAKED = 14  # Win8+: true for DWM-cloaked (e.g. suspended/off-screen UWP) windows -- filtered out below.

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
    """Enumerate top-level, visible, unowned, non-cloaked windows with a real title -- roughly "what could I Alt+Tab to". One entry per pid; returns a fresh list every call, callers wanting this throttled should use `refresh_if_stale` instead."""
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
    """The pid that currently owns real OS foreground focus, or 0 if none."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    return _pid_for_hwnd(hwnd)


_REFRESH_INTERVAL_SEC = 2.0
# Module-level rather than on WindowSelectState, which is meant to stay pure data.
_last_refresh_monotonic = 0.0


def _reacquire_selected_if_stale(state: WindowSelectState) -> None:
    """Pids aren't stable across a target process's own restarts -- if `selected` is no longer among the freshly enumerated windows but one with the same exe name now is, silently re-point `selected` at it instead of leaving it stuck on a dead pid."""
    selected = state.selected
    if selected is None:
        return
    if any(p.pid == selected.pid for p in state.available):
        return  # still a live, correctly-targeted process -- nothing to do
    match = next((p for p in state.available if p.exe_name.lower() == selected.exe_name.lower()), None)
    if match is not None:
        state.selected = match


def force_refresh(state: WindowSelectState) -> None:
    """Immediate re-enumeration, bypassing the throttle -- for the UI's manual Refresh button."""
    global _last_refresh_monotonic
    _last_refresh_monotonic = time.monotonic()
    try:
        state.available = enumerate_target_windows()
        _reacquire_selected_if_stale(state)
    except OSError:
        pass


def refresh_if_stale(state: WindowSelectState) -> None:
    """Cheap per-frame entry point for the UI layer: always updates `state.selected_has_focus`, but only re-enumerates `state.available` once `_REFRESH_INTERVAL_SEC` has elapsed since the last enumeration."""
    global _last_refresh_monotonic
    now = time.monotonic()
    if (now - _last_refresh_monotonic) >= _REFRESH_INTERVAL_SEC:
        _last_refresh_monotonic = now
        try:
            state.available = enumerate_target_windows()
            _reacquire_selected_if_stale(state)
        except OSError:
            # Best-effort -- leave the previous list rather than blank the UI
            # over a transient Win32 error.
            pass

    if state.selected is not None:
        state.selected_has_focus = cached_foreground_pid() == state.selected.pid
    else:
        state.selected_has_focus = False


# Independent focus polling -- see module docstring.

_FOCUS_POLL_INTERVAL_SEC = 0.1
# Generous vs. the poll interval -- past this, cached_foreground_pid() falls back to a direct call.
_FOCUS_CACHE_STALE_SEC = 1.0

_focus_lock = threading.Lock()
_cached_foreground_pid = 0
_last_focus_poll_monotonic = 0.0
_focus_thread: Optional[threading.Thread] = None
_focus_thread_stop = threading.Event()

# Live-resolved target pid, kept current by the SAME background thread as focus polling below.
_TARGET_REACQUIRE_INTERVAL_SEC = 2.0  # matches _REFRESH_INTERVAL_SEC's cadence
_target_lock = threading.Lock()
_target_exe_name: Optional[str] = None
_cached_target_pid = 0
_last_target_reacquire_monotonic = 0.0


def set_target_exe_name(exe_name: Optional[str]) -> None:
    """Tell the background focus-tracking thread which exe (if any) to keep resolving a live pid for -- pass `None` for no target. Cheap and safe to call redundantly every frame; only needs to actually land once per target change, since `cached_target_pid()` then stays current on its own even while the Companion window is minimized."""
    global _target_exe_name, _cached_target_pid, _last_target_reacquire_monotonic
    with _target_lock:
        if _target_exe_name != exe_name:
            _target_exe_name = exe_name
            _cached_target_pid = 0
            # Force the next poll tick to re-resolve immediately rather than
            # waiting out whatever's left of the old target's interval.
            _last_target_reacquire_monotonic = 0.0


def cached_target_pid() -> int:
    """Thread-safe, non-blocking read of the live-resolved pid for whatever exe `set_target_exe_name()` was last told to target; 0 if none set or found yet. Kept current by the background focus thread independent of the Companion window's render state, so the remapper's gate stays correct even if the target game restarts with a new pid while minimized."""
    with _target_lock:
        return _cached_target_pid


def _maybe_reacquire_target(now: float) -> None:
    """One tick of target-pid re-resolution, factored out of `_focus_poll_loop()` so tests can call it without spinning a real thread."""
    global _cached_target_pid, _last_target_reacquire_monotonic
    with _target_lock:
        target_exe = _target_exe_name
        due = target_exe is not None and (now - _last_target_reacquire_monotonic) >= _TARGET_REACQUIRE_INTERVAL_SEC
    if not due:
        return
    try:
        windows = enumerate_target_windows()
    except OSError:
        windows = []
    match = next((w for w in windows if w.exe_name.lower() == target_exe.lower()), None)
    with _target_lock:
        # Re-check target hasn't changed while the slow enumeration above was running.
        if _target_exe_name == target_exe:
            _cached_target_pid = match.pid if match is not None else 0
            _last_target_reacquire_monotonic = now


def _focus_poll_loop() -> None:
    global _cached_foreground_pid, _last_focus_poll_monotonic
    while not _focus_thread_stop.is_set():
        try:
            pid = foreground_pid()
        except OSError:
            pid = 0
        with _focus_lock:
            _cached_foreground_pid = pid
            _last_focus_poll_monotonic = time.monotonic()

        _maybe_reacquire_target(time.monotonic())

        _focus_thread_stop.wait(_FOCUS_POLL_INTERVAL_SEC)


def start_focus_tracking() -> None:
    """Start the background thread that keeps focus tracking current independent of the Companion window's render loop. Idempotent."""
    global _focus_thread
    if _focus_thread is not None and _focus_thread.is_alive():
        return
    _focus_thread_stop.clear()
    _focus_thread = threading.Thread(target=_focus_poll_loop, name="SGO-FocusTracker", daemon=True)
    _focus_thread.start()


def stop_focus_tracking() -> None:
    """Stop the background thread cleanly. Safe to call even if never started."""
    global _focus_thread, _target_exe_name, _cached_target_pid, _last_target_reacquire_monotonic
    _focus_thread_stop.set()
    thread = _focus_thread
    _focus_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)
    with _target_lock:
        _target_exe_name = None
        _cached_target_pid = 0
        _last_target_reacquire_monotonic = 0.0


def focus_tracking_is_running() -> bool:
    return _focus_thread is not None and _focus_thread.is_alive()


def cached_foreground_pid() -> int:
    """Thread-safe, non-blocking read of the last-polled OS foreground pid; falls back to a direct synchronous call if the cache is stale (tracker never started, or died)."""
    with _focus_lock:
        cached = _cached_foreground_pid
        fresh_enough = (time.monotonic() - _last_focus_poll_monotonic) < _FOCUS_CACHE_STALE_SEC
    if fresh_enough:
        return cached
    return foreground_pid()
