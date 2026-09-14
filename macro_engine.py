"""macro_engine.py -- executes `AppState.macros.macros` (`MacroDef`) entries:
Once/Hold/Toggle trigger modes, editable delay steps, per-macro enable, and
real humanize jitter on Delay-step timing. Playback is only ever
`input_inject.send_key` / `send_mouse_button` / `send_scroll` calls --
discrete actions, no continuous mouse-movement synthesis (hard scope
boundary for this project).

Installs no hook of its own. It subscribes to
`remapper.remapper_engine.add_effective_listener(...)` and matches triggers
against the post-remap identity stream, so a remapped key correctly arms a
macro bound to its destination. This also gives macros the window-focus gate
for free: remapper.py stops publishing entirely while a targeted process is
unfocused, so macros go inert alongside the remapper with no separate
window_select.py lookup needed here.

`handle_effective_event()` runs on the hook's background thread and must
stay fast/non-blocking -- it only does a dict/list lookup and starts/signals
a background thread. Each active trigger session (Hold held, Toggle on, Once
firing) runs its own step playback, including all Delay sleeping, on its own
dedicated thread, so delays never block hook processing.

`update_snapshot(macros_state)` is called once per Companion-window frame
and copies MacroDef/MacroStep's plain-data fields into immutable snapshot
dataclasses -- the live, UI-mutable instances are never read from a
background thread.

The one documented behavioral detection vector for SendInput macros is
inhumanly-regular timing. `MacroDef.humanize_jitter_pct` is applied as real
random variance (uniform(1 - pct, 1 + pct)) to every Delay step's sleep
duration on every playback, not just a cosmetic display value.
"""

from __future__ import annotations

import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import input_inject
from input_inject import MouseButton

try:  # pragma: no cover - only used for type hints
    from app_state import MacroMode, MacroStepKind, MacrosState
except Exception:  # pragma: no cover
    MacroMode = object  # type: ignore
    MacroStepKind = object  # type: ignore
    MacrosState = object  # type: ignore

from remapper import EffectiveInputEvent

_MOUSE_BUTTON_BY_LABEL = {
    "Left": MouseButton.LEFT,
    "Right": MouseButton.RIGHT,
    "Middle": MouseButton.MIDDLE,
    "X1": MouseButton.X1,
    "X2": MouseButton.X2,
}

# Fixed hold duration for a tap/click step so the receiving app registers a
# real press rather than a same-instant down+up some input readers coalesce
# away. Intentionally NOT humanized -- jitter is scoped to Delay steps only.
_TAP_HOLD_SEC = 0.03
_CLICK_HOLD_SEC = 0.04

# Yield between Hold/Toggle iterations with no Delay steps, so the loop
# can't spin the CPU at 100%.
_LOOP_YIELD_SEC = 0.001


@dataclass(frozen=True)
class _StepSnap:
    kind: "MacroStepKind"
    key_vk: Optional[int]
    mouse_button: str
    scroll_delta: int
    delay_ms: int


@dataclass(frozen=True)
class _MacroSnap:
    id: str
    name: str
    trigger_vk: Optional[int]
    mode: "MacroMode"
    enabled: bool
    humanize_jitter_pct: int
    steps: Tuple[_StepSnap, ...]


@dataclass
class _RuntimeState:
    held: bool = False
    toggle_running: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None
    # Keys/mouse buttons this macro has sent a raw KEY_DOWN/MOUSE_DOWN for
    # that haven't been matched by a KEY_UP/MOUSE_UP step yet -- see
    # _force_release_held()'s comment for why this exists.
    held_keys: set = field(default_factory=set)
    held_mouse: set = field(default_factory=set)
    # Guards held_keys/held_mouse specifically. In normal operation only this
    # runtime's own dedicated playback thread ever touches them, so this
    # never actually contends -- but stop() calls _force_release_held() from
    # the Companion thread after join(timeout=1.0), and if a thread somehow
    # didn't finish within that timeout (shouldn't happen -- every wait
    # inside _execute_step is interruptible via cancel_event -- but a hang
    # elsewhere could still leave one alive), that thread could still be
    # concurrently mutating these same sets. This closes that race rather
    # than leaving it open on a "should never happen" assumption.
    state_lock: threading.Lock = field(default_factory=threading.Lock)


class MacroEngine:
    """Public thread-safe methods (callable from the Companion window's own
    thread):

        start() / stop()
        update_snapshot(macros_state)
        handle_effective_event(event)   -- registered as a remapper.py listener
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._macros: List[_MacroSnap] = []
        self._runtime: Dict[str, _RuntimeState] = {}
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        with self._lock:
            runtimes = list(self._runtime.values())
            self._runtime.clear()
        for rt in runtimes:
            rt.held = False
            rt.toggle_running = False
            rt.cancel_event.set()
        for rt in runtimes:
            if rt.thread is not None and rt.thread.is_alive():
                rt.thread.join(timeout=1.0)
        # `self._runtime.clear()` above means each loop's own next
        # `_get_existing_runtime()` call reads back None, not this `rt` --
        # its in-loop cleanup branch (see `_hold_loop`/`_toggle_loop`) can
        # never fire on this shutdown path, so it's done explicitly here
        # instead, once every thread has actually stopped touching `rt`.
        for rt in runtimes:
            self._force_release_held(rt)

    def update_snapshot(self, macros_state: "MacrosState") -> None:
        snaps: List[_MacroSnap] = []
        for macro in macros_state.macros:
            steps = tuple(
                _StepSnap(
                    kind=step.kind,
                    key_vk=step.key.vk_code,
                    mouse_button=step.mouse_button,
                    scroll_delta=step.scroll_delta,
                    delay_ms=step.delay_ms,
                )
                for step in macro.steps
            )
            snaps.append(
                _MacroSnap(
                    id=macro.id,
                    name=macro.name,
                    trigger_vk=macro.trigger.vk_code,
                    mode=macro.mode,
                    enabled=macro.enabled,
                    humanize_jitter_pct=macro.humanize_jitter_pct,
                    steps=steps,
                )
            )
        with self._lock:
            self._macros = snaps

    # ------------------------------------------------------------------
    # Trigger detection -- called from remapper.py's hook thread
    # ------------------------------------------------------------------

    def handle_effective_event(self, event: EffectiveInputEvent) -> None:
        if not self._started or event.vk_code is None:
            return
        with self._lock:
            macros = self._macros
        for macro in macros:
            if not macro.enabled or macro.trigger_vk is None:
                continue
            if macro.trigger_vk != event.vk_code:
                continue
            self._on_trigger(macro, event.up)
            break  # first enabled macro bound to this trigger wins

    def handle_gate_closed(self) -> None:
        """Registered as a `remapper.RemapperEngine.add_gate_close_listener()`
        callback -- fires the instant the window-filter gate closes (targeted
        process just lost focus). Stops every currently-running Hold/Toggle
        session exactly as if its trigger had been released/toggled off
        normally.

        Needed because a session already running on its own dedicated thread
        has no other way to learn about a trigger release that happens while
        the gate is closed: `remapper.py`'s `_handle()` returns before
        publishing anything at all while the gate is shut, so that release
        never reaches `handle_effective_event()` below. Without this, a Hold
        macro whose trigger got released during a focus loss would keep
        re-firing its steps into whatever now has focus, indefinitely, until
        the process regained focus and the key were released again -- see
        remapper.py's `add_gate_close_listener()` docstring for the full
        scenario. Flipping `held`/`toggle_running` here is all that's needed;
        each session's own loop (`_hold_loop`/`_toggle_loop`) notices on its
        next iteration and runs its existing `_force_release_held()` cleanup
        exactly as it would for a normal release.

        Runs on the hook thread -- must stay fast, same constraint as
        `handle_effective_event()`."""
        with self._lock:
            runtimes = list(self._runtime.values())
        for rt in runtimes:
            if rt.held:
                rt.held = False
                rt.cancel_event.set()
            if rt.toggle_running:
                rt.toggle_running = False
                rt.cancel_event.set()

    def _get_runtime(self, macro_id: str) -> _RuntimeState:
        with self._lock:
            rt = self._runtime.get(macro_id)
            if rt is None:
                rt = _RuntimeState()
                self._runtime[macro_id] = rt
            return rt

    def _on_trigger(self, macro: _MacroSnap, is_up: bool) -> None:
        rt = self._get_runtime(macro.id)

        if macro.mode == MacroMode.HOLD:
            if not is_up:
                rt.held = True
                rt.cancel_event.clear()
                self._ensure_thread(rt, macro.id, self._hold_loop)
            else:
                rt.held = False
                rt.cancel_event.set()  # interrupt an in-progress Delay sleep promptly

        elif macro.mode == MacroMode.TOGGLE:
            if not is_up:
                return
            if rt.toggle_running:
                rt.toggle_running = False
                rt.cancel_event.set()
            else:
                rt.toggle_running = True
                rt.cancel_event.clear()
                self._ensure_thread(rt, macro.id, self._toggle_loop)

        else:  # Once -- fires on release
            if is_up:
                threading.Thread(
                    target=self._once_run, args=(macro.id,), daemon=True, name=f"SGO-Macro-{macro.id}-once"
                ).start()

    @staticmethod
    def _ensure_thread(rt: _RuntimeState, macro_id: str, target) -> None:
        if rt.thread is not None and rt.thread.is_alive():
            return  # loop is already running and will pick up the new state
        rt.thread = threading.Thread(target=target, args=(macro_id,), daemon=True, name=f"SGO-Macro-{macro_id}")
        rt.thread.start()

    # ------------------------------------------------------------------
    # Playback loops (dedicated background threads -- never the hook thread)
    # ------------------------------------------------------------------

    def _find_macro(self, macro_id: str) -> Optional[_MacroSnap]:
        with self._lock:
            return next((m for m in self._macros if m.id == macro_id), None)

    def _get_existing_runtime(self, macro_id: str) -> Optional[_RuntimeState]:
        with self._lock:
            return self._runtime.get(macro_id)

    def _hold_loop(self, macro_id: str) -> None:
        while True:
            rt = self._get_existing_runtime(macro_id)
            if rt is None or not rt.held:
                if rt is not None:
                    self._force_release_held(rt)
                return
            macro = self._find_macro(macro_id)
            if macro is None or not macro.enabled:
                self._force_release_held(rt)
                return
            self._execute_steps(macro, rt.cancel_event, rt)
            rt.cancel_event.clear()
            time.sleep(_LOOP_YIELD_SEC)

    def _toggle_loop(self, macro_id: str) -> None:
        while True:
            rt = self._get_existing_runtime(macro_id)
            if rt is None or not rt.toggle_running:
                if rt is not None:
                    self._force_release_held(rt)
                return
            macro = self._find_macro(macro_id)
            if macro is None or not macro.enabled:
                rt.toggle_running = False
                self._force_release_held(rt)
                return
            self._execute_steps(macro, rt.cancel_event, rt)
            rt.cancel_event.clear()
            time.sleep(_LOOP_YIELD_SEC)

    def _once_run(self, macro_id: str) -> None:
        macro = self._find_macro(macro_id)
        if macro is None or not macro.enabled:
            return
        self._execute_steps(macro, threading.Event(), _RuntimeState())

    def _force_release_held(self, rt: _RuntimeState) -> None:
        """Kill switch for a Hold/Toggle session that's ending (trigger
        released, toggled off, macro disabled/removed, or engine stop()):
        release any key/mouse button this macro's own KEY_DOWN/MOUSE_DOWN
        steps left down without a matching UP.

        Needed because a Hold/Toggle session can be cancelled between steps
        at any point -- if that happens right after a raw KEY_DOWN/MOUSE_DOWN
        and before its matching UP later in the sequence, that step never
        runs, and SendInput-injected key state outlives both this thread and
        the triggering keypress (same class of bug remapper.py's
        `_force_release_pending()` exists for). KEY_TAP/MOUSE_CLICK need no
        such tracking -- they always send their own "up" immediately after,
        even when the wait between them is cut short by cancellation.

        Read-and-clear happens under `rt.state_lock`; the actual `send_key`/
        `send_mouse_button` calls run after releasing it, same pattern
        remapper.py's own force-release paths use -- never hold a lock
        across a call into the OS."""
        with rt.state_lock:
            keys = list(rt.held_keys)
            rt.held_keys.clear()
            buttons = list(rt.held_mouse)
            rt.held_mouse.clear()

        for vk in keys:
            input_inject.send_key(vk, key_up=True)

        for label in buttons:
            button = _MOUSE_BUTTON_BY_LABEL.get(label)
            if button is not None:
                input_inject.send_mouse_button(button, up=True)

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _execute_steps(self, macro: _MacroSnap, cancel_event: threading.Event, rt: _RuntimeState) -> bool:
        """Returns True if every step ran; False if interrupted mid-sequence
        (Hold released / Toggle turned off). `rt` tracks any KEY_DOWN/
        MOUSE_DOWN left outstanding by an interruption -- see
        `_force_release_held()`."""
        for step in macro.steps:
            if cancel_event.is_set():
                return False
            try:
                self._execute_step(step, macro.humanize_jitter_pct, cancel_event, rt)
            except Exception:
                traceback.print_exc()
            if cancel_event.is_set():
                return False
        return True

    def _execute_step(self, step: _StepSnap, jitter_pct: int, cancel_event: threading.Event, rt: _RuntimeState) -> None:
        kind = step.kind

        if kind == MacroStepKind.DELAY:
            seconds = self._humanized_delay_seconds(step.delay_ms, jitter_pct)
            cancel_event.wait(seconds)  # interruptible sleep -- the one real jitter target
            return

        if kind == MacroStepKind.KEY_DOWN:
            if step.key_vk is not None:
                input_inject.send_key(step.key_vk, key_up=False)
                with rt.state_lock:
                    rt.held_keys.add(step.key_vk)
            return
        if kind == MacroStepKind.KEY_UP:
            if step.key_vk is not None:
                input_inject.send_key(step.key_vk, key_up=True)
                with rt.state_lock:
                    rt.held_keys.discard(step.key_vk)
            return
        if kind == MacroStepKind.KEY_TAP:
            if step.key_vk is not None:
                input_inject.send_key(step.key_vk, key_up=False)
                cancel_event.wait(_TAP_HOLD_SEC)
                input_inject.send_key(step.key_vk, key_up=True)  # always sent, even if the wait above was cut short
            return

        button = _MOUSE_BUTTON_BY_LABEL.get(step.mouse_button)
        if kind == MacroStepKind.MOUSE_DOWN:
            if button is not None:
                input_inject.send_mouse_button(button, up=False)
                with rt.state_lock:
                    rt.held_mouse.add(step.mouse_button)
            return
        if kind == MacroStepKind.MOUSE_UP:
            if button is not None:
                input_inject.send_mouse_button(button, up=True)
                with rt.state_lock:
                    rt.held_mouse.discard(step.mouse_button)
            return
        if kind == MacroStepKind.MOUSE_CLICK:
            if button is not None:
                input_inject.send_mouse_button(button, up=False)
                cancel_event.wait(_CLICK_HOLD_SEC)
                input_inject.send_mouse_button(button, up=True)  # always sent, even if the wait above was cut short
            return

        if kind == MacroStepKind.SCROLL:
            input_inject.send_scroll(step.scroll_delta)
            return

    @staticmethod
    def _humanized_delay_seconds(delay_ms: int, jitter_pct: int) -> float:
        ms = max(0, delay_ms)
        if jitter_pct > 0:
            frac = min(100, jitter_pct) / 100.0
            ms = ms * random.uniform(1.0 - frac, 1.0 + frac)
        return max(0.0, ms) / 1000.0


# Process-wide singleton -- main.py starts/stops this alongside the
# Companion window's lifecycle and wires it to remapper_engine's
# effective-event stream.
macro_engine = MacroEngine()
