"""window_select.py -- process/window enumeration and OS-foreground-focus
tracking for the Target Window feature.

Companion counterpart to panels/window_select.py (same split as
input_hooks.py/input_inject.py vs their panel files): this module owns the
actual psutil + win32 API calls; the panel only renders app_state.py's
WindowSelectState.

Pure user-mode, read-only Win32 calls only -- EnumWindows/GetWindowText/
GetWindowThreadProcessId/GetForegroundWindow via ctypes, plus psutil for the
exe name of a pid. No driver, no target-process memory access, no DLL
injection -- keeping to the project's hard rule against any of that. This
module never touches SendInput or the input hooks at all.

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

Independent focus polling (decoupled from the Companion window's render loop)
-------------------------------------------------------------------------
`refresh_if_stale()` above is only ever called from Hello ImGui's `show_gui`
callback (see main.py/shell.py) -- which, like many GLFW-backed render loops,
is simply not invoked at all while the Companion window is minimized
(`IsIconic`). That's fine for `state.available` (a picker list -- nothing to
show while minimized anyway) but was a real bug for OS-foreground-focus
tracking: `remapper.py`'s window-filter gate needs to react the instant a
targeted game regains focus, including while the Companion window has been
sitting minimized the whole time, and a value that only updates when a frame
happens to render can't do that.

`start_focus_tracking()`/`stop_focus_tracking()` run a small dedicated
background thread (`SGO-FocusTracker`, matching `input_hooks.HookManager`'s
`SGO-InputHook` naming) that polls `foreground_pid()` on its own short,
fixed interval, publishing the result under a lock as `cached_foreground_pid()`
-- exactly the same "own thread + lock-guarded snapshot, never a shared
mutable dataclass across threads" pattern this project already uses for
`stats_poller.py`/`hud_overlay.py`. `remapper.py`'s hook thread calls
`cached_foreground_pid()` directly, fresh on every physical input event, so
its window-filter gate is correct regardless of whether the Companion window
is currently rendering, minimized, or occluded. `cached_foreground_pid()`
also has a built-in staleness fallback (see its docstring) so a caller is
never silently handed a frozen answer if the tracker thread was never
started or has died.
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


def force_refresh(state: WindowSelectState) -> None:
    """Immediate re-enumeration, bypassing the throttle -- for the UI
    layer's manual Refresh button, so the user isn't stuck waiting out
    `_REFRESH_INTERVAL_SEC` after e.g. launching a game."""
    global _last_refresh_monotonic
    _last_refresh_monotonic = time.monotonic()
    try:
        state.available = enumerate_target_windows()
    except OSError:
        pass


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
        state.selected_has_focus = cached_foreground_pid() == state.selected.pid
    else:
        state.selected_has_focus = False


# ---------------------------------------------------------------------------
# Independent focus polling -- see module docstring's "Independent focus
# polling" section for why this exists as its own dedicated thread rather
# than piggybacking on refresh_if_stale()'s per-Companion-frame call site.
# ---------------------------------------------------------------------------

_FOCUS_POLL_INTERVAL_SEC = 0.1
# Generous vs. the poll interval above -- if the background thread hasn't
# published a fresh value within this long (never started, or died
# unexpectedly), cached_foreground_pid() falls back to a direct synchronous
# call rather than silently handing back a value frozen at whatever it was
# the instant the thread stopped updating.
_FOCUS_CACHE_STALE_SEC = 1.0

_focus_lock = threading.Lock()
_cached_foreground_pid = 0
_last_focus_poll_monotonic = 0.0
_focus_thread: Optional[threading.Thread] = None
_focus_thread_stop = threading.Event()


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
        _focus_thread_stop.wait(_FOCUS_POLL_INTERVAL_SEC)


def start_focus_tracking() -> None:
    """Start the dedicated background thread that keeps OS-foreground-focus
    tracking current independent of the Companion window's render loop --
    call once at startup (see main.py's `_post_init`), alongside
    `hud_overlay.start()`/`stats_poller.start()`/`remapper_engine.start()`.
    Idempotent; safe to call again if already running."""
    global _focus_thread
    if _focus_thread is not None and _focus_thread.is_alive():
        return
    _focus_thread_stop.clear()
    _focus_thread = threading.Thread(target=_focus_poll_loop, name="SGO-FocusTracker", daemon=True)
    _focus_thread.start()


def stop_focus_tracking() -> None:
    """Stop the background thread cleanly (see main.py's `_before_exit`).
    Safe to call even if the thread was never started."""
    global _focus_thread
    _focus_thread_stop.set()
    thread = _focus_thread
    _focus_thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=1.0)


def focus_tracking_is_running() -> bool:
    return _focus_thread is not None and _focus_thread.is_alive()


def cached_foreground_pid() -> int:
    """Thread-safe, cheap, non-blocking read of the last-polled OS
    foreground pid, kept current by the background thread started via
    `start_focus_tracking()`. Safe to call from any thread (in particular
    `remapper.py`'s hook thread, on every physical input event) at any rate,
    and correct regardless of whether the Companion window is currently
    rendering frames at all (minimized, occluded, idling, etc.) -- this is
    the whole point of the dedicated thread over piggybacking on
    `refresh_if_stale()`.

    Falls back to a direct synchronous `foreground_pid()` call if the cached
    value is stale (tracker never started, or died) -- callers never
    silently get a frozen answer.
    """
    with _focus_lock:
        cached = _cached_foreground_pid
        fresh_enough = (time.monotonic() - _last_focus_poll_monotonic) < _FOCUS_CACHE_STALE_SEC
    if fresh_enough:
        return cached
    return foreground_pid()
