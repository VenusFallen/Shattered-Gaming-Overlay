"""key_capture.py -- shared "press a key to bind" support for the Companion
window (Remapper source/destination binds, Macro trigger binds).

This is a UI convenience only. It uses the engine-side `input_hooks.py` (pure
user-mode SetWindowsHookEx capture -- no driver, see that module's docstring)
to observe the *next* physical key press so a bind button can show it back to
the user. It never matches, dispatches, remaps, or injects anything -- that's
the future remapper/macro engine's job, not this file's. It also never
suppresses the observed key (the hook callback always returns None), so
pressing a key to bind it still reaches the rest of the system normally.

Mouse-button binds
-------------------
`KeyBind` only ever carries a single `vk_code` int, so mouse buttons (which
`input_hooks.py` reports as a `MouseButton` enum via `WH_MOUSE_LL`, not a vk
code) are represented using Windows' own real, reserved virtual-key
constants for them -- VK_LBUTTON=0x01, VK_RBUTTON=0x02, VK_MBUTTON=0x04,
VK_XBUTTON1=0x05, VK_XBUTTON2=0x06. These codes are never emitted by
WH_KEYBOARD_LL for a physical keyboard key, so there is no collision between
a mouse-button KeyBind and a keyboard-key KeyBind sharing this same int
field. This is the same scheme AutoHotkey uses for LButton/RButton binds.
remapper.py and macro_engine.py both key off this mapping (via
`is_mouse_vk`/`keybind_vk_to_mouse_button`) to know whether a bound
source/destination/trigger should be matched/injected as a keyboard key or a
mouse button.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from input_hooks import HookManager, KeyEvent, MouseButtonEvent
from input_inject import MouseButton


@dataclass(frozen=True)
class KeyBind:
    """A single bound key: a vk_code plus a human-readable display name.

    `vk_code is None` means unbound.
    """

    vk_code: Optional[int] = None
    name: str = "Unbound"

    @property
    def is_bound(self) -> bool:
        return self.vk_code is not None


UNBOUND = KeyBind()

# ---------------------------------------------------------------------------
# Mouse-button pseudo-vk scheme -- see module docstring.
# ---------------------------------------------------------------------------

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06

_MOUSE_VK_BY_BUTTON = {
    MouseButton.LEFT: VK_LBUTTON,
    MouseButton.RIGHT: VK_RBUTTON,
    MouseButton.MIDDLE: VK_MBUTTON,
    MouseButton.X1: VK_XBUTTON1,
    MouseButton.X2: VK_XBUTTON2,
}
_BUTTON_BY_MOUSE_VK = {vk: btn for btn, vk in _MOUSE_VK_BY_BUTTON.items()}
_MOUSE_VK_NAMES = {
    VK_LBUTTON: "Mouse Left",
    VK_RBUTTON: "Mouse Right",
    VK_MBUTTON: "Mouse Middle",
    VK_XBUTTON1: "Mouse 4",
    VK_XBUTTON2: "Mouse 5",
}


def mouse_button_to_vk(button: MouseButton) -> int:
    return _MOUSE_VK_BY_BUTTON[button]


def mouse_button_to_keybind(button: MouseButton) -> KeyBind:
    vk = _MOUSE_VK_BY_BUTTON[button]
    return KeyBind(vk_code=vk, name=_MOUSE_VK_NAMES[vk])


def keybind_vk_to_mouse_button(vk_code: Optional[int]) -> Optional[MouseButton]:
    if vk_code is None:
        return None
    return _BUTTON_BY_MOUSE_VK.get(vk_code)


def is_mouse_vk(vk_code: Optional[int]) -> bool:
    return vk_code is not None and vk_code in _BUTTON_BY_MOUSE_VK


class KeyCaptureService:
    """Process-wide singleton owning the one hook used for bind capture.

    Started lazily on first use (no hook is installed until the user
    actually clicks a bind button) and left running afterward -- cheap to
    keep alive, and avoids install/uninstall churn on every bind click.

    Thread-safety: `input_hooks.HookManager` fires its callback on its own
    background message-pump thread. We only ever write a single KeyBind
    behind a lock from that callback; the render thread polls it once per
    frame via `poll_result()`. This mirrors the lock-guarded /
    simple-atomic-assignment pattern used for HUD state hand-off elsewhere
    in this project, applied here to the Companion window's own capture
    bridge.
    """

    def __init__(self) -> None:
        self._hook: Optional[HookManager] = None
        self._lock = threading.Lock()
        self._pending: Optional[KeyBind] = None
        self._active = False

    def _ensure_hook_running(self) -> None:
        if self._hook is None:
            self._hook = HookManager()
            self._hook.on_key_down(self._on_key_down)
            self._hook.on_mouse_button(self._on_mouse_button)
        if not self._hook.is_running:
            self._hook.start()

    def _on_key_down(self, event: KeyEvent) -> Optional[bool]:
        # Ignore our own injected input (there isn't any here, but stay
        # consistent with the from_self convention) and anything observed
        # while no capture is in progress.
        if event.from_self:
            return None
        with self._lock:
            if not self._active:
                return None
            name = event.name or f"VK_{event.vk_code:#04x}"
            self._pending = KeyBind(vk_code=event.vk_code, name=name)
            self._active = False
        return None  # never suppress -- passive observation only

    def _on_mouse_button(self, event: MouseButtonEvent) -> Optional[bool]:
        # Only the down edge counts as "pressed to bind" -- mirrors
        # _on_key_down only firing on WH_KEYBOARD_LL keydown, and avoids
        # capturing the button-up half of the same physical click.
        if event.from_self or event.up:
            return None
        with self._lock:
            if not self._active:
                return None
            self._pending = mouse_button_to_keybind(event.button)
            self._active = False
        return None  # never suppress -- passive observation only

    def begin_capture(self) -> None:
        """Start listening for the next physical key press."""
        self._ensure_hook_running()
        with self._lock:
            self._pending = None
            self._active = True

    def cancel_capture(self) -> None:
        with self._lock:
            self._active = False
            self._pending = None

    @property
    def is_capturing(self) -> bool:
        with self._lock:
            return self._active

    def poll_result(self) -> Optional[KeyBind]:
        """Call once per frame while capturing. Returns and clears the
        captured bind once available, else None."""
        with self._lock:
            result = self._pending
            self._pending = None
            return result

    def shutdown(self) -> None:
        """Uninstall the hook if it was ever started. Safe to call even if
        capture was never used (no-op in that case)."""
        with self._lock:
            self._active = False
            self._pending = None
        if self._hook is not None and self._hook.is_running:
            self._hook.stop()


# One shared instance -- there is only ever one "currently capturing" bind
# button across the whole Companion window at a time.
capture_service = KeyCaptureService()
