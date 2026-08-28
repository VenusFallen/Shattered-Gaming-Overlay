"""macro_engine.py -- executes `AppState.macros.macros` (`MacroDef`) entries:
Once/Hold/Toggle trigger modes, editable delay steps, per-macro enable, and
real humanize jitter on Delay-step timing. Pure user-mode: playback is only
ever `input_inject.send_key` / `send_mouse_button` / `send_scroll` calls
(discrete actions -- no continuous mouse-movement synthesis; that's a hard
scope boundary for this project).

Trigger source: remapper.py's effective event stream, not raw hooks
---------------------------------------------------------------------
This module installs no hook of its own. It subscribes to
`remapper.remapper_engine.add_effective_listener(...)` (wired in main.py)
and matches macro triggers against the *post-remap* identity stream that
publishes -- see remapper.py's module docstring for why. This is also how
this module inherits the window-focus gate for free: remapper.py stops
publishing entirely while a targeted process is unfocused, so macros
naturally go inert alongside the Remapper without this module needing its
own separate window_select.py lookup.

Threading
---------
`handle_effective_event()` runs on the input-hook's own background thread
(via remapper.py) and must stay fast/non-blocking (per input_hooks.py's
hook-responsiveness-timeout warning) -- it only ever does a dict/list lookup
and starts/signals a background thread, never sleeps or blocks itself.
Each active macro trigger session (a Hold being held, a Toggle being on, an
Once firing) runs its actual step playback -- including all Delay-step
sleeping -- on its own dedicated background thread, so a macro's delays can
never block hook processing.

`update_snapshot(macros_state)` is called once per Companion-window frame
from main.py (same lock-guarded-snapshot pattern hud_overlay.py established
for the crosshair) and copies MacroDef/MacroStep's plain-data fields into
immutable snapshot dataclasses -- the live, UI-mutable MacroDef/MacroStep
instances themselves are never read from a background thread.

Humanize jitter
----------------
The one documented behavioral detection vector for SendInput macros is
inhumanly-regular timing -- add jitter to macro playback delays.
`MacroDef.humanize_jitter_pct` is applied as real random
variance (uniform(1 - pct, 1 + pct)) to every Delay step's sleep duration,
every playback, not just a cosmetic display value.
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

# Small fixed hold duration for a "tap"/"click" step (down then up) so the
# receiving app/game registers a real press rather than a same-instant
# down+up some input readers coalesce away. Intentionally NOT humanized --
# the jitter requirement is scoped to Delay-step timing specifically; this
# is a fixed physical-plausibility floor, not a
# recorded/played-back delay the user configured.
_TAP_HOLD_SEC = 0.03
_CLICK_HOLD_SEC = 0.04

# How often a Hold/Toggle loop with no Delay steps at all yields to the OS
# between iterations, so it can't spin the CPU at 100% -- mirrors R9Tools'
# same `time.sleep(0.001)` between playback iterations.
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

        else:  # Once -- fires on release, same precedent as R9Tools
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
                return
            macro = self._find_macro(macro_id)
            if macro is None or not macro.enabled:
                return
            self._execute_steps(macro, rt.cancel_event)
            rt.cancel_event.clear()
            time.sleep(_LOOP_YIELD_SEC)

    def _toggle_loop(self, macro_id: str) -> None:
        while True:
            rt = self._get_existing_runtime(macro_id)
            if rt is None or not rt.toggle_running:
                return
            macro = self._find_macro(macro_id)
            if macro is None or not macro.enabled:
                if rt is not None:
                    rt.toggle_running = False
                return
            self._execute_steps(macro, rt.cancel_event)
            rt.cancel_event.clear()
            time.sleep(_LOOP_YIELD_SEC)

    def _once_run(self, macro_id: str) -> None:
        macro = self._find_macro(macro_id)
        if macro is None or not macro.enabled:
            return
        self._execute_steps(macro, threading.Event())

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _execute_steps(self, macro: _MacroSnap, cancel_event: threading.Event) -> bool:
        """Returns True if every step ran; False if interrupted mid-sequence
        (Hold released / Toggle turned off)."""
        for step in macro.steps:
            if cancel_event.is_set():
                return False
            try:
                self._execute_step(step, macro.humanize_jitter_pct, cancel_event)
            except Exception:
                traceback.print_exc()
            if cancel_event.is_set():
                return False
        return True

    def _execute_step(self, step: _StepSnap, jitter_pct: int, cancel_event: threading.Event) -> None:
        kind = step.kind

        if kind == MacroStepKind.DELAY:
            seconds = self._humanized_delay_seconds(step.delay_ms, jitter_pct)
            cancel_event.wait(seconds)  # interruptible sleep -- the one real jitter target
            return

        if kind == MacroStepKind.KEY_DOWN:
            if step.key_vk is not None:
                input_inject.send_key(step.key_vk, key_up=False)
            return
        if kind == MacroStepKind.KEY_UP:
            if step.key_vk is not None:
                input_inject.send_key(step.key_vk, key_up=True)
            return
        if kind == MacroStepKind.KEY_TAP:
            if step.key_vk is not None:
                input_inject.send_key(step.key_vk, key_up=False)
                cancel_event.wait(_TAP_HOLD_SEC)
                input_inject.send_key(step.key_vk, key_up=True)
            return

        button = _MOUSE_BUTTON_BY_LABEL.get(step.mouse_button)
        if kind == MacroStepKind.MOUSE_DOWN:
            if button is not None:
                input_inject.send_mouse_button(button, up=False)
            return
        if kind == MacroStepKind.MOUSE_UP:
            if button is not None:
                input_inject.send_mouse_button(button, up=True)
            return
        if kind == MacroStepKind.MOUSE_CLICK:
            if button is not None:
                input_inject.send_mouse_button(button, up=False)
                cancel_event.wait(_CLICK_HOLD_SEC)
                input_inject.send_mouse_button(button, up=True)
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


# ---------------------------------------------------------------------------
# Process-wide singleton -- main.py starts/stops this alongside the
# Companion window's own lifecycle, and wires it to remapper_engine's
# effective-event stream (hud_overlay/capture_service/remapper_engine
# pattern).
# ---------------------------------------------------------------------------

macro_engine = MacroEngine()
