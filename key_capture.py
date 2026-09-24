"""Shared "press a key to bind" support for the Companion window (Remapper source/destination binds,
Macro trigger binds). UI convenience only: observes the next physical key/button press via `input_hooks.py`
and hands it back to a bind button -- never matches, dispatches, remaps, injects, or suppresses.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

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

# KeyBind only carries a vk_code int, so mouse buttons use Windows' own reserved virtual-key
# constants (same scheme AutoHotkey uses) -- these never collide with real keyboard vk codes.
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
    """Process-wide singleton owning the one hook used for bind capture. Started lazily on first bind-button
    click, left running afterward. Also supports chord capture (hold modifiers, tap a final key, release ->
    resolved as (modifiers, trigger) in one gesture), reusing the same hook since only one of plain/chord
    capture is ever armed at a time."""

    def __init__(self) -> None:
        self._hook: Optional[HookManager] = None
        self._lock = threading.Lock()
        self._pending: Optional[KeyBind] = None
        self._active = False
        # Chord capture state -- see begin_chord_capture()'s own docstring.
        self._chord_active = False
        self._chord_pressed: List[KeyBind] = []  # press order, de-duplicated
        self._chord_pending: Optional[Tuple[Tuple[KeyBind, ...], KeyBind]] = None

    def _ensure_hook_running(self) -> None:
        if self._hook is None:
            self._hook = HookManager()
            self._hook.on_key_down(self._on_key_down)
            self._hook.on_key_up(self._on_key_up)
            self._hook.on_mouse_button(self._on_mouse_button)
        if not self._hook.is_running:
            self._hook.start()

    def _on_key_down(self, event: KeyEvent) -> Optional[bool]:
        if event.from_self:
            return None
        with self._lock:
            if self._chord_active:
                name = event.name or f"VK_{event.vk_code:#04x}"
                self._chord_add(KeyBind(vk_code=event.vk_code, name=name))
                return None
            if not self._active:
                return None
            name = event.name or f"VK_{event.vk_code:#04x}"
            self._pending = KeyBind(vk_code=event.vk_code, name=name)
            self._active = False
        return None  # never suppress -- passive observation only

    def _on_key_up(self, event: KeyEvent) -> Optional[bool]:
        if event.from_self:
            return None
        with self._lock:
            if self._chord_active:
                self._chord_maybe_finalize()
        return None  # never suppress -- passive observation only

    def _on_mouse_button(self, event: MouseButtonEvent) -> Optional[bool]:
        if event.from_self:
            return None
        with self._lock:
            if self._chord_active:
                if event.up:
                    self._chord_maybe_finalize()
                else:
                    self._chord_add(mouse_button_to_keybind(event.button))
                return None
            # Only the down edge counts as "pressed to bind" for plain capture.
            if not self._active or event.up:
                return None
            self._pending = mouse_button_to_keybind(event.button)
            self._active = False
        return None  # never suppress -- passive observation only

    def _chord_add(self, kb: KeyBind) -> None:
        # Called under self._lock. Ignore OS key-repeat re-fires of an already-held key.
        if kb not in self._chord_pressed:
            self._chord_pressed.append(kb)

    def _chord_maybe_finalize(self) -> None:
        # Called under self._lock, on the first release since begin_chord_capture() (either key can release
        # first). The LAST key pressed becomes the trigger, everything before it becomes modifiers.
        if not self._chord_pressed:
            return  # a release with nothing ever pressed -- ignore, never crash
        *modifiers, trigger = self._chord_pressed
        self._chord_pending = (tuple(modifiers), trigger)
        self._chord_active = False
        self._chord_pressed = []

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

    def begin_chord_capture(self) -> None:
        """Start listening for a held-modifiers-plus-final-key gesture, e.g. hold Shift, press R, release either -> modifiers=(Shift,), trigger=R."""
        self._ensure_hook_running()
        with self._lock:
            self._chord_pending = None
            self._chord_pressed = []
            self._chord_active = True

    def cancel_chord_capture(self) -> None:
        with self._lock:
            self._chord_active = False
            self._chord_pressed = []
            self._chord_pending = None

    @property
    def is_chord_capturing(self) -> bool:
        with self._lock:
            return self._chord_active

    def poll_chord_result(self) -> Optional[Tuple[Tuple[KeyBind, ...], KeyBind]]:
        """Call once per frame while chord-capturing. Returns and clears
        (modifiers, trigger) once available, else None."""
        with self._lock:
            result = self._chord_pending
            self._chord_pending = None
            return result

    def shutdown(self) -> None:
        """Uninstall the hook if it was ever started; safe no-op otherwise."""
        with self._lock:
            self._active = False
            self._pending = None
            self._chord_active = False
            self._chord_pressed = []
            self._chord_pending = None
        if self._hook is not None and self._hook.is_running:
            self._hook.stop()


# One shared instance -- there is only ever one "currently capturing" bind
# button across the whole Companion window at a time.
capture_service = KeyCaptureService()
