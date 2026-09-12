"""tests/test_macro_engine_hold_release.py -- kill-switch coverage for
MacroEngine's Hold/Toggle sessions: releasing the trigger (or the macro
getting disabled/removed, or the engine stopping) must never leave a key or
mouse button physically down that a KEY_DOWN/MOUSE_DOWN step sent without
its matching UP ever running.

Found live 2026-09-11: a Hold session cancelled between an unmatched
KEY_DOWN and its later KEY_UP step left the key stuck at the OS level, since
nothing released it once the session stopped. KEY_TAP/MOUSE_CLICK need no
such tracking -- see macro_engine.py's `_force_release_held()` docstring for
why they always self-complete even when cut short.

Exercises `_execute_steps()`/`_force_release_held()` directly for the
deterministic unit-level behavior, and the real `_hold_loop()`/`_toggle_loop()`
methods (called synchronously on the test's own thread, never spawned as a
background thread) for the end-to-end path -- only `time.sleep` is
monkeypatched, to control loop iteration count without a real wait.
input_inject.send_key/send_mouse_button are monkeypatched so nothing here
ever calls real SendInput.
"""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

import input_inject
import macro_engine as macro_engine_module
from app_state import MacroDef, MacroMode, MacroStep, MacroStepKind, MacrosState
from input_inject import MouseButton
from key_capture import KeyBind
from macro_engine import MacroEngine, _MacroSnap, _RuntimeState, _StepSnap
from remapper import EffectiveInputEvent


@pytest.fixture(autouse=True)
def _stub_injection(monkeypatch):
    monkeypatch.setattr(input_inject, "send_key", Mock())
    monkeypatch.setattr(input_inject, "send_mouse_button", Mock())
    yield


class _ScriptedCancelEvent:
    """Returns a pre-programmed sequence of is_set() results, one per call --
    lets a test place the "cancelled" transition at an exact point between
    two steps without any real threading or waiting."""

    def __init__(self, results):
        self._results = list(results)

    def is_set(self) -> bool:
        return self._results.pop(0)

    def wait(self, timeout=None) -> bool:
        return False


def _key_step(step_id: str, kind: MacroStepKind, vk: int) -> MacroStep:
    return MacroStep(id=step_id, kind=kind, key=KeyBind(vk_code=vk, name="A"))


# ---------------------------------------------------------------------------
# _execute_steps -- interruption between an unmatched DOWN and its UP
# ---------------------------------------------------------------------------


def test_execute_steps_interrupted_between_key_down_and_up_leaves_it_tracked():
    engine = MacroEngine()
    steps = (
        _StepSnap(kind=MacroStepKind.KEY_DOWN, key_vk=0x41, mouse_button="Left", scroll_delta=120, delay_ms=0),
        _StepSnap(kind=MacroStepKind.KEY_UP, key_vk=0x41, mouse_button="Left", scroll_delta=120, delay_ms=0),
    )
    macro = _MacroSnap(
        id="m1", name="M", trigger_vk=0x51, mode=MacroMode.HOLD, enabled=True, humanize_jitter_pct=0, steps=steps
    )
    rt = _RuntimeState()
    # is_set() calls: before step1 (False) -> KEY_DOWN runs -> after step1
    # (False, keep going) -> before step2 (True) -> interrupted, KEY_UP never runs.
    cancel_event = _ScriptedCancelEvent([False, False, True])

    completed = engine._execute_steps(macro, cancel_event, rt)

    assert completed is False
    input_inject.send_key.assert_called_once_with(0x41, key_up=False)
    assert rt.held_keys == {0x41}  # KEY_UP never ran -- still outstanding


def test_execute_steps_interrupted_between_mouse_down_and_up_leaves_it_tracked():
    engine = MacroEngine()
    steps = (
        _StepSnap(kind=MacroStepKind.MOUSE_DOWN, key_vk=None, mouse_button="Left", scroll_delta=120, delay_ms=0),
        _StepSnap(kind=MacroStepKind.MOUSE_UP, key_vk=None, mouse_button="Left", scroll_delta=120, delay_ms=0),
    )
    macro = _MacroSnap(
        id="m1", name="M", trigger_vk=0x51, mode=MacroMode.HOLD, enabled=True, humanize_jitter_pct=0, steps=steps
    )
    rt = _RuntimeState()
    cancel_event = _ScriptedCancelEvent([False, False, True])

    completed = engine._execute_steps(macro, cancel_event, rt)

    assert completed is False
    input_inject.send_mouse_button.assert_called_once_with(MouseButton.LEFT, up=False)
    assert rt.held_mouse == {"Left"}


def test_execute_steps_balanced_sequence_leaves_nothing_tracked():
    engine = MacroEngine()
    steps = (
        _StepSnap(kind=MacroStepKind.KEY_DOWN, key_vk=0x41, mouse_button="Left", scroll_delta=120, delay_ms=0),
        _StepSnap(kind=MacroStepKind.KEY_UP, key_vk=0x41, mouse_button="Left", scroll_delta=120, delay_ms=0),
    )
    macro = _MacroSnap(
        id="m1", name="M", trigger_vk=0x51, mode=MacroMode.HOLD, enabled=True, humanize_jitter_pct=0, steps=steps
    )
    rt = _RuntimeState()
    cancel_event = _ScriptedCancelEvent([False, False, False, False])

    completed = engine._execute_steps(macro, cancel_event, rt)

    assert completed is True
    assert rt.held_keys == set()


def test_key_tap_always_completes_even_when_cut_short():
    # KEY_TAP's own internal wait being interrupted must NOT skip its "up" --
    # it's self-contained, unlike a raw KEY_DOWN/KEY_UP pair.
    engine = MacroEngine()
    steps = (_StepSnap(kind=MacroStepKind.KEY_TAP, key_vk=0x41, mouse_button="Left", scroll_delta=120, delay_ms=0),)
    macro = _MacroSnap(
        id="m1", name="M", trigger_vk=0x51, mode=MacroMode.HOLD, enabled=True, humanize_jitter_pct=0, steps=steps
    )
    rt = _RuntimeState()
    cancel_event = _ScriptedCancelEvent([False, False])

    engine._execute_step(steps[0], 0, cancel_event, rt)

    calls = input_inject.send_key.call_args_list
    assert [c.kwargs["key_up"] for c in calls] == [False, True]
    assert rt.held_keys == set()  # never tracked -- always self-completes


# ---------------------------------------------------------------------------
# _force_release_held
# ---------------------------------------------------------------------------


def test_force_release_held_releases_tracked_key_and_mouse_button():
    engine = MacroEngine()
    rt = _RuntimeState()
    rt.held_keys = {0x41}
    rt.held_mouse = {"Left"}

    engine._force_release_held(rt)

    input_inject.send_key.assert_called_once_with(0x41, key_up=True)
    input_inject.send_mouse_button.assert_called_once_with(MouseButton.LEFT, up=True)
    assert rt.held_keys == set()
    assert rt.held_mouse == set()


def test_force_release_held_is_a_noop_when_nothing_tracked():
    engine = MacroEngine()
    rt = _RuntimeState()

    engine._force_release_held(rt)

    input_inject.send_key.assert_not_called()
    input_inject.send_mouse_button.assert_not_called()


# ---------------------------------------------------------------------------
# _hold_loop / _toggle_loop end-to-end -- real MacroDef, real loop method,
# only time.sleep monkeypatched to drive iteration count deterministically.
# ---------------------------------------------------------------------------


def test_hold_loop_force_releases_unmatched_key_down_when_trigger_released(monkeypatch):
    engine = MacroEngine()
    macro_def = MacroDef(
        id="m1",
        name="M",
        trigger=KeyBind(vk_code=0x51, name="Q"),
        mode=MacroMode.HOLD,
        enabled=True,
        steps=[_key_step("s1", MacroStepKind.KEY_DOWN, 0x41)],  # no matching KEY_UP at all
    )
    engine.update_snapshot(MacrosState(macros=[macro_def]))

    rt = engine._get_runtime("m1")
    rt.held = True

    calls = {"n": 0}

    def fake_sleep(_sec):
        calls["n"] += 1
        if calls["n"] == 1:
            rt.held = False  # trigger released between the first and second pass

    monkeypatch.setattr(macro_engine_module.time, "sleep", fake_sleep)

    engine._hold_loop("m1")

    assert [c.kwargs["key_up"] for c in input_inject.send_key.call_args_list] == [False, True]
    assert rt.held_keys == set()


def test_hold_loop_releases_nothing_extra_for_a_balanced_macro(monkeypatch):
    engine = MacroEngine()
    macro_def = MacroDef(
        id="m1",
        name="M",
        trigger=KeyBind(vk_code=0x51, name="Q"),
        mode=MacroMode.HOLD,
        enabled=True,
        steps=[
            _key_step("s1", MacroStepKind.KEY_DOWN, 0x41),
            _key_step("s2", MacroStepKind.KEY_UP, 0x41),
        ],
    )
    engine.update_snapshot(MacrosState(macros=[macro_def]))

    rt = engine._get_runtime("m1")
    rt.held = True

    calls = {"n": 0}

    def fake_sleep(_sec):
        calls["n"] += 1
        if calls["n"] == 1:
            rt.held = False

    monkeypatch.setattr(macro_engine_module.time, "sleep", fake_sleep)

    engine._hold_loop("m1")

    # One clean down/up pair from the one full pass, nothing extra from cleanup.
    assert [c.kwargs["key_up"] for c in input_inject.send_key.call_args_list] == [False, True]


def test_toggle_loop_force_releases_unmatched_mouse_down_when_toggled_off(monkeypatch):
    engine = MacroEngine()
    macro_def = MacroDef(
        id="m1",
        name="M",
        trigger=KeyBind(vk_code=0x51, name="Q"),
        mode=MacroMode.TOGGLE,
        enabled=True,
        steps=[MacroStep(id="s1", kind=MacroStepKind.MOUSE_DOWN, mouse_button="Left")],
    )
    engine.update_snapshot(MacrosState(macros=[macro_def]))

    rt = engine._get_runtime("m1")
    rt.toggle_running = True

    calls = {"n": 0}

    def fake_sleep(_sec):
        calls["n"] += 1
        if calls["n"] == 1:
            rt.toggle_running = False

    monkeypatch.setattr(macro_engine_module.time, "sleep", fake_sleep)

    engine._toggle_loop("m1")

    assert [c.kwargs["up"] for c in input_inject.send_mouse_button.call_args_list] == [False, True]
    assert rt.held_mouse == set()


def test_hold_loop_force_releases_when_macro_disabled_mid_hold(monkeypatch):
    engine = MacroEngine()
    macro_def = MacroDef(
        id="m1",
        name="M",
        trigger=KeyBind(vk_code=0x51, name="Q"),
        mode=MacroMode.HOLD,
        enabled=True,
        steps=[_key_step("s1", MacroStepKind.KEY_DOWN, 0x41)],
    )
    macros_state = MacrosState(macros=[macro_def])
    engine.update_snapshot(macros_state)

    rt = engine._get_runtime("m1")
    rt.held = True

    calls = {"n": 0}

    def fake_sleep(_sec):
        calls["n"] += 1
        if calls["n"] == 1:
            macro_def.enabled = False
            engine.update_snapshot(macros_state)

    monkeypatch.setattr(macro_engine_module.time, "sleep", fake_sleep)

    engine._hold_loop("m1")

    assert [c.kwargs["key_up"] for c in input_inject.send_key.call_args_list] == [False, True]
    assert rt.held_keys == set()


# ---------------------------------------------------------------------------
# stop() -- must release anything left tracked even though it clears
# self._runtime before the loop threads can run their own cleanup branch.
# ---------------------------------------------------------------------------


def test_stop_force_releases_key_left_tracked_by_a_runtime():
    engine = MacroEngine()
    engine.start()
    rt = _RuntimeState()
    rt.held_keys = {0x41}
    rt.held_mouse = {"Right"}
    rt.thread = None  # no real thread to join for this test
    engine._runtime["m1"] = rt

    engine.stop()

    input_inject.send_key.assert_called_once_with(0x41, key_up=True)
    input_inject.send_mouse_button.assert_called_once_with(MouseButton.RIGHT, up=True)


def test_stop_releases_unmatched_key_down_from_a_genuinely_running_hold_thread():
    """Same guarantee as the test above, but through the real public path --
    a real trigger-down via handle_effective_event(), a real background
    thread spawned by _ensure_thread()/_hold_loop() looping on real
    threading.Event/time.sleep (only `_LOOP_YIELD_SEC`, never mocked) --
    rather than a hand-built `_RuntimeState` with `thread = None`. Covers the
    actual join()-then-release ordering in stop() under real thread
    scheduling, not just the logic in isolation."""
    engine = MacroEngine()
    engine.start()
    macro_def = MacroDef(
        id="m1",
        name="M",
        trigger=KeyBind(vk_code=0x51, name="Q"),
        mode=MacroMode.HOLD,
        enabled=True,
        steps=[_key_step("s1", MacroStepKind.KEY_DOWN, 0x41)],  # no matching KEY_UP at all
    )
    engine.update_snapshot(MacrosState(macros=[macro_def]))

    engine.handle_effective_event(EffectiveInputEvent(vk_code=0x51, up=False))

    # Give the real background thread a moment to actually start looping and
    # send at least one real KEY_DOWN before stop() races it.
    deadline = time.monotonic() + 2.0
    while not input_inject.send_key.called and time.monotonic() < deadline:
        time.sleep(0.005)
    assert input_inject.send_key.called  # sanity: the loop actually ran before we stop it

    engine.stop()

    calls = [c.kwargs["key_up"] for c in input_inject.send_key.call_args_list]
    assert calls[-1] is True  # the last thing sent for 0x41 is always its release
    assert calls.count(True) == 1  # exactly one release -- no double-release race


# ---------------------------------------------------------------------------
# handle_gate_closed() -- registered as remapper.py's add_gate_close_listener
# callback. A live Hold/Toggle session has no other way to learn its trigger
# was released while the targeted process was unfocused, since remapper.py
# stops publishing events entirely while its gate is closed. Found live
# 2026-09-12: without this, a Hold macro released mid-focus-loss kept
# re-firing into whatever now had focus, indefinitely.
# ---------------------------------------------------------------------------


def test_handle_gate_closed_stops_a_running_hold_session():
    engine = MacroEngine()
    engine.start()
    macro_def = MacroDef(
        id="m1",
        name="M",
        trigger=KeyBind(vk_code=0x51, name="Q"),
        mode=MacroMode.HOLD,
        enabled=True,
        steps=[_key_step("s1", MacroStepKind.KEY_DOWN, 0x41)],  # no matching KEY_UP at all
    )
    engine.update_snapshot(MacrosState(macros=[macro_def]))
    engine.handle_effective_event(EffectiveInputEvent(vk_code=0x51, up=False))

    deadline = time.monotonic() + 2.0
    while not input_inject.send_key.called and time.monotonic() < deadline:
        time.sleep(0.005)
    assert input_inject.send_key.called  # sanity: the loop actually started

    rt = engine._get_existing_runtime("m1")
    assert rt is not None and rt.held is True

    # Simulate the trigger key's release having been swallowed by remapper.py
    # because the gate was closed at the time -- handle_effective_event()
    # never gets called for it. handle_gate_closed() is the only thing that
    # can still stop this session.
    engine.handle_gate_closed()

    deadline = time.monotonic() + 2.0
    while rt.held and time.monotonic() < deadline:
        time.sleep(0.005)
    assert rt.held is False

    # Let the loop thread's own in-loop cleanup actually run and release the
    # key it left down.
    deadline = time.monotonic() + 2.0
    while 0x41 in rt.held_keys and time.monotonic() < deadline:
        time.sleep(0.005)

    calls = [c.kwargs["key_up"] for c in input_inject.send_key.call_args_list]
    assert True in calls  # the unmatched KEY_DOWN was eventually released
    engine.stop()


def test_handle_gate_closed_stops_a_running_toggle_session():
    engine = MacroEngine()
    engine.start()
    macro_def = MacroDef(
        id="m1",
        name="M",
        trigger=KeyBind(vk_code=0x51, name="Q"),
        mode=MacroMode.TOGGLE,
        enabled=True,
        steps=[MacroStep(id="s1", kind=MacroStepKind.MOUSE_DOWN, mouse_button="Left")],
    )
    engine.update_snapshot(MacrosState(macros=[macro_def]))
    # Toggle arms on release, per _on_trigger's TOGGLE branch.
    engine.handle_effective_event(EffectiveInputEvent(vk_code=0x51, up=True))

    deadline = time.monotonic() + 2.0
    while not input_inject.send_mouse_button.called and time.monotonic() < deadline:
        time.sleep(0.005)
    assert input_inject.send_mouse_button.called

    rt = engine._get_existing_runtime("m1")
    assert rt is not None and rt.toggle_running is True

    engine.handle_gate_closed()

    deadline = time.monotonic() + 2.0
    while rt.toggle_running and time.monotonic() < deadline:
        time.sleep(0.005)
    assert rt.toggle_running is False

    engine.stop()


def test_handle_gate_closed_is_a_noop_with_no_active_sessions():
    engine = MacroEngine()
    engine.start()
    # No trigger ever fired -- must not raise with an empty _runtime dict.
    engine.handle_gate_closed()
    engine.stop()
    assert engine._runtime == {}  # stop() always clears runtimes, real thread or not
