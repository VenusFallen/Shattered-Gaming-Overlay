"""
input_hooks.py -- pure user-mode input capture for Shattered Gaming Overlay.

Wraps `SetWindowsHookEx(WH_KEYBOARD_LL / WH_MOUSE_LL, ...)` via `ctypes`.
Deliberately dependency-free (no third-party hook library) so both the
future overlay and future capture/bind-UI code can import this cleanly.
No driver, no ViGEm, no game-process access -- see
.claude/agents/engine-agent.md for why.

Public API
----------
`HookManager` -- install the hooks, pump the Win32 message loop on its own
thread, and dispatch typed dataclass events (`KeyEvent`, `MouseButtonEvent`,
`MouseMoveEvent`, `MouseScrollEvent`) to registered callbacks.

    hm = HookManager()
    hm.on_key_down(lambda e: print(e))
    hm.start()
    ...
    hm.stop()

Injected-input detection
-------------------------
Every event carries two independent flags:

- `injected`  -- Windows' own LLKHF_INJECTED / LLMHF_INJECTED bit: true for
  *any* synthetic input, from us, from the Windows On-Screen Keyboard, from
  another accessibility tool, from anything.
- `from_self` -- true only when the event's dwExtraInfo matches
  `input_inject.INJECTED_MARKER`, i.e. it was produced by *our own*
  `input_inject.py`.

The remapper/macro engine built on top of this should key off `from_self`
to avoid feedback loops on its own re-injected events, while still treating
other injected input (e.g. an accessibility user's on-screen keyboard) as a
legitimate trigger. Filtering on the blunter `injected` flag would break
that.

Suppression
------------
By design this layer is observe-only by default: it always calls
CallNextHookEx and lets the event through. A registered callback may
return `True` to suppress the event (stop it from reaching the rest of
the system) -- this is intentionally exposed now because the remapper
that gets built on top of this module will need it (to swallow the
original key of a remap), not because this task implements remapping.
With no callbacks, or callbacks that return None/False, nothing is ever
blocked.

Threading / timing gotcha
--------------------------
`WH_KEYBOARD_LL`/`WH_MOUSE_LL` callbacks run on the thread that installed
them, driven by that thread's message pump, and are subject to Windows'
low-level-hook responsiveness timeout: a callback that blocks or does
slow work can make Windows treat the hook as unresponsive, causing
system-wide input lag or the hook silently getting removed. Keep
registered callbacks fast and non-blocking; hand off real work to a
queue/other thread.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, List, Optional

if not sys.platform.startswith("win"):
    raise ImportError("input_hooks.py is Windows-only (uses SetWindowsHookEx via ctypes).")

from input_inject import INJECTED_MARKER, MouseButton  # noqa: E402  (platform-gated import above)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

HC_ACTION = 0

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_MOUSEHWHEEL = 0x020E

WM_QUIT = 0x0012

# KBDLLHOOKSTRUCT.flags bits
LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10
LLKHF_ALTDOWN = 0x20
LLKHF_UP = 0x80

# MSLLHOOKSTRUCT.flags bits
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

MAPVK_VK_TO_VSC = 0
PM_NOREMOVE = 0x0000


# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK

user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT

user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL

user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = ctypes.c_int

user32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
user32.PeekMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)

user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL

user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT

user32.GetKeyNameTextW.argtypes = (ctypes.c_long, wintypes.LPWSTR, ctypes.c_int)
user32.GetKeyNameTextW.restype = ctypes.c_int

kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def get_key_name(vk_code: int, scan_code: int = 0, extended: bool = False) -> str:
    """Best-effort human-readable name for a vk/scan code, e.g. 'F1', 'Left Ctrl'.

    Handy for a future "press a key to bind" capture UI. Falls back to a
    'VK_0x..' string if Windows doesn't have a name for it.
    """
    if not scan_code:
        scan_code = user32.MapVirtualKeyW(vk_code, MAPVK_VK_TO_VSC)
    lparam = (scan_code & 0xFF) << 16
    if extended:
        lparam |= 1 << 24
    buf = ctypes.create_unicode_buffer(64)
    length = user32.GetKeyNameTextW(lparam, buf, 64)
    if length > 0:
        return buf.value
    return f"VK_{vk_code:#04x}"


# ---------------------------------------------------------------------------
# Public event dataclasses -- stable, ergonomic types for downstream code
# (remapper, macro recorder, capture UI). Never leak raw ctypes structs.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyEvent:
    vk_code: int
    scan_code: int
    up: bool
    extended: bool
    alt_down: bool
    injected: bool
    from_self: bool
    time_ms: int
    name: str = ""


@dataclass(frozen=True)
class MouseButtonEvent:
    button: MouseButton
    up: bool
    x: int
    y: int
    injected: bool
    from_self: bool
    time_ms: int


@dataclass(frozen=True)
class MouseMoveEvent:
    x: int
    y: int
    injected: bool
    from_self: bool
    time_ms: int


@dataclass(frozen=True)
class MouseScrollEvent:
    delta: int
    horizontal: bool
    x: int
    y: int
    injected: bool
    from_self: bool
    time_ms: int


KeyCallback = Callable[[KeyEvent], Optional[bool]]
MouseButtonCallback = Callable[[MouseButtonEvent], Optional[bool]]
MouseMoveCallback = Callable[[MouseMoveEvent], Optional[bool]]
MouseScrollCallback = Callable[[MouseScrollEvent], Optional[bool]]


def _dispatch(callbacks: List[Callable], event) -> bool:
    """Call every callback with `event`; return True if any asked to suppress.

    Exceptions from a callback are swallowed (never allowed to propagate
    out of a ctypes hook callback -- an unhandled exception there can
    crash the process or, worse, wedge the system-wide input hook).
    """
    suppress = False
    for cb in list(callbacks):
        try:
            result = cb(event)
        except Exception:
            import traceback

            traceback.print_exc()
            continue
        if result:
            suppress = True
    return suppress


class HookManager:
    """Installs WH_KEYBOARD_LL / WH_MOUSE_LL and dispatches typed events.

    Runs its own Win32 message pump on a dedicated background thread (hooks
    of this kind are only ever delivered on the thread that installed them),
    so calling start() never blocks the caller's own message loop / main
    thread.
    """

    def __init__(self) -> None:
        self._keyboard_callbacks_down: List[KeyCallback] = []
        self._keyboard_callbacks_up: List[KeyCallback] = []
        self._mouse_button_callbacks: List[MouseButtonCallback] = []
        self._mouse_move_callbacks: List[MouseMoveCallback] = []
        self._scroll_callbacks: List[MouseScrollCallback] = []

        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._started = threading.Event()
        self._start_error: Optional[BaseException] = None

        self._keyboard_hook = None
        self._mouse_hook = None
        # Keep strong references to the ctypes callback trampolines --
        # if these get garbage collected while the hook is installed,
        # Windows will call into freed memory and crash the process.
        self._keyboard_proc = HOOKPROC(self._keyboard_hook_proc)
        self._mouse_proc = HOOKPROC(self._mouse_hook_proc)

    # -- registration ----------------------------------------------------

    def on_key_down(self, callback: KeyCallback) -> KeyCallback:
        self._keyboard_callbacks_down.append(callback)
        return callback

    def on_key_up(self, callback: KeyCallback) -> KeyCallback:
        self._keyboard_callbacks_up.append(callback)
        return callback

    def on_mouse_button(self, callback: MouseButtonCallback) -> MouseButtonCallback:
        self._mouse_button_callbacks.append(callback)
        return callback

    def on_mouse_move(self, callback: MouseMoveCallback) -> MouseMoveCallback:
        self._mouse_move_callbacks.append(callback)
        return callback

    def on_scroll(self, callback: MouseScrollCallback) -> MouseScrollCallback:
        self._scroll_callbacks.append(callback)
        return callback

    def remove_key_down(self, callback: KeyCallback) -> None:
        self._remove(self._keyboard_callbacks_down, callback)

    def remove_key_up(self, callback: KeyCallback) -> None:
        self._remove(self._keyboard_callbacks_up, callback)

    def remove_mouse_button(self, callback: MouseButtonCallback) -> None:
        self._remove(self._mouse_button_callbacks, callback)

    def remove_mouse_move(self, callback: MouseMoveCallback) -> None:
        self._remove(self._mouse_move_callbacks, callback)

    def remove_scroll(self, callback: MouseScrollCallback) -> None:
        self._remove(self._scroll_callbacks, callback)

    @staticmethod
    def _remove(lst: List[Callable], callback: Callable) -> None:
        try:
            lst.remove(callback)
        except ValueError:
            pass

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 5.0) -> None:
        """Install both hooks and start pumping messages on a new thread.

        Blocks until the hooks are confirmed installed (or raises on
        failure) so that a `stop()` immediately after `start()` is always
        safe.
        """
        if self.is_running:
            return

        self._started.clear()
        self._start_error = None
        self._thread = threading.Thread(target=self._thread_main, name="SGO-InputHook", daemon=True)
        self._thread.start()

        if not self._started.wait(timeout):
            raise RuntimeError("Timed out waiting for input hook thread to start")
        if self._start_error is not None:
            err = self._start_error
            self._thread = None
            raise err

    def stop(self, timeout: float = 5.0) -> None:
        """Uninstall the hooks and stop the message pump thread."""
        if not self.is_running or self._thread_id is None:
            return
        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout)
        self._thread = None
        self._thread_id = None

    # -- worker thread -------------------------------------------------------

    def _thread_main(self) -> None:
        try:
            self._thread_id = kernel32.GetCurrentThreadId()

            # Force creation of this thread's message queue before we
            # start relying on GetMessageW / PostThreadMessageW.
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)

            hmod = kernel32.GetModuleHandleW(None)

            self._keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._keyboard_proc, hmod, 0)
            if not self._keyboard_hook:
                raise ctypes.WinError(ctypes.get_last_error())

            self._mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, hmod, 0)
            if not self._mouse_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
                self._keyboard_hook = None
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException as exc:  # noqa: BLE001 - surface to start()
            self._start_error = exc
            self._started.set()
            return

        self._started.set()

        try:
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:  # WM_QUIT
                    break
                if ret == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._keyboard_hook:
                user32.UnhookWindowsHookEx(self._keyboard_hook)
                self._keyboard_hook = None
            if self._mouse_hook:
                user32.UnhookWindowsHookEx(self._mouse_hook)
                self._mouse_hook = None

    # -- hook callbacks ------------------------------------------------------

    def _keyboard_hook_proc(self, nCode: int, wParam: int, lParam: int) -> int:
        try:
            if nCode == HC_ACTION:
                info = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                up = wParam in (WM_KEYUP, WM_SYSKEYUP)
                injected = bool(info.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
                from_self = info.dwExtraInfo == INJECTED_MARKER
                extended = bool(info.flags & LLKHF_EXTENDED)
                event = KeyEvent(
                    vk_code=info.vkCode,
                    scan_code=info.scanCode,
                    up=up,
                    extended=extended,
                    alt_down=bool(info.flags & LLKHF_ALTDOWN),
                    injected=injected,
                    from_self=from_self,
                    time_ms=info.time,
                    name=get_key_name(info.vkCode, info.scanCode, extended),
                )
                callbacks = self._keyboard_callbacks_up if up else self._keyboard_callbacks_down
                if _dispatch(callbacks, event):
                    return 1
        except Exception:
            import traceback

            traceback.print_exc()

        return user32.CallNextHookEx(self._keyboard_hook, nCode, wParam, lParam)

    def _mouse_hook_proc(self, nCode: int, wParam: int, lParam: int) -> int:
        try:
            if nCode == HC_ACTION:
                info = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                injected = bool(info.flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED))
                from_self = info.dwExtraInfo == INJECTED_MARKER
                x, y = info.pt.x, info.pt.y

                if wParam == WM_MOUSEMOVE:
                    event = MouseMoveEvent(x=x, y=y, injected=injected, from_self=from_self, time_ms=info.time)
                    if _dispatch(self._mouse_move_callbacks, event):
                        return 1

                elif wParam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    delta = ctypes.c_short(info.mouseData >> 16).value
                    event = MouseScrollEvent(
                        delta=delta,
                        horizontal=(wParam == WM_MOUSEHWHEEL),
                        x=x,
                        y=y,
                        injected=injected,
                        from_self=from_self,
                        time_ms=info.time,
                    )
                    if _dispatch(self._scroll_callbacks, event):
                        return 1

                else:
                    button = _BUTTON_FROM_MSG.get(wParam)
                    if button is None and wParam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                        xbtn = (info.mouseData >> 16) & 0xFFFF
                        button = MouseButton.X1 if xbtn == XBUTTON1 else MouseButton.X2
                    if button is not None:
                        up = wParam in (WM_LBUTTONUP, WM_RBUTTONUP, WM_MBUTTONUP, WM_XBUTTONUP)
                        event = MouseButtonEvent(
                            button=button,
                            up=up,
                            x=x,
                            y=y,
                            injected=injected,
                            from_self=from_self,
                            time_ms=info.time,
                        )
                        if _dispatch(self._mouse_button_callbacks, event):
                            return 1
        except Exception:
            import traceback

            traceback.print_exc()

        return user32.CallNextHookEx(self._mouse_hook, nCode, wParam, lParam)


_BUTTON_FROM_MSG = {
    WM_LBUTTONDOWN: MouseButton.LEFT,
    WM_LBUTTONUP: MouseButton.LEFT,
    WM_RBUTTONDOWN: MouseButton.RIGHT,
    WM_RBUTTONUP: MouseButton.RIGHT,
    WM_MBUTTONDOWN: MouseButton.MIDDLE,
    WM_MBUTTONUP: MouseButton.MIDDLE,
}
