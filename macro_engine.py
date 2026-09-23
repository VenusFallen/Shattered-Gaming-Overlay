"""Plays back AppState.macros.macros (MacroDef) entries: Once/Hold/Toggle
modes, per-step humanize jitter, and the window-focus gate inherited from
remapper.py's effective-event stream."""

from __future__ import annotations

import math
import random
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import input_inject
from input_inject import MouseButton

try:  # pragma: no cover - only used for type hints
    from app_state import MacroMode, MacroStepKind, MacrosState, SettingsState
except Exception:  # pragma: no cover
    MacroMode = object  # type: ignore
    MacroStepKind = object  # type: ignore
    MacrosState = object  # type: ignore
    SettingsState = object  # type: ignore

from remapper import EffectiveInputEvent

_MOUSE_BUTTON_BY_LABEL = {
    "Left": MouseButton.LEFT,
    "Right": MouseButton.RIGHT,
    "Middle": MouseButton.MIDDLE,
    "X1": MouseButton.X1,
    "X2": MouseButton.X2,
}

# Not humanized -- jitter only applies to Delay steps.
_TAP_HOLD_SEC = 0.03
_CLICK_HOLD_SEC = 0.04

# Yield between Hold/Toggle iterations so an empty loop can't spin the CPU at 100%.
_LOOP_YIELD_SEC = 0.001


@dataclass(frozen=True)
class _StepSnap:
    id: str
    kind: "MacroStepKind"
    key_vk: Optional[int]
    mouse_button: str
    scroll_delta: int
    delay_ms: int
    move_x: int
    move_y: int
    path_wobble_pct: int
    move_speed_pct: int


@dataclass(frozen=True)
class _MacroSnap:
    id: str
    name: str
    trigger_vk: Optional[int]
    modifier_vks: Tuple[int, ...]
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
    # Whether this macro's trigger press had every required modifier held at that instant.
    combo_armed: bool = False
    # Raw KEY_DOWN/MOUSE_DOWN sent without a matching UP yet -- see _force_release_held().
    held_keys: set = field(default_factory=set)
    held_mouse: set = field(default_factory=set)
    # Guards held_keys/held_mouse against stop()'s post-timeout cleanup path.
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
        # Hook-thread-only, mirrors remapper.py's _physical_down but scoped to the post-remap stream.
        self._effective_down: "set[int]" = set()
        self._mouse_polling_rate_hz: int = 1000

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        self._effective_down.clear()
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
        # Runtime dict is already cleared, so each loop's in-loop cleanup branch can't fire -- do it here instead.
        for rt in runtimes:
            self._force_release_held(rt)

    def update_snapshot(self, macros_state: "MacrosState", settings_state: "SettingsState") -> None:
        self._mouse_polling_rate_hz = settings_state.mouse_polling_rate_hz
        snaps: List[_MacroSnap] = []
        for macro in macros_state.macros:
            steps = tuple(
                _StepSnap(
                    id=step.id,
                    kind=step.kind,
                    key_vk=step.key.vk_code,
                    mouse_button=step.mouse_button,
                    scroll_delta=step.scroll_delta,
                    delay_ms=step.delay_ms,
                    move_x=step.move_x,
                    move_y=step.move_y,
                    path_wobble_pct=step.path_wobble_pct,
                    move_speed_pct=step.move_speed_pct,
                )
                for step in macro.steps
            )
            modifier_vks = tuple(m.vk_code for m in macro.trigger_modifiers if m.vk_code is not None)
            snaps.append(
                _MacroSnap(
                    id=macro.id,
                    name=macro.name,
                    trigger_vk=macro.trigger.vk_code,
                    modifier_vks=modifier_vks,
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

        if event.up:
            self._effective_down.discard(event.vk_code)
        else:
            self._effective_down.add(event.vk_code)

        with self._lock:
            macros = self._macros
        for macro in macros:
            if not macro.enabled or macro.trigger_vk is None:
                continue
            if macro.trigger_vk != event.vk_code:
                continue

            rt = self._get_runtime(macro.id)
            if not event.up:
                # Modifiers are checked at press time only -- releasing one mid-Hold doesn't interrupt the session.
                armed = all(vk in self._effective_down for vk in macro.modifier_vks)
                rt.combo_armed = armed
                if not armed:
                    continue  # another macro on the same key may still match
            elif macro.modifier_vks and not rt.combo_armed:
                continue  # release doesn't correspond to an armed press of this macro's trigger

            self._on_trigger(macro, event.up)
            break  # first enabled, modifier-satisfied macro bound to this trigger wins

    def handle_gate_closed(self) -> None:
        """Registered as remapper.RemapperEngine's gate-close callback --
        stops every running Hold/Toggle session when the targeted window
        loses focus, since a focus-closed gate never publishes the release
        that would normally stop them."""
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
        """Releases any key/mouse button this macro's own KEY_DOWN/MOUSE_DOWN
        steps left down without a matching UP, when a Hold/Toggle session
        ends mid-sequence."""
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
        """Returns True if every step ran; False if interrupted mid-sequence."""
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
            cancel_event.wait(seconds)  # interruptible sleep
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
                input_inject.send_key(step.key_vk, key_up=True)  # always sent, even if cut short
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
                input_inject.send_mouse_button(button, up=True)  # always sent, even if cut short
            return

        if kind == MacroStepKind.SCROLL:
            input_inject.send_scroll(step.scroll_delta)
            return

        if kind == MacroStepKind.MOUSE_MOVE_TO:
            # Always glides -- an instant absolute teleport is never reachable through this step, by design.
            x, y = self._humanized_move_to(step.move_x, step.move_y, jitter_pct)
            try:
                cur_x, cur_y = input_inject.get_cursor_pos()
            except OSError:
                input_inject.send_mouse_move_absolute(x, y)
                return
            interval = 1.0 / max(1, self._mouse_polling_rate_hz)
            for step_dx, step_dy in self._move_to_glide_offsets(cur_x, cur_y, x, y, self._mouse_polling_rate_hz, step.move_speed_pct):
                input_inject.send_mouse_move_relative(step_dx, step_dy)
                if cancel_event.wait(interval):
                    break  # interrupted mid-glide -- stop where it is
            return
        if kind == MacroStepKind.MOUSE_MOVE_BY:
            # Runs unrestricted in Hold/Toggle -- live testing showed real-world latency defeats recoil-compensation abuse; no cap has been added.
            # step.move_y is "positive = up"; jitter preserves sign, send_mouse_move_by_step() flips it for SendInput.
            # Always stepped and timed at the real polling rate, like Move To -- no single-instant-jump path.
            dx, dy = self._humanized_move_by(step.move_x, step.move_y, jitter_pct)
            interval = 1.0 / max(1, self._mouse_polling_rate_hz)
            for step_dx, step_dy in self._move_by_wave_offsets(dx, dy, step.path_wobble_pct, self._mouse_polling_rate_hz):
                input_inject.send_mouse_move_by_step(step_dx, step_dy)
                if cancel_event.wait(interval):
                    break  # interrupted mid-gesture -- stop where it is
            return

    # Fixed pixel ceiling rather than a percentage of the target coordinate, so jitter stays a plausible click-precision amount at any target.
    _MOVE_TARGET_JITTER_MAX_PX = 6

    @staticmethod
    def _humanized_delay_seconds(delay_ms: int, jitter_pct: int) -> float:
        ms = max(0, delay_ms)
        if jitter_pct > 0:
            frac = min(100, jitter_pct) / 100.0
            ms = ms * random.uniform(1.0 - frac, 1.0 + frac)
        return max(0.0, ms) / 1000.0

    @classmethod
    def _humanized_move_to(cls, x: int, y: int, jitter_pct: int) -> Tuple[int, int]:
        if jitter_pct <= 0:
            return x, y
        radius = min(100, jitter_pct) / 100.0 * cls._MOVE_TARGET_JITTER_MAX_PX
        return (
            x + round(random.uniform(-radius, radius)),
            y + round(random.uniform(-radius, radius)),
        )

    # Hand-speed range a glide's 0-100 speed slider maps onto, px/sec -- shared by Move To and Move By, since
    # both are the same physical hand motion. Duration scales with distance and speed, clamped at both ends
    # (never instant, never absurdly slow).
    _GLIDE_MIN_SPEED_PX_PER_SEC = 800.0
    _GLIDE_MAX_SPEED_PX_PER_SEC = 6000.0
    _GLIDE_MIN_DURATION_SEC = 0.03
    _GLIDE_MAX_DURATION_SEC = 0.5

    @classmethod
    def _glide_duration_sec(cls, total_distance: float, speed_pct: int) -> float:
        frac = min(100, max(0, speed_pct)) / 100.0
        speed = cls._GLIDE_MIN_SPEED_PX_PER_SEC + frac * (cls._GLIDE_MAX_SPEED_PX_PER_SEC - cls._GLIDE_MIN_SPEED_PX_PER_SEC)
        duration = total_distance / speed
        return min(cls._GLIDE_MAX_DURATION_SEC, max(cls._GLIDE_MIN_DURATION_SEC, duration))

    @classmethod
    def _move_to_glide_offsets(cls, cur_x: int, cur_y: int, target_x: int, target_y: int, polling_rate_hz: int, speed_pct: int) -> List[Tuple[int, int]]:
        """Splits a Move To jump into small relative sub-moves timed at
        polling_rate_hz, following a cosine ease-in-out curve (zero velocity
        at both ends) instead of one instantaneous absolute teleport --
        approximates how a real mouse accelerates into and decelerates out
        of a deliberate movement. Reconstructs (target_x, target_y) exactly."""
        dx, dy = target_x - cur_x, target_y - cur_y
        total_distance = math.hypot(dx, dy)
        if total_distance < 1:
            return []

        duration = cls._glide_duration_sec(total_distance, speed_pct)
        n = max(1, round(duration * polling_rate_hz))

        offsets: List[Tuple[int, int]] = []
        sent_x, sent_y = 0, 0  # exact integer running total actually sent so far
        for i in range(1, n + 1):
            t = i / n
            eased_t = 0.5 - 0.5 * math.cos(math.pi * t)  # ease-in-out: zero velocity at t=0 and t=1
            step_dx = round(dx * eased_t) - sent_x
            step_dy = round(dy * eased_t) - sent_y
            sent_x += step_dx
            sent_y += step_dy
            if step_dx or step_dy:
                offsets.append((step_dx, step_dy))
        return offsets

    @staticmethod
    def _humanized_move_by(dx: int, dy: int, jitter_pct: int) -> Tuple[int, int]:
        if jitter_pct <= 0:
            return dx, dy
        frac = min(100, jitter_pct) / 100.0
        return (
            MacroEngine._jitter_component(dx, frac),
            MacroEngine._jitter_component(dy, frac),
        )

    @staticmethod
    def _jitter_component(value: int, frac: float) -> int:
        # Rounds the deviation's magnitude UP, not to nearest -- plain round() collapses back to value for small dx/dy, making jitter invisible.
        if value == 0:
            return 0
        delta = value * random.uniform(-frac, frac)
        if delta == 0:
            return value
        return value + math.ceil(abs(delta)) * (1 if delta > 0 else -1)

    # Fixed mid-range speed -- Move By has no user-facing speed control (yet), unlike Move To's per-step slider.
    _MOVE_BY_GESTURE_SPEED_PCT = 50
    # Max sideways wobble at 100%, as a fraction of total distance so it scales with gesture size.
    _MOVE_BY_WAVE_AMPLITUDE_FRACTION = 0.25
    _MOVE_BY_MAX_WAVE_CYCLES = 3

    @classmethod
    def _move_by_wave_offsets(cls, dx: int, dy: int, wobble_pct: int, polling_rate_hz: int = 1000) -> List[Tuple[int, int]]:
        """Splits one Move By gesture into sub-moves timed at polling_rate_hz,
        following a sine wave perpendicular to the direction of travel (linear
        progress along it -- arrival is the point, not pacing), so it always
        reconstructs (dx, dy) exactly while varying the route, not the
        destination. wobble_pct=0 is just a straight, still real-time-stepped
        line -- there's no separate single-jump path anymore."""
        total_distance = math.hypot(dx, dy)
        if total_distance < 1:
            return []

        frac = min(100, max(0, wobble_pct)) / 100.0
        ux, uy = dx / total_distance, dy / total_distance  # unit vector along the line of travel
        px, py = -uy, ux  # unit vector perpendicular to it

        amplitude = frac * cls._MOVE_BY_WAVE_AMPLITUDE_FRACTION * total_distance
        if frac > 0:
            amplitude = max(amplitude, 1.0)  # floor so small moves still wobble visibly
        cycles = random.randint(1, max(1, round(frac * cls._MOVE_BY_MAX_WAVE_CYCLES))) if frac > 0 else 0
        direction_sign = random.choice((-1, 1))

        duration = cls._glide_duration_sec(total_distance, cls._MOVE_BY_GESTURE_SPEED_PCT)
        n = max(1, round(duration * polling_rate_hz))
        offsets: List[Tuple[int, int]] = []
        sent_x, sent_y = 0, 0  # exact integer running total actually sent so far
        for i in range(1, n + 1):
            t = i / n
            along = t * total_distance
            perp = direction_sign * amplitude * math.sin(cycles * math.pi * t) if cycles else 0.0
            target_x = ux * along + px * perp
            target_y = uy * along + py * perp
            step_dx = round(target_x) - sent_x
            step_dy = round(target_y) - sent_y
            sent_x += step_dx
            sent_y += step_dy
            if step_dx or step_dy:
                offsets.append((step_dx, step_dy))
        return offsets


# Process-wide singleton -- main.py starts/stops this alongside the Companion window.
macro_engine = MacroEngine()
