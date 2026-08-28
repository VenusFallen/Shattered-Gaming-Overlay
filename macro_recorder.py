"""macro_recorder.py -- "press Record, perform the actions, press Stop, get
real MacroStep objects" capture for the Macros panel's step editor.

Why not reuse key_capture.capture_service
-------------------------------------------
`key_capture.KeyCaptureService` is built for exactly one job: observe the
*next single* physical key/button press for a "press a key to bind" button,
then go idle again. Macro recording needs a continuous multi-event session
(potentially many key/mouse/scroll events, with real hold durations) running
*alongside* whatever `capture_service` might independently be doing elsewhere
in the same panel (e.g. a step's own key-capture bind button) -- reusing it
directly would mean one "active capture" flag trying to serve two different
lifecycles at once. So this module owns its own dedicated `HookManager`
instance instead.

Why not reuse remapper.remapper_engine's hook
------------------------------------------------
`remapper_engine` actively suppresses matched source events (that's its
whole job) and only publishes its own post-remap `EffectiveInputEvent`
stream, not raw physical events. Recording needs a faithful, unsuppressed
view of exactly what the user physically pressed -- for a user who has
active remaps configured, matching against the remapper's effective stream
(or worse, sharing its suppressing hook) would corrupt the recording. So
this module installs a fresh, non-suppressing `HookManager` of its own
(every registered callback here returns `None`, same "observe-only" contract
`key_capture.py` uses).

Threading discipline
---------------------
`HookManager` callbacks fire on their own background message-pump thread.
This module only ever appends immutable `_RawEvent` records to a
lock-guarded list from that thread -- it never touches `app_state.MacroDef`/
`MacroStep` (real dataclasses meant for the Companion window's own thread)
from there. `stop()` -- called from the Companion window's thread -- drains
the buffer and converts it into `RecordedStep` values, which
`panels/macros.py` then turns into real `MacroStep` objects via
`macro.add_step()` (so ID generation stays centralized in app_state.py,
rather than this module reaching into app_state's private `_next_id`).

No mouse-movement capture
---------------------------
`HookManager.on_mouse_move` is intentionally never registered here. This
project does not synthesize continuous/relative mouse movement anywhere
(that's a hard scope boundary for this project) -- recording
simply never listens for movement in the first place, rather than listening
and then discarding it.

Tap-vs-hold / delay conversion rules
--------------------------------------
- A key/mouse-button press+release with a hold duration <=
  `TAP_HOLD_THRESHOLD_MS` (120ms -- squarely in the 80-150ms range a quick,
  deliberate human tap falls into, matching the tap/hold convention used
  elsewhere in this project; long enough that an ordinary fast tap never
  gets mis-split into DOWN/DELAY/UP, short enough that a genuine held key
  reliably clears it) collapses into a single `KEY_TAP` / `MOUSE_CLICK` step.
- A longer hold becomes an explicit `KEY_DOWN`, then a `DELAY` step sized to
  the real measured hold duration, then `KEY_UP` (and the mouse-button
  equivalents) -- so a held movement key actually reproduces the hold.
- A `DELAY` step is synthesized between consecutive recorded actions sized
  to the real elapsed gap between them, but only if that gap is >=
  `MIN_DELAY_MS` (20ms). Below that, the gap is dropped entirely rather than
  emitting a near-zero delay step for effectively-simultaneous events (e.g.
  a fast double-click, or scroll ticks that land in the same hook tick).
- OS key-repeat (Windows re-fires WM_KEYDOWN continuously while a key is
  physically held) is filtered: a key-down while that same key is already
  recorded as open is treated as a repeat and ignored, not a second press.
- A key/button still open when `stop()` is called (the user stopped
  recording mid-hold) is closed as a hold ending at the stop time, so
  recording never silently drops a still-held key -- it just becomes a
  DOWN/DELAY/UP triple ending exactly when Stop was pressed.
- Escape is never recorded as a step at all (filtered in the hook callbacks,
  before buffering) -- it's reserved as the "cancel/stop recording" key
  throughout this Companion window (see `_handle_trigger_capture`/
  `_handle_step_key_capture` in panels/macros.py), so it's already a key a
  user can never bind/record via this UI, consistent with that precedent.
- The physical left-click used to press the Stop button itself is never
  recorded as a trailing step. `panels/macros.py` triggers `stop()` on
  mouse-DOWN (`is_item_activated()`), not the button's own release-triggered
  "clicked" return, so recording is flagged off before the corresponding
  mouse-up can be buffered; any still-open LEFT press at the moment `stop()`
  runs is discarded rather than closed-as-a-hold (see `_convert_events`).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

from app_state import MacroStepKind
from input_hooks import HookManager, KeyEvent, MouseButtonEvent, MouseScrollEvent
from input_inject import MouseButton
from key_capture import KeyBind, UNBOUND

# --- tunables (documented above) -------------------------------------------
TAP_HOLD_THRESHOLD_MS = 120
MIN_DELAY_MS = 20

_VK_ESCAPE = 0x1B

_MOUSE_BUTTON_LABELS: Dict[MouseButton, str] = {
    MouseButton.LEFT: "Left",
    MouseButton.RIGHT: "Right",
    MouseButton.MIDDLE: "Middle",
    MouseButton.X1: "X1",
    MouseButton.X2: "X2",
}


class _RawKind(Enum):
    KEY = "key"
    MOUSE_BUTTON = "mouse_button"
    SCROLL = "scroll"


@dataclass(frozen=True)
class _RawEvent:
    kind: _RawKind
    t: float  # time.monotonic() seconds, hook-thread timestamp
    up: bool = False
    vk_code: Optional[int] = None
    name: str = ""
    mouse_button: Optional[MouseButton] = None
    scroll_delta: int = 0


@dataclass(frozen=True)
class RecordedStep:
    """Plain conversion result -- NOT app_state.MacroStep. The panel turns
    each of these into a real MacroStep via macro.add_step() so ID
    generation stays centralized in app_state.py."""

    kind: MacroStepKind
    key: KeyBind = UNBOUND
    mouse_button: str = "Left"
    scroll_delta: int = 120
    delay_ms: int = 50


class MacroRecorder:
    """One dedicated hook, started lazily on first Record click and left
    running afterward (same "cheap to keep alive" precedent as
    key_capture.KeyCaptureService)."""

    def __init__(self) -> None:
        self._hook: Optional[HookManager] = None
        self._lock = threading.Lock()
        self._recording = False
        self._events: List[_RawEvent] = []
        self._started_at: Optional[float] = None

    def _ensure_hook_running(self) -> None:
        if self._hook is None:
            hook = HookManager()
            hook.on_key_down(self._on_key)
            hook.on_key_up(self._on_key)
            hook.on_mouse_button(self._on_mouse_button)
            hook.on_scroll(self._on_scroll)
            # Deliberately no on_mouse_move registration -- see module docstring.
            self._hook = hook
        if not self._hook.is_running:
            self._hook.start()

    # -- public API (Companion-window thread) ---------------------------

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def elapsed_seconds(self) -> float:
        with self._lock:
            if not self._recording or self._started_at is None:
                return 0.0
            return time.monotonic() - self._started_at

    def start(self) -> None:
        self._ensure_hook_running()
        with self._lock:
            self._events = []
            self._recording = True
            self._started_at = time.monotonic()

    def stop(self) -> List[RecordedStep]:
        """Stop recording and convert whatever was captured into
        RecordedStep values. Safe to call even if nothing was recorded
        (returns an empty list)."""
        with self._lock:
            self._recording = False
            events = self._events
            self._events = []
        stop_time = time.monotonic()
        return _convert_events(events, stop_time)

    def cancel(self) -> None:
        """Stop recording and discard whatever was captured -- no steps
        produced."""
        with self._lock:
            self._recording = False
            self._events = []
            self._started_at = None

    def shutdown(self) -> None:
        """Uninstall the hook if it was ever started. Safe no-op otherwise."""
        with self._lock:
            self._recording = False
            self._events = []
        if self._hook is not None and self._hook.is_running:
            self._hook.stop()

    # -- hook callbacks (hook thread -- buffer only, never touch AppState) -

    def _record(self, event: _RawEvent) -> None:
        with self._lock:
            if not self._recording:
                return
            self._events.append(event)

    def _on_key(self, event: KeyEvent) -> Optional[bool]:
        if event.injected or event.from_self:
            return None
        if event.vk_code == _VK_ESCAPE:
            return None  # reserved stop/cancel key -- never recorded, see docstring
        name = event.name or f"VK_{event.vk_code:#04x}"
        self._record(_RawEvent(kind=_RawKind.KEY, t=time.monotonic(), up=event.up, vk_code=event.vk_code, name=name))
        return None  # never suppress -- passive observation only

    def _on_mouse_button(self, event: MouseButtonEvent) -> Optional[bool]:
        if event.injected or event.from_self:
            return None
        self._record(
            _RawEvent(kind=_RawKind.MOUSE_BUTTON, t=time.monotonic(), up=event.up, mouse_button=event.button)
        )
        return None

    def _on_scroll(self, event: MouseScrollEvent) -> Optional[bool]:
        if event.injected or event.from_self:
            return None
        self._record(_RawEvent(kind=_RawKind.SCROLL, t=time.monotonic(), scroll_delta=event.delta))
        return None


# ---------------------------------------------------------------------------
# Raw-event -> RecordedStep conversion (pure function, unit-testable without
# a live hook -- mirrors input_hooks.py/input_inject.py's own
# "cleanly separated so either can be unit-tested" goal).
# ---------------------------------------------------------------------------


@dataclass
class _Action:
    """One completed down/up pair (or an instantaneous scroll tick),
    resolved into its final RecordedStep shape but not yet placed into the
    output list. Kept separate from emission so overlapping presses (e.g. a
    movement key held while a quick tap happens mid-hold) can be sorted back
    into real press-start order before any Delay steps are computed -- see
    _convert_events' docstring note below on why that matters and its
    limits."""

    start: float
    end: float
    step_list: List[RecordedStep]


def _convert_events(events: List[_RawEvent], stop_time: float) -> List[RecordedStep]:
    """Two passes, deliberately:

    1. Walk the raw events and resolve every completed down/up pair (or
       scroll tick) into an `_Action` -- this is where tap-vs-hold and OS
       key-repeat filtering happen (see module docstring), independent of
       final ordering.
    2. Sort `_Action`s by their *start* (press) time and emit them in that
       order, inserting a Delay step for any real gap between one action's
       end and the next action's start.

    Sorting by start time (not by whichever event closes first) matters for
    overlapping presses: if a movement key is held down, then a second key
    is tapped mid-hold and released before the movement key is, resolving
    actions in raw close-order would emit the tap *before* the held key's
    own Down step, silently reordering what was actually pressed first.
    Sorting by start time keeps that press order intact.

    This still can't represent true concurrency -- MacroStep sequences are
    strictly linear (macro_engine.py's own _execute_steps runs one step at a
    time), so two genuinely overlapping holds recorded together will always
    serialize into back-to-back blocks rather than a real simultaneous hold.
    That's an inherent limitation of the step-sequence format itself, not
    something recording can paper over; simple single-action-at-a-time
    recordings (the common case) aren't affected.
    """
    actions: List[_Action] = []
    open_keys: Dict[int, Tuple[float, str]] = {}  # vk_code -> (down_time, name)
    open_mouse: Dict[MouseButton, float] = {}  # button -> down_time

    def build_key_action(vk: int, name: str, down_t: float, up_t: float) -> None:
        hold_ms = (up_t - down_t) * 1000.0
        bind = KeyBind(vk_code=vk, name=name)
        if hold_ms <= TAP_HOLD_THRESHOLD_MS:
            step_list = [RecordedStep(kind=MacroStepKind.KEY_TAP, key=bind)]
        else:
            step_list = [
                RecordedStep(kind=MacroStepKind.KEY_DOWN, key=bind),
                RecordedStep(kind=MacroStepKind.DELAY, delay_ms=max(0, int(round(hold_ms)))),
                RecordedStep(kind=MacroStepKind.KEY_UP, key=bind),
            ]
        actions.append(_Action(start=down_t, end=up_t, step_list=step_list))

    def build_mouse_action(button: MouseButton, down_t: float, up_t: float) -> None:
        hold_ms = (up_t - down_t) * 1000.0
        label = _MOUSE_BUTTON_LABELS[button]
        if hold_ms <= TAP_HOLD_THRESHOLD_MS:
            step_list = [RecordedStep(kind=MacroStepKind.MOUSE_CLICK, mouse_button=label)]
        else:
            step_list = [
                RecordedStep(kind=MacroStepKind.MOUSE_DOWN, mouse_button=label),
                RecordedStep(kind=MacroStepKind.DELAY, delay_ms=max(0, int(round(hold_ms)))),
                RecordedStep(kind=MacroStepKind.MOUSE_UP, mouse_button=label),
            ]
        actions.append(_Action(start=down_t, end=up_t, step_list=step_list))

    for e in events:
        if e.kind == _RawKind.KEY:
            assert e.vk_code is not None
            if not e.up:
                if e.vk_code not in open_keys:
                    open_keys[e.vk_code] = (e.t, e.name)
                # else: OS key-repeat while held -- ignore, see docstring
            else:
                opened = open_keys.pop(e.vk_code, None)
                if opened is not None:
                    down_t, name = opened
                    build_key_action(e.vk_code, name, down_t, e.t)
                # else: up with no matching down (recording started mid-hold) -- ignore

        elif e.kind == _RawKind.MOUSE_BUTTON:
            assert e.mouse_button is not None
            if not e.up:
                if e.mouse_button not in open_mouse:
                    open_mouse[e.mouse_button] = e.t
            else:
                down_t = open_mouse.pop(e.mouse_button, None)
                if down_t is not None:
                    build_mouse_action(e.mouse_button, down_t, e.t)

        elif e.kind == _RawKind.SCROLL:
            actions.append(
                _Action(
                    start=e.t,
                    end=e.t,
                    step_list=[RecordedStep(kind=MacroStepKind.SCROLL, scroll_delta=e.scroll_delta)],
                )
            )

    # Anything still open when recording stopped: close it as a hold ending
    # at stop_time, so a key/button still physically held when Stop was
    # pressed still round-trips instead of being silently dropped.
    for vk, (down_t, name) in open_keys.items():
        build_key_action(vk, name, down_t, stop_time)
    for button, down_t in open_mouse.items():
        if button == MouseButton.LEFT:
            # Discard, don't close-as-hold: stop() is only ever reachable via
            # a left-click on the Stop button in the UI (see
            # panels/macros.py::_handle_recording, which now triggers on
            # mouse-DOWN via is_item_activated() specifically to make this
            # true as early as possible). A mouse has exactly one left
            # button, so an open LEFT press at this exact instant can only
            # be that same triggering click -- it can't simultaneously be
            # clicking Stop AND independently holding left elsewhere. Right/
            # Middle/X1/X2 and all keys are real holds and still close normally.
            continue
        build_mouse_action(button, down_t, stop_time)

    actions.sort(key=lambda a: a.start)

    steps: List[RecordedStep] = []
    last_end: Optional[float] = None
    for action in actions:
        if last_end is not None:
            gap_ms = (action.start - last_end) * 1000.0
            if gap_ms >= MIN_DELAY_MS:
                steps.append(RecordedStep(kind=MacroStepKind.DELAY, delay_ms=int(round(gap_ms))))
        steps.extend(action.step_list)
        last_end = action.end

    return steps


# ---------------------------------------------------------------------------
# Process-wide singleton -- same "one shared instance" precedent as
# key_capture.capture_service.
# ---------------------------------------------------------------------------

macro_recorder = MacroRecorder()
