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

Two per-entry modes (`RemapEntry.mode`): Hold (default) mirrors source
down/up onto destination 1:1, exactly as above. Toggle latches destination
down on the first physical source press and up on the next; source release
is a no-op (only presses are published/injected in Toggle mode -- see
`_handle_toggle`). Toggle's on/off latch is tracked per-entry-id in
`_toggle_on` (hook-thread AND Companion-thread touch this, so it's always
read/written under `_lock`, unlike the Hold-only `_active_remaps`).

Stuck-key prevention: a Toggle latched "on", or a Hold remap whose physical
source is still down, both inject a real held key/button in whatever app has
focus, so every path that can leave one behind forces its release first.
`_force_release_pending()` is the release primitive for both; it's called
from `stop()` (app exit/engine stop) and from `_handle()` on a gate
open->closed transition (window-filter focus loss -- see below). Gate-close
matters for Hold too, not just Toggle: `_handle()` returns early while the
gate is closed, before Hold's own release-on-physical-up path ever runs, so
without this a Hold remap held through a focus loss would stay stuck if the
user released the physical key while still unfocused (a real gap, fixed
2026-08-31 -- `_force_release_pending()` was Toggle-only until then).
`update_snapshot()` separately force-releases any individual toggle-on entry
that's been removed/disabled/switched off Toggle mode/had its destination
edited since the last frame (covers profile switches too: apply_profile()
just replaces `AppState.remapper.entries` wholesale, so a switch to a profile
without that entry's id looks identical to a delete here -- no separate
profile-switch hook needed); Hold has no equivalent per-entry diff since it
has no persistent latch to invalidate -- ordinary map rebuilding already
stops mirroring a removed/disabled entry's source. A same-id/same-destination
edit to only a Toggle entry's *source* key intentionally does NOT force a
release -- the injected destination key's state isn't affected by what
triggers it, so tearing it down there would just be a spurious extra
keystroke.

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
from typing import Callable, Dict, List, Optional, Tuple

import input_inject
import window_select
from input_hooks import HookManager, KeyEvent, MouseButtonEvent
from key_capture import KeyBind, is_mouse_vk, keybind_vk_to_mouse_button, mouse_button_to_vk

# Avoid a hard import-time dependency on app_state beyond type hints, so this
# module stays importable/testable without a live hook. RemapMode is used at
# runtime (mode branch in _handle()), not just for type hints, but the
# fallback below still keeps a live-hook-free import from raising.
try:
    from app_state import RemapMode
except Exception:  # pragma: no cover
    RemapMode = None  # type: ignore

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


@dataclass(frozen=True)
class _MappingEntry:
    """One live, enabled, fully-bound remap -- what `_mapping` stores per
    source vk. Carries `entry_id`/`mode` alongside the destination (unlike
    the old bare-`KeyBind` mapping) because Toggle's per-entry on/off latch
    needs a stable identity independent of the source vk, which can be
    rebound without disturbing an already-latched toggle."""

    entry_id: str
    destination: KeyBind
    mode: "RemapMode"


# (enabled, mode, destination) as of the last update_snapshot() call, keyed
# by entry id -- what update_snapshot()'s toggle-cleanup diff compares
# against to notice an edit/disable/removal since the previous frame.
_EntrySnap = Tuple[bool, "RemapMode", KeyBind]


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
        self._mapping: Dict[int, _MappingEntry] = {}
        self._target_pid: Optional[int] = None

        # Hook-thread-only: tracks which source identities are currently
        # suppressed-and-held (Hold mode only) so a key held across a live
        # config edit still releases the correct destination.
        self._active_remaps: Dict[int, KeyBind] = {}

        # Toggle mode's on/off latch: entry id -> destination currently held
        # down by that entry's toggle. Both the hook thread (press flips it)
        # and the Companion thread (update_snapshot()'s cleanup diff) touch
        # this, so it's always read/written under `_lock` -- unlike
        # `_active_remaps` above, which is hook-thread-only.
        self._toggle_on: Dict[str, KeyBind] = {}

        # Hook-thread-only: last-evaluated window-filter gate state, so
        # `_handle()` can notice an open->closed transition (target process
        # just lost focus) and force-release any latched toggle immediately,
        # even with the Companion window minimized -- see module docstring.
        self._gate_open = True

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
        self._force_release_pending()  # never leave a latched Toggle or held Hold remap stuck on exit
        self._hook.stop()
        self._started = False
        self._gate_open = True

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
        of snapshotted here.

        Also runs the Toggle-mode cleanup diff: any entry id currently
        latched on in `_toggle_on` that no longer matches (removed,
        disabled, switched off Toggle, or its destination changed) since the
        last call gets force-released -- see module docstring's "Toggle
        stuck-key prevention" section."""
        mapping: Dict[int, _MappingEntry] = {}
        entry_snapshot: Dict[str, _EntrySnap] = {}
        for entry in remapper_state.entries:
            entry_snapshot[entry.id] = (entry.enabled, entry.mode, entry.destination)
            if not entry.enabled:
                continue
            if not entry.source.is_bound or not entry.destination.is_bound:
                continue
            mapping.setdefault(
                entry.source.vk_code,
                _MappingEntry(entry_id=entry.id, destination=entry.destination, mode=entry.mode),
            )

        selected = window_select_state.selected
        target_pid = selected.pid if selected is not None else None

        to_release: List[KeyBind] = []
        with self._lock:
            for entry_id, dest in list(self._toggle_on.items()):
                snap = entry_snapshot.get(entry_id)
                still_valid = snap is not None and snap[0] and snap[1] == RemapMode.TOGGLE and snap[2] == dest
                if not still_valid:
                    to_release.append(dest)
                    del self._toggle_on[entry_id]

            self._mapping = mapping
            self._target_pid = target_pid

        for dest in to_release:
            self._release_dest(dest)

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

        if gate_open != self._gate_open:
            self._gate_open = gate_open
            if not gate_open:
                # Just lost focus on the targeted process -- force-release any
                # latched Toggle before going inert, so it doesn't sit "held"
                # in whatever now has focus. Checked here (not just
                # update_snapshot()) because this fires on ANY system-wide
                # key/mouse-button event -- reliable even while the Companion
                # window is minimized, unlike the per-frame path.
                self._force_release_pending()

        if not gate_open:
            # Inert: targeted process doesn't currently have focus. Input
            # passes through untouched -- no remap, no macro-trigger visibility.
            return None

        entry = mapping.get(vk)

        if entry is None:
            # Not currently a remap source. Still check for a stale-but-active
            # Hold remap on release, in case entries changed while the key
            # was held. (Toggle entries never populate `_active_remaps` --
            # their release is a no-op, handled below.)
            active_dest = self._active_remaps.pop(vk, None) if up else None
            if active_dest is None:
                self._publish(EffectiveInputEvent(vk_code=vk, up=up, name=name, from_remap=False, time_ms=time_ms))
                return None
            self._inject(active_dest, up)
            self._publish(
                EffectiveInputEvent(vk_code=active_dest.vk_code, up=up, name=active_dest.name, from_remap=True, time_ms=time_ms)
            )
            return True

        if RemapMode is not None and entry.mode == RemapMode.TOGGLE:
            return self._handle_toggle(entry, up, time_ms)

        # Hold mode: mirror source down/up onto destination 1:1.
        dest = entry.destination
        if up:
            self._active_remaps.pop(vk, None)
        else:
            self._active_remaps[vk] = dest

        self._inject(dest, up)
        self._publish(EffectiveInputEvent(vk_code=dest.vk_code, up=up, name=dest.name, from_remap=True, time_ms=time_ms))
        return True  # suppress the original physical event

    def _handle_toggle(self, entry: _MappingEntry, up: bool, time_ms: int) -> bool:
        """Only presses matter in Toggle mode -- release is a fully-absorbed
        no-op (suppressed, not published, not injected). First press latches
        destination down; the next press releases it, using the destination
        recorded at latch time (not a fresh lookup) so a mid-hold config edit
        can't release the wrong key -- `update_snapshot()` force-releases any
        entry whose destination actually changes while latched anyway."""
        if up:
            return True  # suppress; physical release does nothing in Toggle mode

        with self._lock:
            current_dest = self._toggle_on.get(entry.entry_id)
            if current_dest is None:
                self._toggle_on[entry.entry_id] = entry.destination
                dest, now_up = entry.destination, False
            else:
                del self._toggle_on[entry.entry_id]
                dest, now_up = current_dest, True

        self._inject(dest, now_up)
        self._publish(EffectiveInputEvent(vk_code=dest.vk_code, up=now_up, name=dest.name, from_remap=True, time_ms=time_ms))
        return True

    def _force_release_pending(self) -> None:
        """Release every currently-latched Toggle entry AND every Hold-mode
        remap whose physical source is still down, then clear both. Called
        on gate-close (window-filter focus loss) and stop() -- the two
        cleanup transitions that aren't already covered by update_snapshot()'s
        per-entry diff.

        Was Toggle-only until 2026-08-31: gate-close returned early before
        Hold's own release-on-up path in `_handle()` ever ran, so a Hold
        remap held through a focus loss stayed stuck if the user released
        the physical key while still unfocused. Fixed by folding
        `_active_remaps` into this same cleanup.

        Safe to call from either thread: `_toggle_on` is always
        lock-protected. `_active_remaps` is otherwise hook-thread-only
        (see its own comment in __init__), but every call site here either
        runs ON the hook thread itself (the gate-close path, inside
        `_handle()`) or only after `HookManager.stop()`'s `join()` has
        already guaranteed the hook thread has exited (`stop()` below) --
        never reached from the Companion thread while the hook thread could
        still be concurrently mutating it."""
        with self._lock:
            pending = list(self._toggle_on.values())
            self._toggle_on.clear()
        pending.extend(self._active_remaps.values())
        self._active_remaps.clear()
        for dest in pending:
            self._release_dest(dest)

    def _release_dest(self, dest: KeyBind) -> None:
        self._inject(dest, up=True)
        self._publish(EffectiveInputEvent(vk_code=dest.vk_code, up=True, name=dest.name, from_remap=True, time_ms=0))

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
