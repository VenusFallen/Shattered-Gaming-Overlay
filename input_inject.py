"""input_inject.py -- pure user-mode input injection via the Win32
`SendInput` API. This is the only way this project synthesizes input -- no
driver, no ViGEm/virtual-controller HID emulation, no game-process memory
writes.

Every injected event is tagged with `INJECTED_MARKER` in `dwExtraInfo`;
`input_hooks.py` reads it back out so consumers can tell their own injected
input apart from real physical input or another tool's injection.

No sleeps or jitter logic here -- humanized delay timing is macro_engine.py's
concern; this module only ever fires a single instantaneous SendInput call.
Has no dependency on input_hooks.py, so it stays importable/testable standalone.
"""

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
# Arbitrary but stable 32-bit tag. Only needs to be unlikely to collide with
# whatever other software on the machine happens to set in dwExtraInfo.
INJECTED_MARKER = 0x53474F31  # "SGO1"


class MouseButton(Enum):
    LEFT = auto()
    RIGHT = auto()
    MIDDLE = auto()
    X1 = auto()
    X2 = auto()


# ---------------------------------------------------------------------------
# ctypes structures matching the real Win32 INPUT / MOUSEINPUT / KEYBDINPUT
# layout. Field types (c_long for signed dx/dy, c_size_t standing in for
# ULONG_PTR, which ctypes.wintypes does not expose) matter for correct
# struct size/alignment on 64-bit -- get these wrong and SendInput silently
# reads garbage or the union straddles wrong.
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

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

WHEEL_DELTA = 120

# Best-effort set of virtual-key codes that require KEYEVENTF_EXTENDEDKEY
# when injected as scan codes (arrows, ins/del/home/end/pgup/pgdn, the
# right-hand ctrl/alt, numlock, the numpad divide key, and the windows
# keys). Not exhaustive, but covers everything a remapper/macro user is
# likely to bind.
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
    """Synthesize a mouse wheel event.

    `delta` is in the same units as Windows' WHEEL_DELTA (120 = one
    physical notch). Positive = forward/up (or right, if horizontal).
    """
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
