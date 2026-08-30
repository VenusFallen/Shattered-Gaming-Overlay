"""key_capture.py -- shared "press a key to bind" support for the Companion
window (Remapper source/destination binds, Macro trigger binds).

UI convenience only: observes the next physical key/button press via
`input_hooks.py` and hands it back to a bind button. Never matches,
dispatches, remaps, or injects -- and never suppresses the observed key, so
it still reaches the rest of the system normally.

`KeyBind` only carries a single `vk_code` int, so mouse buttons (which
`input_hooks.py` reports as a `MouseButton` enum, not a vk code) are
represented with Windows' own reserved virtual-key constants for them --
VK_LBUTTON=0x01, VK_RBUTTON=0x02, VK_MBUTTON=0x04, VK_XBUTTON1=0x05,
VK_XBUTTON2=0x06 -- the same scheme AutoHotkey uses. These never collide with
real keyboard vk codes. remapper.py/macro_engine.py use
`is_mouse_vk`/`keybind_vk_to_mouse_button` to tell which a bound
source/destination/trigger is.
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
    `vk_code is None` means unbound."""

    vk_code: Optional[int] = None
    name: str = "Unbound"

    @property
    def is_bound(self) -> bool:
        return self.vk_code is not None


UNBOUND = KeyBind()

# Mouse-button pseudo-vk scheme -- see module docstring.

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

    Started lazily on first bind-button click, left running afterward.
    `_pending` is written under `_lock` from the hook's background thread;
    the render thread polls it once per frame via `poll_result()`.
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
        # Only the down edge counts as "pressed to bind".
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
