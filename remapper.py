"""remapper.py -- matches live keyboard/mouse-button events against
`AppState.remapper.entries` (source -> destination), suppresses the matched
physical event, and injects the destination via `input_inject`. Pure
user-mode: `input_hooks.HookManager` for capture, `input_inject.send_key` /
`send_mouse_button` for injection -- no driver, no ViGEm, no game-process
access.

A remap also registers as its destination for the rest of the app's own
trigger matching (so a remapped key correctly arms macros bound to that
destination), not just re-emitting the raw OS event. Every physical
keyboard/mouse-button event is published as a normalized
`EffectiveInputEvent`: destination identity + `from_remap=True` if it matched
an enabled remap entry, original identity + `from_remap=False` otherwise.
`macro_engine.py` subscribes via `add_effective_listener` instead of
installing its own hook, so there's exactly one place a remap's destination
becomes visible to the rest of the app's trigger matching.

Window-filter gating: when a process is targeted
(`WindowSelectState.selected` set), match/inject goes inert the instant that
process loses OS foreground focus and resumes the instant it regains it.
Applied once, centrally, in `_handle()` -- while closed, events are neither
remapped/suppressed nor published, which is what makes the macro engine go
inert alongside the remapper without its own focus-tracking logic. The gate
is evaluated fresh on every event via `window_select.cached_foreground_pid()`,
not a value cached in `update_snapshot()` -- Hello ImGui's `show_gui`
callback doesn't fire while the Companion window is minimized, which would
freeze a cached gate at whatever it was the instant before minimizing.
`update_snapshot()` only hands off the target pid itself (not focus-sensitive);
`_handle()` checks focus live against window_select's own polling thread.

This module owns its own `HookManager`, separate from
`key_capture.capture_service`'s: that one is momentary and never suppresses
(passive bind-capture); this one is always-on for the app's lifetime and
actively suppresses matched source events. Windows chains multiple
WH_KEYBOARD_LL/WH_MOUSE_LL hooks natively, so running both is not redundant.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import input_inject
import window_select
from input_hooks import HookManager, KeyEvent, MouseButtonEvent
from key_capture import KeyBind, is_mouse_vk, keybind_vk_to_mouse_button, mouse_button_to_vk

# Avoid a hard import-time dependency on app_state beyond type hints, so this
# module stays importable/testable without a live hook.
try:  # pragma: no cover - only used for type hints
    from app_state import RemapperState, WindowSelectState
except Exception:  # pragma: no cover
    RemapperState = object  # type: ignore
    WindowSelectState = object  # type: ignore


@dataclass(frozen=True)
class EffectiveInputEvent:
    """A normalized post-remap identity: a remap's destination if the
    physical event matched an enabled entry, else the original physical
    identity. `vk_code` uses key_capture.py's keyboard-vk / mouse-pseudo-vk
    scheme uniformly, so consumers don't need to care whether it originated
    from a keyboard or a mouse button."""

    vk_code: int
    up: bool
    name: str = ""
    from_remap: bool = False
    time_ms: int = 0


EffectiveListener = Callable[[EffectiveInputEvent], None]


class RemapperEngine:
    """Always-on remap matcher/injector. Public thread-safe methods
    (callable from the Companion window's own thread):

        start() / stop()
        update_snapshot(remapper_state, window_select_state)
        add_effective_listener(callback)

    Everything else runs on the hook's own background thread -- never read
    AppState.remapper / AppState.window_select directly from there;
    update_snapshot() is the only bridge.
    """

    def __init__(self) -> None:
        self._hook = HookManager()
        self._lock = threading.Lock()

        # Written only by update_snapshot() (Companion thread), read only by
        # _handle() (hook thread). `_target_pid` is None when no process is
        # targeted (gate always open); the has-focus check itself is
        # deliberately NOT part of this snapshot -- see module docstring.
        self._mapping: Dict[int, KeyBind] = {}
        self._target_pid: Optional[int] = None

        # Hook-thread-only: tracks which source identities are currently
        # suppressed-and-remapped so a key held across a live config edit
        # still releases the correct destination.
        self._active_remaps: Dict[int, KeyBind] = {}

        self._listeners: List[EffectiveListener] = []
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._hook.on_key_down(self._on_key)
        self._hook.on_key_up(self._on_key)
        self._hook.on_mouse_button(self._on_mouse_button)
        self._hook.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._hook.stop()
        self._started = False
        with self._lock:
            self._active_remaps.clear()

    @property
    def is_running(self) -> bool:
        return self._hook.is_running

    def add_effective_listener(self, callback: EffectiveListener) -> None:
        self._listeners.append(callback)

    def update_snapshot(self, remapper_state: "RemapperState", window_select_state: "WindowSelectState") -> None:
        """Call once per Companion-window frame. KeyBind is a frozen
        dataclass, so storing destination references directly is safe to
        read from the hook thread without copying further.

        Only hands off *which* pid is targeted (or None), not whether it
        currently has focus -- see module docstring's "Window-filter gating"
        section for why that check is evaluated live in `_handle()` instead
        of snapshotted here."""
        mapping: Dict[int, KeyBind] = {}
        for entry in remapper_state.entries:
            if not entry.enabled:
                continue
            if not entry.source.is_bound or not entry.destination.is_bound:
                continue
            mapping.setdefault(entry.source.vk_code, entry.destination)

        selected = window_select_state.selected
        target_pid = selected.pid if selected is not None else None

        with self._lock:
            self._mapping = mapping
            self._target_pid = target_pid

    # ------------------------------------------------------------------
    # Hook callbacks (hook thread)
    # ------------------------------------------------------------------

    def _on_key(self, event: KeyEvent) -> Optional[bool]:
        if event.from_self:
            return None
        return self._handle(event.vk_code, event.up, event.name, event.time_ms)

    def _on_mouse_button(self, event: MouseButtonEvent) -> Optional[bool]:
        if event.from_self:
            return None
        vk = mouse_button_to_vk(event.button)
        return self._handle(vk, event.up, "", event.time_ms)

    def _handle(self, vk: int, up: bool, name: str, time_ms: int) -> Optional[bool]:
        with self._lock:
            mapping = self._mapping
            target_pid = self._target_pid

        # Evaluated live, every event -- see module docstring's
        # "Window-filter gating" section.
        gate_open = target_pid is None or window_select.cached_foreground_pid() == target_pid

        if not gate_open:
            # Inert: targeted process doesn't currently have focus. Input
            # passes through untouched -- no remap, no macro-trigger visibility.
            return None

        dest = mapping.get(vk)

        if dest is None:
            # Not currently a remap source. Still check for a stale-but-active
            # remap on release, in case entries changed while the key was held.
            active_dest = self._active_remaps.pop(vk, None) if up else None
            if active_dest is None:
                self._publish(EffectiveInputEvent(vk_code=vk, up=up, name=name, from_remap=False, time_ms=time_ms))
                return None
            dest = active_dest
        else:
            if up:
                self._active_remaps.pop(vk, None)
            else:
                self._active_remaps[vk] = dest

        self._inject(dest, up)
        self._publish(EffectiveInputEvent(vk_code=dest.vk_code, up=up, name=dest.name, from_remap=True, time_ms=time_ms))
        return True  # suppress the original physical event

    @staticmethod
    def _inject(dest: KeyBind, up: bool) -> None:
        if is_mouse_vk(dest.vk_code):
            button = keybind_vk_to_mouse_button(dest.vk_code)
            if button is not None:
                input_inject.send_mouse_button(button, up=up)
        else:
            input_inject.send_key(dest.vk_code, key_up=up)

    def _publish(self, event: EffectiveInputEvent) -> None:
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:
                traceback.print_exc()


# ---------------------------------------------------------------------------
# Process-wide singleton -- main.py starts/stops this alongside the
# Companion window's own lifecycle (hud_overlay/capture_service pattern).
# ---------------------------------------------------------------------------

remapper_engine = RemapperEngine()
