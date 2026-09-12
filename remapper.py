"""remapper.py -- two independent, always-on matchers against
`AppState.remapper` state, both driven off the same live keyboard/mouse-
button hook. Pure user-mode: `input_hooks.HookManager` for capture,
`input_inject.send_key` / `send_mouse_button` for injection -- no driver, no
ViGEm, no game-process access.

**Standard Remapping** (`RemapperState.entries`, `RemapEntry`): a plain 1:1
source -> destination remap. Destination mirrors source down/up, always, at
whatever frequency the source is pressed -- no mode concept. A remap also
registers as its destination for the rest of the app's own trigger matching
(so a remapped key correctly arms macros bound to that destination), not
just re-emitting the raw OS event.

**Auto Toggle/Hold** (`RemapperState.auto_entries`, `AutoToggleHoldEntry`):
a single key acting on itself -- there's no destination, since the point is
changing how the key's OWN presses land, not remapping it elsewhere.
  - Toggle: first physical press latches the key down and leaves it down,
    next physical press sends it up. Physical release is a no-op. This is
    the same latch state machine standard RemapEntry.mode used to implement
    (`_handle_toggle`, now `_handle_auto_toggle`) -- relocated here, not
    reimplemented.
  - Hold (new): on physical press, tap the key (down, wait
    `_AUTO_HOLD_TAP_GAP_MS`, up). On physical release, tap it again,
    independently. This converts a game's own toggle-only action into
    something that feels like hold-to-use from the player's side, using the
    press and release edges as two independent tap triggers rather than
    mirroring a held state. The delayed "up" is NOT slept inline on the hook
    callback thread -- Windows enforces a low-level-hook responsiveness
    timeout (~300ms, LowLevelHooksTimeout) and will silently unhook a
    callback that blocks too long, and this needs to do it on both edges of
    every gesture. Instead the hook callback injects the "down" and hands the
    delayed "up" to a short-lived `threading.Timer` (`self._timer_factory`,
    swappable in tests), then returns immediately.

If a physical vk matches BOTH a standard remap source and an Auto Toggle/
Hold key, the standard remap wins (`_mapping` is checked first) -- an
unusual double-binding, but deterministic rather than undefined.

OS key-repeat: while a key is held, Windows resends "down" transitions
through WH_KEYBOARD_LL at the keyboard repeat rate -- there's no repeat-
count/prior-state bit on this hook the way WM_KEYDOWN's lParam has one, so
each repeat is indistinguishable from a fresh press at this layer. Harmless
for a standard remap (mirroring redundant downs onto an already-down
destination changes nothing), but Auto Toggle/Hold treat every press as one
discrete edge -- without filtering, a held Auto Hold key fired a new tap on
every repeat tick instead of one on press and one on release (found live
2026-09-11: a held Toggle-sprint key auto-repeated in and out of sprint),
and Auto Toggle would rapidly flip on/off the same way. `_physical_down`
tracks genuine physical key state (updated unconditionally at the top of
`_handle()`, for every vk, not just auto-mapped ones -- see its own comment
in __init__) so only the real press/release transitions reach either
handler; a repeat is suppressed as a no-op.

Every physical keyboard/mouse-button event is published as a normalized
`EffectiveInputEvent`: the synthesized identity + `from_remap=True` if it
matched an enabled entry in either section, original identity + `from_remap
=False` otherwise. `macro_engine.py` subscribes via `add_effective_listener`
instead of installing its own hook, so there's exactly one place a remap's
destination (or an Auto entry's own key) becomes visible to the rest of the
app's trigger matching.

Stuck-key prevention: a Toggle latched "on", a Hold-mode standard remap
whose physical source is still down, or an Auto Hold entry with a tap still
pending, all inject (or are about to inject) a real held/about-to-be-
released key/button in whatever app has focus, so every path that can leave
one behind forces its release first. `_force_release_pending()` is the
release primitive for all three; it's called from `stop()` (app exit/engine
stop) and from `_handle()` on a gate open->closed transition (window-filter
focus loss -- see below). Gate-close matters for the standard Hold remap
too, not just Toggle/Auto-Hold: `_handle()` returns early while the gate is
closed, before the standard remap's own release-on-physical-up path ever
runs, so without this a held remap through a focus loss would stay stuck if
the user released the physical key while still unfocused (a real gap, fixed
2026-08-31 -- `_force_release_pending()` was Toggle-only until then).
`update_snapshot()` separately force-releases any individual Toggle/Auto-Hold
entry that's been removed/disabled/switched mode/had its key edited since
the last frame (covers profile switches too: apply_profile() just replaces
`AppState.remapper.entries`/`.auto_entries` wholesale, so a switch to a
profile without that entry's id looks identical to a delete here -- no
separate profile-switch hook needed); the standard Hold remap has no
equivalent per-entry diff since it has no persistent latch to invalidate --
ordinary map rebuilding already stops mirroring a removed/disabled entry's
source. A same-id/same-key edit to only a Toggle/Auto-Hold entry's OTHER
cosmetic field (e.g. name) intentionally does NOT force a release -- the
injected key's state isn't affected by anything but its own identity.

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
"the macro engine go inert alongside the remapper" is true for *matching* a
fresh trigger, but not for a Hold/Toggle session already running on its own
thread by the time the gate closes -- `add_gate_close_listener()` exists for
that case, see its own docstring.

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
# runtime (Auto Toggle/Hold's mode branch in _handle()), not just for type
# hints, but the fallback below still keeps a live-hook-free import from
# raising.
try:
    from app_state import RemapMode
except Exception:  # pragma: no cover
    RemapMode = None  # type: ignore

try:  # pragma: no cover - only used for type hints
    from app_state import RemapperState, WindowSelectState
except Exception:  # pragma: no cover
    RemapperState = object  # type: ignore
    WindowSelectState = object  # type: ignore

# Fixed gap between the synthesized down and up of one Auto Hold tap. Not
# user-configurable -- per design, this exists specifically to prevent
# misfires from input lag, not to be tuned per key.
_AUTO_HOLD_TAP_GAP_MS = 100


@dataclass(frozen=True)
class EffectiveInputEvent:
    """A normalized post-remap identity: a remap's destination (or an Auto
    Toggle/Hold entry's own key) if the physical event matched an enabled
    entry, else the original physical identity. `vk_code` uses
    key_capture.py's keyboard-vk / mouse-pseudo-vk scheme uniformly, so
    consumers don't need to care whether it originated from a keyboard or a
    mouse button."""

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
    """One live, enabled, fully-bound standard remap -- what `_mapping`
    stores per source vk. Carries `entry_id` alongside the destination
    (unlike a bare `KeyBind`) purely so a same-source rebind can be diffed
    the same way Auto entries are, if that's ever needed -- not otherwise
    used today since standard remaps have no persistent latch to clean up."""

    entry_id: str
    destination: KeyBind


@dataclass(frozen=True)
class _AutoMappingEntry:
    """One live, enabled, fully-bound Auto Toggle/Hold entry -- what
    `_auto_mapping` stores per key vk. `key` doubles as both the match
    target and the injected identity, since these entries act on themselves."""

    entry_id: str
    key: KeyBind
    mode: "RemapMode"


@dataclass
class _PendingHoldTap:
    """One Auto Hold entry's in-flight tap: the timer that will send the
    delayed "up", and the key it'll send it for (captured at tap-start, not
    re-looked-up later, so a mid-tap config edit can't release the wrong
    key -- same principle as Toggle's `_toggle_on` below)."""

    timer: threading.Timer
    key: KeyBind


# (enabled, key) as of the last update_snapshot() call, keyed by entry id --
# what update_snapshot()'s Toggle/Hold cleanup diffs compare against to
# notice an edit/mode-switch/disable/removal since the previous frame.
_AutoEntrySnap = Tuple[bool, "RemapMode", KeyBind]


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
        self._auto_mapping: Dict[int, _AutoMappingEntry] = {}
        self._target_pid: Optional[int] = None

        # Hook-thread-only: tracks which source identities are currently
        # suppressed-and-held (standard remap only) so a key held across a
        # live config edit still releases the correct destination.
        self._active_remaps: Dict[int, KeyBind] = {}

        # Auto Toggle's on/off latch: entry id -> key currently held down by
        # that entry's toggle. Both the hook thread (press flips it) and the
        # Companion thread (update_snapshot()'s cleanup diff) touch this, so
        # it's always read/written under `_lock` -- unlike `_active_remaps`
        # above, which is hook-thread-only.
        self._toggle_on: Dict[str, KeyBind] = {}

        # Auto Hold's in-flight taps: entry id -> pending timer + key. Same
        # multi-thread touch pattern as `_toggle_on` (hook thread schedules/
        # supersedes, the timer's own thread completes it, the Companion
        # thread's cleanup diff can force-drain it) -- always under `_lock`.
        self._hold_pending: Dict[str, _PendingHoldTap] = {}

        # Hook-thread-only: physical vks currently held down, tracked for
        # EVERY key/button regardless of whether it currently matches
        # anything -- see `_handle()`'s repeat-suppression comment for why
        # this can't be scoped to just auto-mapped keys. Windows' own
        # keyboard auto-repeat resends a "down" transition through
        # WH_KEYBOARD_LL at the OS repeat rate for as long as a key stays
        # physically held, indistinguishable at this layer from a fresh
        # press -- there's no repeat-count/prior-state bit on
        # KBDLLHOOKSTRUCT the way WM_KEYDOWN's lParam has one. A standard
        # remap mirroring redundant downs onto an already-down destination is
        # harmless, but Auto Toggle/Hold treat every press as one discrete
        # edge (flip the latch / fire one tap) -- without this, holding an
        # Auto Hold key fires a new tap on every repeat tick instead of
        # exactly one on press and one on release (real bug, found live
        # 2026-09-11: holding a Toggle-sprint key auto-repeated in and out of
        # sprint), and Auto Toggle would rapidly flip on/off the same way.
        self._physical_down: "set[int]" = set()

        # Swappable so tests can control tap timing without a real sleep --
        # see tests/test_remapper_auto_hold.py. Defaults to a real
        # daemonized threading.Timer.
        self._timer_factory: Callable[[float, Callable[[], None]], threading.Timer] = _make_timer

        # Hook-thread-only: last-evaluated window-filter gate state, so
        # `_handle()` can notice an open->closed transition (target process
        # just lost focus) and force-release any latched toggle/pending hold
        # immediately, even with the Companion window minimized -- see
        # module docstring.
        self._gate_open = True

        self._listeners: List[EffectiveListener] = []
        # Fired on the same open->closed transition as _force_release_pending()
        # above, for a consumer with its own independent stuck-state to clean
        # up on focus loss -- see add_gate_close_listener()'s docstring.
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
        """Register a callback fired on the window-filter gate's exact
        open->closed transition (target process just lost focus) -- the same
        moment `_force_release_pending()` runs for this module's own state.

        Exists for macro_engine.py: a live Hold/Toggle macro session runs on
        its own dedicated thread reading `_RuntimeState.held`/`toggle_running`,
        which only changes when a real trigger-release event reaches
        `handle_effective_event()` -- and while the gate is closed, `_handle()`
        returns before publishing anything at all (see module docstring's
        "Window-filter gating" section), so a trigger release that happens
        while the targeted process is unfocused is silently dropped and the
        macro's loop never learns to stop. Without this, a Hold macro whose
        trigger got released during a focus loss keeps re-firing its steps
        into whatever now has focus, indefinitely -- found live 2026-09-12,
        worse than a single stuck key since it's an actively repeating
        action. Callbacks run on the hook thread; keep them fast, same
        constraint as every other hook callback in this module."""
        self._gate_close_listeners.append(callback)

    def update_snapshot(self, remapper_state: "RemapperState", window_select_state: "WindowSelectState") -> None:
        """Call once per Companion-window frame. KeyBind is a frozen
        dataclass, so storing destination/key references directly is safe to
        read from the hook thread without copying further.

        Only hands off *which* pid is targeted (or None), not whether it
        currently has focus -- see module docstring's "Window-filter gating"
        section for why that check is evaluated live in `_handle()` instead
        of snapshotted here.

        Also runs the Auto Toggle/Hold cleanup diffs: any entry id currently
        latched on in `_toggle_on`, or with a tap pending in `_hold_pending`,
        that no longer matches (removed, disabled, switched mode, or its key
        changed) since the last call gets force-released/cancelled -- see
        module docstring's "Stuck-key prevention" section."""
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
        target_pid = selected.pid if selected is not None else None

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
            self._target_pid = target_pid

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
        # Tracked unconditionally, before the gate/mapping checks below, so
        # it stays accurate regardless of window-filter state or whether
        # anything currently matches `vk` -- see `_physical_down`'s own
        # comment in __init__ for why membership can't be scoped to just the
        # auto-mapped keys.
        was_down = vk in self._physical_down
        if up:
            self._physical_down.discard(vk)
        else:
            self._physical_down.add(vk)
        is_os_repeat = (not up) and was_down

        with self._lock:
            mapping = self._mapping
            auto_mapping = self._auto_mapping
            target_pid = self._target_pid

        # Evaluated live, every event -- see module docstring's
        # "Window-filter gating" section.
        gate_open = target_pid is None or window_select.cached_foreground_pid() == target_pid

        if gate_open != self._gate_open:
            self._gate_open = gate_open
            if not gate_open:
                # Just lost focus on the targeted process -- force-release any
                # latched Toggle/pending Hold tap before going inert, so
                # nothing sits "held" (or fires late) in whatever now has
                # focus. Checked here (not just update_snapshot()) because
                # this fires on ANY system-wide key/mouse-button event --
                # reliable even while the Companion window is minimized,
                # unlike the per-frame path.
                self._force_release_pending()
                for listener in list(self._gate_close_listeners):
                    try:
                        listener()
                    except Exception:
                        traceback.print_exc()

        if not gate_open:
            # Inert: targeted process doesn't currently have focus. Input
            # passes through untouched -- no remap, no macro-trigger visibility.
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

        # Not currently a remap source. Still check for a stale-but-active
        # standard remap on release, in case entries changed while the key
        # was held.
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
                # Windows re-firing "down" for a key still physically held --
                # not a real press edge. Already latched (Toggle) or already
                # tapped (Hold); still suppress so the raw repeat doesn't
                # leak through as an unmapped keystroke.
                return True
            if RemapMode is not None and auto_entry.mode == RemapMode.TOGGLE:
                return self._handle_auto_toggle(auto_entry, up, time_ms)
            return self._handle_auto_hold(auto_entry, up, time_ms)

        self._publish(EffectiveInputEvent(vk_code=vk, up=up, name=name, from_remap=False, time_ms=time_ms))
        return None

    def _handle_auto_toggle(self, entry: _AutoMappingEntry, up: bool, time_ms: int) -> bool:
        """Only presses matter -- release is a fully-absorbed no-op
        (suppressed, not published, not injected). First press latches the
        key down; the next press releases it, using the key recorded at
        latch time (not a fresh lookup) so a mid-hold config edit can't
        release the wrong key -- `update_snapshot()` force-releases any
        entry whose key actually changes while latched anyway."""
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
        """Fires an identical down-wait-up tap on BOTH the press and release
        edges (see module docstring) -- converts a game's toggle-only action
        into hold-to-use from the player's side. `up` isn't otherwise used:
        both edges do the exact same thing.

        Any tap already pending for this entry is cancelled and force-
        completed first, so two fast edges (e.g. a quick tap of the physical
        key, faster than `_AUTO_HOLD_TAP_GAP_MS`) never leave an orphaned
        timer or an overlapping down state behind."""
        with self._lock:
            old = self._hold_pending.pop(entry.entry_id, None)
        if old is not None:
            old.timer.cancel()
            self._release_dest(old.key)

        self._inject(entry.key, up=False)
        self._publish(EffectiveInputEvent(vk_code=entry.key.vk_code, up=False, name=entry.key.name, from_remap=True, time_ms=time_ms))

        entry_id = entry.entry_id
        # `timer` is captured by the lambda AFTER assignment below, not by
        # value now -- by the time the callback actually runs, the local
        # `timer` name in this call's scope already points at the right
        # object. Passed through explicitly (not just `entry_id`) so a late
        # fire from an already-superseded tap (see the `old` handling above)
        # can tell itself apart from the tap that superseded it -- both would
        # share the same entry_id, and entry_id alone being present in
        # `_hold_pending` isn't proof it's THIS particular timer's tap.
        timer = self._timer_factory(_AUTO_HOLD_TAP_GAP_MS / 1000.0, lambda: self._finish_hold_tap(entry_id, timer))
        with self._lock:
            self._hold_pending[entry_id] = _PendingHoldTap(timer=timer, key=entry.key)
        timer.start()
        return True  # suppress the original physical event

    def _finish_hold_tap(self, entry_id: str, timer: threading.Timer) -> None:
        """Timer completion callback -- runs on the timer's own thread, NOT
        the hook thread. Checks identity (both the entry id AND that this is
        still the exact timer registered for it) before acting: cancel()
        can't guarantee a callback that already started won't still run, and
        a same-id tap can get superseded by a new one before this fires."""
        with self._lock:
            pending = self._hold_pending.get(entry_id)
            if pending is None or pending.timer is not timer:
                return
            del self._hold_pending[entry_id]
            key = pending.key
        self._release_dest(key)

    def _force_release_pending(self) -> None:
        """Release every currently-latched Auto Toggle entry, cancel and
        release every Auto Hold entry with a tap in flight, AND release every
        standard remap whose physical source is still down, then clear all
        three. Called on gate-close (window-filter focus loss) and stop() --
        the two cleanup transitions that aren't already covered by
        update_snapshot()'s per-entry diff.

        Was Toggle-only until 2026-08-31: gate-close returned early before
        the standard remap's own release-on-up path in `_handle()` ever ran,
        so a held remap through a focus loss stayed stuck if the user
        released the physical key while still unfocused. Fixed by folding
        `_active_remaps` into this same cleanup; `_hold_pending` (Auto Hold)
        joined it when that mode was added.

        Safe to call from either thread: `_toggle_on`/`_hold_pending` are
        always lock-protected. `_active_remaps` is otherwise hook-thread-only
        (see its own comment in __init__), but every call site here either
        runs ON the hook thread itself (the gate-close path, inside
        `_handle()`) or only after `HookManager.stop()`'s `join()` has
        already guaranteed the hook thread has exited (`stop()` below) --
        never reached from the Companion thread while the hook thread could
        still be concurrently mutating it."""
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
# Process-wide singleton -- main.py starts/stops this alongside the
# Companion window's own lifecycle (hud_overlay/capture_service pattern).
# ---------------------------------------------------------------------------

remapper_engine = RemapperEngine()
