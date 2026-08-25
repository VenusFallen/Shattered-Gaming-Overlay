"""key_capture.py -- shared "press a key to bind" support for the Companion
window (Remapper source/destination binds, Macro trigger binds).

This is a UI convenience only. It uses engine-agent's `input_hooks.py` (pure
user-mode SetWindowsHookEx capture -- no driver, see that module's docstring)
to observe the *next* physical key press so a bind button can show it back to
the user. It never matches, dispatches, remaps, or injects anything -- that's
the future remapper/macro engine's job, not this file's. It also never
suppresses the observed key (the hook callback always returns None), so
pressing a key to bind it still reaches the rest of the system normally.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from input_hooks import HookManager, KeyEvent


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


class KeyCaptureService:
    """Process-wide singleton owning the one hook used for bind capture.

    Started lazily on first use (no hook is installed until the user
    actually clicks a bind button) and left running afterward -- cheap to
    keep alive, and avoids install/uninstall churn on every bind click.

    Thread-safety: `input_hooks.HookManager` fires its callback on its own
    background message-pump thread. We only ever write a single KeyBind
    behind a lock from that callback; the render thread polls it once per
    frame via `poll_result()`. This mirrors the lock-guarded /
    simple-atomic-assignment guidance engine-agent.md gives for HUD state
    hand-off, applied here to the Companion window's own capture bridge.
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
