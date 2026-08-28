"""remapper.py -- matches live keyboard/mouse-button events against
`AppState.remapper.entries` (source -> destination), suppresses the matched
physical event, and injects the destination via `input_inject`. Pure
user-mode: built entirely on `input_hooks.HookManager` (WH_KEYBOARD_LL /
WH_MOUSE_LL) for capture and `input_inject.send_key` /
`input_inject.send_mouse_button` for injection -- no driver, no ViGEm, no
game-process access.

Effective event stream
-----------------------
By design, a remap also registers as its destination for the rest of the
app's own trigger matching (so a remapped key correctly arms macros/toggles
bound to that destination), not just re-emitting the raw OS event. Rather
than have macro_engine.py independently re-match raw hook
events (and duplicate the remap-lookup logic), this module publishes a
normalized `EffectiveInputEvent` for every physical keyboard/mouse-button
event it observes:

  - If the event matched an enabled remap entry, the published event carries
    the *destination* identity (`from_remap=True`) -- the same identity that
    was just injected.
  - If it didn't match anything, the published event carries the original
    physical identity unchanged (`from_remap=False`).

`macro_engine.py` subscribes to this stream via `add_effective_listener`
instead of installing its own hook, so there is exactly one place a remap's
destination becomes visible to the rest of the app's trigger matching.

Window-filter gating
---------------------
By design, when a process is targeted (`WindowSelectState.selected` is set)
the match/inject step (not hook installation, which stays up for the whole
app lifetime) goes inert the instant that process loses OS foreground focus,
and resumes the instant it regains it. That gate is applied once, centrally,
in `_handle()` below -- while closed, physical events are neither
remapped/suppressed NOR published to the effective-event stream, which is
what makes the Macro engine go inert together with the Remapper without
needing its own separate focus-tracking logic.

Hook-sharing decision (vs. key_capture.py)
--------------------------------------------
This module owns its OWN `HookManager` instance, separate from
`key_capture.capture_service`'s. They serve genuinely different purposes
with different lifecycles and suppression semantics:

  - `key_capture.capture_service`'s hook is momentary (installed lazily on
    first bind-button click, left running but logically idle otherwise) and
    NEVER suppresses -- it only ever observes the next physical key/button
    press for the "press a key to bind" UI widget.
  - This module's hook is always-on for the whole app lifetime (installed
    at startup alongside hud_overlay/capture_service, per this module's own
    "hooks stay installed throughout" rule) and actively suppresses matched
    source events every time a remap fires.

Windows natively chains multiple WH_KEYBOARD_LL/WH_MOUSE_LL hooks together,
so running two here is not "redundant" in the naive sense -- that concern
is about accidentally doing the *same* job twice. Folding both
sets of very different lifecycle/suppression semantics into one HookManager
surface would be more error-prone than keeping the always-on, suppressing
remap hook cleanly separate from the transient, passive bind-capture hook.
"""

from __future__ import annotations

import threading
import traceback
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import input_inject
from input_hooks import HookManager, KeyEvent, MouseButtonEvent
from key_capture import KeyBind, is_mouse_vk, keybind_vk_to_mouse_button, mouse_button_to_vk

# Avoid a hard import-time dependency on app_state's dataclasses beyond type
# hints -- keeps this module importable/unit-testable without a live hook,
# per input_hooks.py's own "capture and injection cleanly separated" goal.
try:  # pragma: no cover - only used for type hints
    from app_state import RemapperState, WindowSelectState
except Exception:  # pragma: no cover
    RemapperState = object  # type: ignore
    WindowSelectState = object  # type: ignore


@dataclass(frozen=True)
class EffectiveInputEvent:
    """A normalized post-remap identity: either a remap's destination (if
    the physical event matched an enabled remap entry) or the original
    physical identity (if it didn't). `vk_code` uses key_capture.py's
    keyboard-vk / mouse-pseudo-vk scheme uniformly, so consumers (currently
    only macro_engine.py) never need to care whether it originated from a
    keyboard or a mouse button.
    """

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

    Everything else runs on the hook's own background message-pump thread
    (see input_hooks.HookManager) -- never read AppState.remapper /
    AppState.window_select directly from there; update_snapshot() is the
    only bridge, mirroring hud_overlay.py's lock-guarded-snapshot pattern.
    """

    def __init__(self) -> None:
        self._hook = HookManager()
        self._lock = threading.Lock()

        # snapshot state, written only by update_snapshot() (Companion
        # thread), read only by _handle() (hook thread)
        self._mapping: Dict[int, KeyBind] = {}
        self._gate_open: bool = True

        # hook-thread-only state (never touched from update_snapshot) --
        # tracks which source identities are *currently* suppressed-and-
        # remapped so a key held across a live config edit still releases
        # the correct destination, mirroring R9Tools' _remapActive pattern.
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
        """Call once per Companion-window frame (see main.py), regardless of
        which panel is active -- mirrors hud_overlay.update_crosshair()'s
        per-frame hand-off. KeyBind is a frozen dataclass, so storing the
        destination references directly here is safe to read from the hook
        thread without copying further."""
        mapping: Dict[int, KeyBind] = {}
        for entry in remapper_state.entries:
            if not entry.enabled:
                continue
            if not entry.source.is_bound or not entry.destination.is_bound:
                continue
            mapping.setdefault(entry.source.vk_code, entry.destination)

        selected = window_select_state.selected
        gate_open = selected is None or window_select_state.selected_has_focus

        with self._lock:
            self._mapping = mapping
            self._gate_open = gate_open

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
            gate_open = self._gate_open

        if not gate_open:
            # Inert: a process is targeted and it doesn't currently have OS
            # focus. Physical input passes through completely untouched --
            # no remap, no macro-trigger visibility either (see module
            # docstring's "Window-filter gating" section).
            return None

        dest = mapping.get(vk)

        if dest is None:
            # Not (currently) configured as a remap source. Still check for
            # a stale-but-active remap on release, in case entries changed
            # while the key was physically held -- see module docstring.
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
