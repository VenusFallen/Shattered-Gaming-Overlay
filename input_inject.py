"""Pure user-mode input injection via the Win32 SendInput API -- no driver,
no ViGEm/virtual-controller emulation, no game-process memory writes.
Injected events are tagged with INJECTED_MARKER so input_hooks.py can tell
them apart from real physical input."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from enum import Enum, auto

if not sys.platform.startswith("win"):
    raise ImportError("input_inject.py is Windows-only (uses SendInput via ctypes).")

user32 = ctypes.WinDLL("user32", use_last_error=True)

# ---------------------------------------------------------------------------
# Shared marker so input_hooks.py can recognize input WE injected.
# ---------------------------------------------------------------------------
INJECTED_MARKER = 0x53474F31  # "SGO1" -- arbitrary, just needs to be unlikely to collide


class MouseButton(Enum):
    LEFT = auto()
    RIGHT = auto()
    MIDDLE = auto()
    X1 = auto()
    X2 = auto()


# ---------------------------------------------------------------------------
# ctypes structures matching the real Win32 INPUT / MOUSEINPUT / KEYBDINPUT
# layout -- field types matter here for correct struct size/alignment on 64-bit.
# ---------------------------------------------------------------------------

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT

MAPVK_VK_TO_VSC = 0

# --- keyboard flags ---
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

# --- mouse flags ---
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

WHEEL_DELTA = 120

# --- cursor position ---
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int

# Virtual-key codes that require KEYEVENTF_EXTENDEDKEY when injected as scan codes. Not exhaustive, covers common binds.
_EXTENDED_VKS = frozenset(
    {
        0x21,  # VK_PRIOR (Page Up)
        0x22,  # VK_NEXT (Page Down)
        0x23,  # VK_END
        0x24,  # VK_HOME
        0x25,  # VK_LEFT
        0x26,  # VK_UP
        0x27,  # VK_RIGHT
        0x28,  # VK_DOWN
        0x2D,  # VK_INSERT
        0x2E,  # VK_DELETE
        0x5B,  # VK_LWIN
        0x5C,  # VK_RWIN
        0x5D,  # VK_APPS
        0x6F,  # VK_DIVIDE (numpad /)
        0x90,  # VK_NUMLOCK
        0xA3,  # VK_RCONTROL
        0xA5,  # VK_RMENU (right alt)
    }
)


def _is_extended_key(vk_code: int) -> bool:
    return vk_code in _EXTENDED_VKS


def _send(*inputs: INPUT) -> int:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        raise ctypes.WinError(err)
    return sent


def send_key(vk_code: int, key_up: bool = False) -> None:
    """Synthesize a single key down (default) or key up event for `vk_code`.
    Sets both the vk and its scan code (some DirectInput-based apps key off
    scan code, not vk) and auto-sets KEYEVENTF_EXTENDEDKEY where needed."""
    scan = user32.MapVirtualKeyW(vk_code, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_KEYUP if key_up else 0
    if _is_extended_key(vk_code):
        flags |= KEYEVENTF_EXTENDEDKEY

    ki = KEYBDINPUT(
        wVk=vk_code,
        wScan=scan,
        dwFlags=flags,
        time=0,
        dwExtraInfo=INJECTED_MARKER,
    )
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = ki
    _send(inp)


_BUTTON_DOWN_FLAGS = {
    MouseButton.LEFT: MOUSEEVENTF_LEFTDOWN,
    MouseButton.RIGHT: MOUSEEVENTF_RIGHTDOWN,
    MouseButton.MIDDLE: MOUSEEVENTF_MIDDLEDOWN,
    MouseButton.X1: MOUSEEVENTF_XDOWN,
    MouseButton.X2: MOUSEEVENTF_XDOWN,
}
_BUTTON_UP_FLAGS = {
    MouseButton.LEFT: MOUSEEVENTF_LEFTUP,
    MouseButton.RIGHT: MOUSEEVENTF_RIGHTUP,
    MouseButton.MIDDLE: MOUSEEVENTF_MIDDLEUP,
    MouseButton.X1: MOUSEEVENTF_XUP,
    MouseButton.X2: MOUSEEVENTF_XUP,
}
_XBUTTON_DATA = {
    MouseButton.X1: XBUTTON1,
    MouseButton.X2: XBUTTON2,
}


def send_mouse_button(button: MouseButton, up: bool = False) -> None:
    """Synthesize a mouse button down (default) or up event."""
    flags = _BUTTON_UP_FLAGS[button] if up else _BUTTON_DOWN_FLAGS[button]
    mouse_data = _XBUTTON_DATA.get(button, 0)

    mi = MOUSEINPUT(
        dx=0,
        dy=0,
        mouseData=mouse_data,
        dwFlags=flags,
        time=0,
        dwExtraInfo=INJECTED_MARKER,
    )
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = mi
    _send(inp)


def send_scroll(delta: int, horizontal: bool = False) -> None:
    """Synthesize a mouse wheel event. delta is in WHEEL_DELTA units (120 = one notch); positive = forward/up (or right, if horizontal)."""
    flags = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    mi = MOUSEINPUT(
        dx=0,
        dy=0,
        mouseData=delta & 0xFFFFFFFF,  # DWORD field, but interpreted as signed
        dwFlags=flags,
        time=0,
        dwExtraInfo=INJECTED_MARKER,
    )
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = mi
    _send(inp)


def get_cursor_pos() -> tuple[int, int]:
    """Current cursor position in virtual-desktop pixel coordinates."""
    pt = POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return pt.x, pt.y


def _normalize_absolute(x: int, y: int) -> tuple[int, int]:
    """Converts a virtual-desktop pixel coordinate to SendInput's 0-65535 absolute space, scoped to the full virtual desktop (all monitors)."""
    origin_x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    origin_y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    norm_x = int((x - origin_x) * 65536 / width) if width else 0
    norm_y = int((y - origin_y) * 65536 / height) if height else 0
    return norm_x, norm_y


def send_mouse_move_absolute(x: int, y: int) -> None:
    """Moves the cursor to an exact virtual-desktop pixel coordinate. One instantaneous jump, not a tracked/animated glide."""
    norm_x, norm_y = _normalize_absolute(x, y)
    mi = MOUSEINPUT(
        dx=norm_x,
        dy=norm_y,
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0,
        dwExtraInfo=INJECTED_MARKER,
    )
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = mi
    _send(inp)


def send_mouse_move_relative(dx: int, dy: int) -> None:
    """Nudges the cursor by a fixed (dx, dy) offset -- e.g. a Macro's
    Move-By step. One instantaneous call, no tracking/animation of its own."""
    mi = MOUSEINPUT(
        dx=dx,
        dy=dy,
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE,
        time=0,
        dwExtraInfo=INJECTED_MARKER,
    )
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = mi
    _send(inp)


def send_mouse_move_by_step(dx: int, dy_up_positive: int) -> None:
    """Same as send_mouse_move_relative(), except dy is "positive = up" (the human-readable convention), not SendInput's raw positive-=-down."""
    send_mouse_move_relative(dx, -dy_up_positive)


# ---------------------------------------------------------------------------
# Process timer resolution -- Windows defaults to a ~15.6ms scheduler tick, so
# threading.Event.wait()/time.sleep() calls shorter than that (e.g. macro_engine.py's
# per-substep glide waits) round up to it instead of the requested duration.
# ---------------------------------------------------------------------------

winmm = ctypes.WinDLL("winmm")
winmm.timeBeginPeriod.argtypes = (ctypes.c_uint,)
winmm.timeBeginPeriod.restype = ctypes.c_uint
winmm.timeEndPeriod.argtypes = (ctypes.c_uint,)
winmm.timeEndPeriod.restype = ctypes.c_uint

_TIMER_RESOLUTION_MS = 1
_TIMERR_NOERROR = 0
_timer_resolution_raised = False  # tracks whether timeBeginPeriod actually succeeded, so restore only matches a real begin


def raise_timer_resolution() -> None:
    """Requests a 1ms system timer resolution for this process's whole
    lifetime, so short waits actually resolve close to what was asked
    instead of rounding up to the OS default tick. Call once at startup;
    pair with restore_timer_resolution() at shutdown. Affects every thread
    in the process, not just input injection -- this lives here because
    the substep glide timing it exists for is input_inject's own concern."""
    global _timer_resolution_raised
    if winmm.timeBeginPeriod(_TIMER_RESOLUTION_MS) == _TIMERR_NOERROR:
        _timer_resolution_raised = True


def restore_timer_resolution() -> None:
    """Releases the request made by raise_timer_resolution(), if it succeeded. timeEndPeriod must be called
    with the exact value a prior timeBeginPeriod used -- calling it unpaired is a documented error, not a no-op."""
    global _timer_resolution_raised
    if _timer_resolution_raised:
        winmm.timeEndPeriod(_TIMER_RESOLUTION_MS)
        _timer_resolution_raised = False
