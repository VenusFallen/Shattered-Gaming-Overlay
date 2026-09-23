"""Two always-on matchers against `AppState.remapper` state, driven off one live keyboard/mouse-button hook.
Pure user-mode: `input_hooks.HookManager` for capture, `input_inject` for injection -- no driver, no ViGEm,
no game-process access. Standard Remapping (`entries`) is a plain 1:1 source->destination mirror; Auto
Toggle/Hold (`auto_entries`) makes a key act on itself (latch on/off, or convert toggle-only into hold-to-use).
Gated by window_select's live focus/target-pid tracking so matching goes inert on focus loss -- see
`_handle()`.
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

# Avoid a hard import-time dependency on app_state so this module stays importable without a live hook.
try:
    from app_state import RemapMode
except Exception:  # pragma: no cover
    RemapMode = None  # type: ignore

try:  # pragma: no cover - only used for type hints
    from app_state import RemapperState, WindowSelectState
except Exception:  # pragma: no cover
    RemapperState = object  # type: ignore
    WindowSelectState = object  # type: ignore

# Fixed gap between the synthesized down and up of one Auto Hold tap; not user-configurable.
_AUTO_HOLD_TAP_GAP_MS = 100


@dataclass(frozen=True)
class EffectiveInputEvent:
    """Normalized post-remap identity: the remap's destination (or an Auto entry's own key) if matched, else the original physical identity."""

    vk_code: int
    up: bool
    name: str = ""
    from_remap: bool = False
    time_ms: int = 0


EffectiveListener = Callable[[EffectiveInputEvent], None]


def _make_timer(interval_sec: float, fn: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(interval_sec, fn)
    timer.daemon = True
    return timer


@dataclass(frozen=True)
class _MappingEntry:
    """One live, enabled, fully-bound standard remap, keyed by source vk in `_mapping`."""

    entry_id: str
    destination: KeyBind


@dataclass(frozen=True)
class _AutoMappingEntry:
    """One live, enabled, fully-bound Auto Toggle/Hold entry, keyed by key vk in `_auto_mapping`; `key` is both the match target and the injected identity."""

    entry_id: str
    key: KeyBind
    mode: "RemapMode"


@dataclass
class _PendingHoldTap:
    """One Auto Hold entry's in-flight tap; `key` is captured at tap-start so a mid-tap config edit can't release the wrong key."""

    timer: threading.Timer
    key: KeyBind


# (enabled, mode, key) as of the last update_snapshot() call, keyed by entry id -- diffed to detect edits/removals.
_AutoEntrySnap = Tuple[bool, "RemapMode", KeyBind]


class RemapperEngine:
    """Always-on remap matcher/injector. start()/stop()/update_snapshot()/add_effective_listener() are thread-safe from the Companion thread; everything else runs on the hook's own background thread."""

    def __init__(self) -> None:
        self._hook = HookManager()
        self._lock = threading.Lock()

        # Written only by update_snapshot() (Companion thread), read only by _handle() (hook thread).
        self._mapping: Dict[int, _MappingEntry] = {}
        self._auto_mapping: Dict[int, _AutoMappingEntry] = {}
        self._gate_active: bool = False  # False when no process is targeted (gate always open)

        # Hook-thread-only: source identities currently suppressed-and-held (standard remap only).
        self._active_remaps: Dict[int, KeyBind] = {}

        # Auto Toggle's on/off latch: entry id -> key currently held down; touched by both hook and Companion threads, always under `_lock`.
        self._toggle_on: Dict[str, KeyBind] = {}

        # Auto Hold's in-flight taps: entry id -> pending timer + key; same multi-thread pattern as `_toggle_on`.
        self._hold_pending: Dict[str, _PendingHoldTap] = {}

        # Hook-thread-only: physical vks currently held, tracked for every key so OS key-repeat (which resends
        # "down" with no distinguishing bit) can be told apart from a genuine press/release edge below.
        self._physical_down: "set[int]" = set()

        # Swappable so tests can control tap timing without a real sleep; defaults to a real daemonized threading.Timer.
        self._timer_factory: Callable[[float, Callable[[], None]], threading.Timer] = _make_timer

        # Hook-thread-only: last-evaluated window-filter gate state, to detect an open->closed transition.
        self._gate_open = True

        self._listeners: List[EffectiveListener] = []
        # Fired on the same open->closed transition as _force_release_pending(), for a consumer with its own stuck-state to clean up.
        self._gate_close_listeners: List[Callable[[], None]] = []
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
        self._force_release_pending()  # never leave a latched Toggle, pending Hold tap, or held remap stuck on exit
        self._hook.stop()
        self._started = False
        self._gate_open = True
        self._physical_down.clear()

    @property
    def is_running(self) -> bool:
        return self._hook.is_running

    def add_effective_listener(self, callback: EffectiveListener) -> None:
        self._listeners.append(callback)

    def add_gate_close_listener(self, callback: Callable[[], None]) -> None:
        """Fires on the window-filter gate's open->closed transition; lets macro_engine.py stop a running Hold/Toggle macro session that would otherwise never see the trigger-release event while the gate is closed. Runs on the hook thread -- keep it fast."""
        self._gate_close_listeners.append(callback)

    def update_snapshot(self, remapper_state: "RemapperState", window_select_state: "WindowSelectState") -> None:
        """Call once per Companion-window frame. Rebuilds the mapping tables and runs the Auto Toggle/Hold cleanup diff, force-releasing any entry that's been removed/disabled/switched mode/had its key edited since the last call."""
        mapping: Dict[int, _MappingEntry] = {}
        for entry in remapper_state.entries:
            if not entry.enabled:
                continue
            if not entry.source.is_bound or not entry.destination.is_bound:
                continue
            mapping.setdefault(entry.source.vk_code, _MappingEntry(entry_id=entry.id, destination=entry.destination))

        auto_mapping: Dict[int, _AutoMappingEntry] = {}
        auto_snapshot: Dict[str, _AutoEntrySnap] = {}
        for auto in remapper_state.auto_entries:
            auto_snapshot[auto.id] = (auto.enabled, auto.mode, auto.key)
            if not auto.enabled or not auto.key.is_bound:
                continue
            auto_mapping.setdefault(auto.key.vk_code, _AutoMappingEntry(entry_id=auto.id, key=auto.key, mode=auto.mode))

        selected = window_select_state.selected
        # Arms window_select's background thread with which exe to keep resolving a live pid for;
        # only needs to land once per actual target change but is cheap enough to call every frame.
        window_select.set_target_exe_name(selected.exe_name if selected is not None else None)
        gate_active = selected is not None

        to_release_toggle: List[KeyBind] = []
        to_release_hold: List[_PendingHoldTap] = []
        with self._lock:
            for entry_id, key in list(self._toggle_on.items()):
                snap = auto_snapshot.get(entry_id)
                still_valid = snap is not None and snap[0] and RemapMode is not None and snap[1] == RemapMode.TOGGLE and snap[2] == key
                if not still_valid:
                    to_release_toggle.append(key)
                    del self._toggle_on[entry_id]

            for entry_id, pending in list(self._hold_pending.items()):
                snap = auto_snapshot.get(entry_id)
                still_valid = snap is not None and snap[0] and RemapMode is not None and snap[1] == RemapMode.HOLD and snap[2] == pending.key
                if not still_valid:
                    to_release_hold.append(pending)
                    del self._hold_pending[entry_id]

            self._mapping = mapping
            self._auto_mapping = auto_mapping
            self._gate_active = gate_active

        for pending in to_release_hold:
            pending.timer.cancel()
        for key in to_release_toggle:
            self._release_dest(key)
        for pending in to_release_hold:
            self._release_dest(pending.key)

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
        # Tracked unconditionally for every vk, regardless of gate/mapping state, to detect OS key-repeat below.
        was_down = vk in self._physical_down
        if up:
            self._physical_down.discard(vk)
        else:
            self._physical_down.add(vk)
        is_os_repeat = (not up) and was_down

        with self._lock:
            mapping = self._mapping
            auto_mapping = self._auto_mapping
            gate_active = self._gate_active

        # Evaluated live against window_select's independently-maintained target pid, not a value snapshotted in update_snapshot().
        gate_open = not gate_active or window_select.cached_foreground_pid() == window_select.cached_target_pid()

        if gate_open != self._gate_open:
            self._gate_open = gate_open
            if not gate_open:
                # Just lost focus on the targeted process -- force-release before going inert so nothing stays held.
                self._force_release_pending()
                for listener in list(self._gate_close_listeners):
                    try:
                        listener()
                    except Exception:
                        traceback.print_exc()

        if not gate_open:
            # Inert: targeted process doesn't have focus. Input passes through untouched.
            return None

        entry = mapping.get(vk)

        if entry is not None:
            # Standard remap: mirror source down/up onto destination 1:1.
            dest = entry.destination
            if up:
                self._active_remaps.pop(vk, None)
            else:
                self._active_remaps[vk] = dest

            self._inject(dest, up)
            self._publish(EffectiveInputEvent(vk_code=dest.vk_code, up=up, name=dest.name, from_remap=True, time_ms=time_ms))
            return True  # suppress the original physical event

        # Not currently a remap source. Still check for a stale-but-active remap on release, in case entries changed while the key was held.
        active_dest = self._active_remaps.pop(vk, None) if up else None
        if active_dest is not None:
            self._inject(active_dest, up)
            self._publish(
                EffectiveInputEvent(vk_code=active_dest.vk_code, up=up, name=active_dest.name, from_remap=True, time_ms=time_ms)
            )
            return True

        auto_entry = auto_mapping.get(vk)
        if auto_entry is not None:
            if is_os_repeat:
                # OS key-repeat re-firing "down" for a key still held, not a real press edge -- suppress without re-triggering.
                return True
            if RemapMode is not None and auto_entry.mode == RemapMode.TOGGLE:
                return self._handle_auto_toggle(auto_entry, up, time_ms)
            return self._handle_auto_hold(auto_entry, up, time_ms)

        self._publish(EffectiveInputEvent(vk_code=vk, up=up, name=name, from_remap=False, time_ms=time_ms))
        return None

    def _handle_auto_toggle(self, entry: _AutoMappingEntry, up: bool, time_ms: int) -> bool:
        """Only presses matter; release is a no-op. First press latches the key down, next press releases it, using the key recorded at latch time (not a fresh lookup) so a mid-hold config edit can't release the wrong key."""
        if up:
            return True  # suppress; physical release does nothing in Toggle mode

        with self._lock:
            current_key = self._toggle_on.get(entry.entry_id)
            if current_key is None:
                self._toggle_on[entry.entry_id] = entry.key
                key, now_up = entry.key, False
            else:
                del self._toggle_on[entry.entry_id]
                key, now_up = current_key, True

        self._inject(key, now_up)
        self._publish(EffectiveInputEvent(vk_code=key.vk_code, up=now_up, name=key.name, from_remap=True, time_ms=time_ms))
        return True

    def _handle_auto_hold(self, entry: _AutoMappingEntry, up: bool, time_ms: int) -> bool:
        """Fires an identical down-wait-up tap on both press and release edges; `up` isn't otherwise used. Any tap already pending for this entry is cancelled and force-completed first, so two fast edges never leave an orphaned timer behind."""
        with self._lock:
            old = self._hold_pending.pop(entry.entry_id, None)
        if old is not None:
            old.timer.cancel()
            self._release_dest(old.key)

        self._inject(entry.key, up=False)
        self._publish(EffectiveInputEvent(vk_code=entry.key.vk_code, up=False, name=entry.key.name, from_remap=True, time_ms=time_ms))

        entry_id = entry.entry_id
        # `timer` is passed explicitly (not just entry_id) so a late fire from an already-superseded tap can tell itself apart from the tap that superseded it.
        timer = self._timer_factory(_AUTO_HOLD_TAP_GAP_MS / 1000.0, lambda: self._finish_hold_tap(entry_id, timer))
        with self._lock:
            self._hold_pending[entry_id] = _PendingHoldTap(timer=timer, key=entry.key)
        timer.start()
        return True  # suppress the original physical event

    def _finish_hold_tap(self, entry_id: str, timer: threading.Timer) -> None:
        """Runs on the timer's own thread. Checks both entry id and timer identity before acting -- cancel() can't guarantee an already-started callback won't still run."""
        with self._lock:
            pending = self._hold_pending.get(entry_id)
            if pending is None or pending.timer is not timer:
                return
            del self._hold_pending[entry_id]
            key = pending.key
        self._release_dest(key)

    def _force_release_pending(self) -> None:
        """Releases every latched Auto Toggle entry, cancels/releases every in-flight Auto Hold tap, and releases every standard remap whose source is still down. Called on gate-close and stop(); safe from either thread since every call site either runs on the hook thread or only after the hook thread has already exited."""
        with self._lock:
            pending_toggle = list(self._toggle_on.values())
            self._toggle_on.clear()
            pending_hold = list(self._hold_pending.values())
            self._hold_pending.clear()
        pending_toggle.extend(self._active_remaps.values())
        self._active_remaps.clear()
        for pending in pending_hold:
            pending.timer.cancel()
        for key in pending_toggle:
            self._release_dest(key)
        for pending in pending_hold:
            self._release_dest(pending.key)

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
# Process-wide singleton -- main.py starts/stops this alongside the Companion window's own lifecycle.
# ---------------------------------------------------------------------------

remapper_engine = RemapperEngine()
