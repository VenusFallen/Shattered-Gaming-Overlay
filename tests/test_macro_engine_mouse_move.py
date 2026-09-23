"""Coverage for macro_engine.py's Move Mouse To/Move Mouse By step kinds.
input_inject's send_mouse_move_absolute/send_mouse_move_by_step/send_mouse_move_relative are
monkeypatched so nothing here calls real SendInput."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import input_inject
from app_state import MacroDef, MacroMode, MacroStep, MacroStepKind, MacrosState, SettingsState
from key_capture import UNBOUND
from macro_engine import MacroEngine, _RuntimeState, _StepSnap


@pytest.fixture(autouse=True)
def _stub_injection(monkeypatch):
    monkeypatch.setattr(input_inject, "send_mouse_move_absolute", Mock())
    monkeypatch.setattr(input_inject, "send_mouse_move_by_step", Mock())
    monkeypatch.setattr(input_inject, "send_mouse_move_relative", Mock())
    monkeypatch.setattr(input_inject, "get_cursor_pos", Mock(return_value=(0, 0)))
    yield


class _InstantEvent:
    """Same interface as threading.Event, but wait() never actually sleeps -- real substep gestures can now
    run to hundreds of steps, and Windows' ~15ms default timer granularity makes real per-step waits
    balloon test runtime for no benefit; these tests only care about the resulting call sequence/math."""

    def __init__(self) -> None:
        self._flag = False

    def set(self) -> None:
        self._flag = True

    def clear(self) -> None:
        self._flag = False

    def is_set(self) -> bool:
        return self._flag

    def wait(self, timeout=None) -> bool:
        return self._flag


def _move_to_step(step_id: str, x: int, y: int) -> MacroStep:
    return MacroStep(id=step_id, kind=MacroStepKind.MOUSE_MOVE_TO, move_x=x, move_y=y)


def _move_by_step(step_id: str, dx: int, dy: int) -> MacroStep:
    return MacroStep(id=step_id, kind=MacroStepKind.MOUSE_MOVE_BY, move_x=dx, move_y=dy)


def _move_to_step_snap(x: int, y: int, speed_pct: int = 50) -> _StepSnap:
    return _StepSnap(id="s1", kind=MacroStepKind.MOUSE_MOVE_TO, key_vk=None, mouse_button="Left", scroll_delta=120, delay_ms=0, move_x=x, move_y=y, path_wobble_pct=0, move_speed_pct=speed_pct)


def _move_by_step_snap(dx: int, dy: int, wobble_pct: int = 0) -> _StepSnap:
    return _StepSnap(id="s1", kind=MacroStepKind.MOUSE_MOVE_BY, key_vk=None, mouse_button="Left", scroll_delta=120, delay_ms=0, move_x=dx, move_y=dy, path_wobble_pct=wobble_pct, move_speed_pct=50)


# ---------------------------------------------------------------------------
# Execution -- Move To always glides, Move By fires the right input_inject call
# ---------------------------------------------------------------------------


def test_move_to_step_glides_to_the_target():
    engine = MacroEngine()
    step = _move_to_step_snap(800, 450)

    engine._execute_step(step, 0, _InstantEvent(), _RuntimeState())

    input_inject.send_mouse_move_absolute.assert_not_called()
    total_dx = sum(c.args[0] for c in input_inject.send_mouse_move_relative.call_args_list)
    total_dy = sum(c.args[1] for c in input_inject.send_mouse_move_relative.call_args_list)
    assert (total_dx, total_dy) == (800, 450)  # cursor starts at (0, 0) per the stub


def test_move_by_step_sums_to_the_configured_delta():
    # move_x/move_y pass through unflipped -- the sign convention is send_mouse_move_by_step's own job.
    # Always stepped/timed at the polling rate now, so this may take more than one call -- check the sum, not a single call.
    engine = MacroEngine()
    step = _move_by_step_snap(-40, 200)

    engine._execute_step(step, 0, _InstantEvent(), _RuntimeState())

    total_dx = sum(c.args[0] for c in input_inject.send_mouse_move_by_step.call_args_list)
    total_dy = sum(c.args[1] for c in input_inject.send_mouse_move_by_step.call_args_list)
    assert (total_dx, total_dy) == (-40, 200)


# ---------------------------------------------------------------------------
# Humanize jitter
# ---------------------------------------------------------------------------


def test_humanized_move_to_is_unchanged_at_zero_jitter():
    assert MacroEngine._humanized_move_to(500, 300, 0) == (500, 300)


def test_humanized_move_to_stays_within_the_fixed_pixel_ceiling():
    x, y = 500, 300
    for _ in range(200):
        jx, jy = MacroEngine._humanized_move_to(x, y, 100)
        assert abs(jx - x) <= MacroEngine._MOVE_TARGET_JITTER_MAX_PX
        assert abs(jy - y) <= MacroEngine._MOVE_TARGET_JITTER_MAX_PX


def test_humanized_move_by_is_unchanged_at_zero_jitter():
    assert MacroEngine._humanized_move_by(100, -50, 0) == (100, -50)


def test_humanized_move_by_scales_with_percentage_not_a_fixed_pixel_ceiling():
    # Unlike Move To, Move By's jitter is a percentage of the configured value, not a fixed pixel ceiling.
    dx = 1000
    deltas = [abs(MacroEngine._humanized_move_by(dx, 0, 50)[0] - dx) for _ in range(200)]
    assert max(deltas) > MacroEngine._MOVE_TARGET_JITTER_MAX_PX


# ---------------------------------------------------------------------------
# Move To glide -- always on, speed is the only knob
# ---------------------------------------------------------------------------


def test_glide_offsets_sum_exactly_to_the_target_delta():
    offsets = MacroEngine._move_to_glide_offsets(100, 200, 900, 50, 1000, 50)
    total_dx = sum(dx for dx, dy in offsets)
    total_dy = sum(dy for dx, dy in offsets)
    assert (total_dx, total_dy) == (800, -150)


def test_glide_offsets_empty_when_already_at_the_target():
    assert MacroEngine._move_to_glide_offsets(500, 500, 500, 500, 1000, 50) == []


def test_glide_offsets_ease_in_and_out():
    # Cosine ease-in-out: step size should be smallest at the start/end and largest near the middle.
    offsets = MacroEngine._move_to_glide_offsets(0, 0, 1000, 0, 1000, 50)
    mid = offsets[len(offsets) // 2][0]
    assert offsets[0][0] < mid
    assert offsets[-1][0] < mid


def test_glide_offsets_substep_count_scales_with_polling_rate():
    slow = MacroEngine._move_to_glide_offsets(0, 0, 1000, 0, 125, 50)
    fast = MacroEngine._move_to_glide_offsets(0, 0, 1000, 0, 1000, 50)
    assert len(fast) > len(slow)


def test_glide_offsets_higher_speed_pct_completes_in_fewer_substeps():
    # More px/sec -> shorter duration -> fewer substeps at the same polling rate.
    slow = MacroEngine._move_to_glide_offsets(0, 0, 2000, 0, 1000, 0)
    fast = MacroEngine._move_to_glide_offsets(0, 0, 2000, 0, 1000, 100)
    assert len(fast) < len(slow)


def test_execute_step_move_to_sends_relative_moves_summing_to_target(monkeypatch):
    monkeypatch.setattr(input_inject, "get_cursor_pos", Mock(return_value=(100, 100)))
    engine = MacroEngine()
    step = _move_to_step_snap(600, 100)

    engine._execute_step(step, 0, _InstantEvent(), _RuntimeState())

    assert input_inject.send_mouse_move_relative.call_count > 1
    input_inject.send_mouse_move_absolute.assert_not_called()
    total_dx = sum(c.args[0] for c in input_inject.send_mouse_move_relative.call_args_list)
    total_dy = sum(c.args[1] for c in input_inject.send_mouse_move_relative.call_args_list)
    assert (total_dx, total_dy) == (500, 0)


def test_execute_step_move_to_is_interruptible_mid_glide(monkeypatch):
    monkeypatch.setattr(input_inject, "get_cursor_pos", Mock(return_value=(0, 0)))
    engine = MacroEngine()
    step = _move_to_step_snap(1000, 0)
    cancel_event = _InstantEvent()
    call_count = {"n": 0}

    def _cancel_after_first_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            cancel_event.set()

    input_inject.send_mouse_move_relative.side_effect = _cancel_after_first_call

    engine._execute_step(step, 0, cancel_event, _RuntimeState())

    assert 0 < input_inject.send_mouse_move_relative.call_count < len(
        MacroEngine._move_to_glide_offsets(0, 0, 1000, 0, engine._mouse_polling_rate_hz, step.move_speed_pct)
    )


def test_execute_step_move_to_falls_back_to_teleport_if_cursor_pos_fails(monkeypatch):
    monkeypatch.setattr(input_inject, "get_cursor_pos", Mock(side_effect=OSError))
    engine = MacroEngine()
    step = _move_to_step_snap(800, 450)

    engine._execute_step(step, 0, _InstantEvent(), _RuntimeState())

    input_inject.send_mouse_move_absolute.assert_called_once_with(800, 450)
    input_inject.send_mouse_move_relative.assert_not_called()


def test_update_snapshot_carries_the_configured_polling_rate_into_the_engine():
    engine = MacroEngine()
    settings = SettingsState()
    settings.mouse_polling_rate_hz = 500

    engine.update_snapshot(MacrosState(), settings)

    assert engine._mouse_polling_rate_hz == 500


@pytest.mark.parametrize("bad_rate", [0, -1, -1000])
def test_execute_step_move_to_survives_a_zero_or_negative_polling_rate(bad_rate):
    # A hand-edited settings.json is the only realistic way this reaches the engine --
    # must degrade gracefully (one big step), never raise ZeroDivisionError.
    engine = MacroEngine()
    engine._mouse_polling_rate_hz = bad_rate
    step = _move_to_step_snap(300, 0)

    engine._execute_step(step, 0, _InstantEvent(), _RuntimeState())

    total_dx = sum(c.args[0] for c in input_inject.send_mouse_move_relative.call_args_list)
    total_dy = sum(c.args[1] for c in input_inject.send_mouse_move_relative.call_args_list)
    assert (total_dx, total_dy) == (300, 0)


@pytest.mark.parametrize("bad_rate", [0, -1, -1000])
def test_execute_step_move_by_survives_a_zero_or_negative_polling_rate(bad_rate):
    engine = MacroEngine()
    engine._mouse_polling_rate_hz = bad_rate
    step = _move_by_step_snap(-40, 200)

    engine._execute_step(step, 0, _InstantEvent(), _RuntimeState())

    total_dx = sum(c.args[0] for c in input_inject.send_mouse_move_by_step.call_args_list)
    total_dy = sum(c.args[1] for c in input_inject.send_mouse_move_by_step.call_args_list)
    assert (total_dx, total_dy) == (-40, 200)


@pytest.mark.parametrize("bad_speed_pct", [-50, 200])
def test_glide_duration_clamps_out_of_range_speed_pct(bad_speed_pct):
    # A corrupted profile could carry an out-of-range move_speed_pct -- must clamp, never raise or invert.
    clamped_low = MacroEngine._glide_duration_sec(1000.0, 0)
    clamped_high = MacroEngine._glide_duration_sec(1000.0, 100)
    duration = MacroEngine._glide_duration_sec(1000.0, bad_speed_pct)
    assert clamped_high <= duration <= clamped_low


# ---------------------------------------------------------------------------
# Move By in Hold/Toggle -- deliberately unrestricted
# ---------------------------------------------------------------------------


def _macros_state_with(mode: MacroMode) -> MacrosState:
    macro = MacroDef(id="m1", name="M", trigger=UNBOUND, mode=mode, enabled=True, humanize_jitter_pct=0)
    macro.steps = [_move_by_step("s1", 300, -300)]
    state = MacrosState()
    state.macros = [macro]
    return state


@pytest.mark.parametrize("mode", [MacroMode.ONCE, MacroMode.HOLD, MacroMode.TOGGLE])
def test_move_by_step_survives_the_snapshot_in_every_mode(mode):
    engine = MacroEngine()

    engine.update_snapshot(_macros_state_with(mode), SettingsState())

    snap = engine._find_macro("m1")
    assert len(snap.steps) == 1
    assert snap.steps[0].kind == MacroStepKind.MOUSE_MOVE_BY


def test_end_to_end_hold_loop_does_fire_send_mouse_move_by_step():
    """Runs the real _hold_loop synchronously and confirms it fires the full stepped gesture once per iteration while held."""
    engine = MacroEngine()
    engine.update_snapshot(_macros_state_with(MacroMode.HOLD), SettingsState())
    rt = engine._get_runtime("m1")
    rt.held = True
    rt.cancel_event = _InstantEvent()  # avoid real per-substep waits, see _InstantEvent's own docstring

    # Let the loop run exactly two iterations then stop itself.
    call_count = {"n": 0}
    real_get_existing_runtime = engine._get_existing_runtime

    def _get_existing_runtime_twice(macro_id):
        call_count["n"] += 1
        if call_count["n"] > 2:
            rt.held = False
        return real_get_existing_runtime(macro_id)

    engine._get_existing_runtime = _get_existing_runtime_twice
    engine._hold_loop("m1")

    per_firing = len(MacroEngine._move_by_wave_offsets(300, -300, 0, engine._mouse_polling_rate_hz))
    assert input_inject.send_mouse_move_by_step.call_count == per_firing * 2


# ---------------------------------------------------------------------------
# Path wobble -- independent of Humanize jitter, two separate knobs
# ---------------------------------------------------------------------------


def test_wave_offsets_sum_exactly_to_the_configured_distance_at_zero_wobble():
    offsets = MacroEngine._move_by_wave_offsets(300, -150, 0)

    total_dx = sum(dx for dx, dy in offsets)
    total_dy = sum(dy for dx, dy in offsets)
    assert (total_dx, total_dy) == (300, -150)


@pytest.mark.parametrize("wobble_pct", [1, 25, 50, 100])
def test_wave_offsets_always_sum_exactly_to_the_configured_distance(wobble_pct):
    for _ in range(20):  # repeat -- direction/cycle count are randomized per call
        offsets = MacroEngine._move_by_wave_offsets(400, 250, wobble_pct)
        total_dx = sum(dx for dx, dy in offsets)
        total_dy = sum(dy for dx, dy in offsets)
        assert (total_dx, total_dy) == (400, 250)


def test_wave_offsets_empty_for_a_zero_length_move():
    assert MacroEngine._move_by_wave_offsets(0, 0, 50) == []


def test_wave_offsets_produce_visible_perpendicular_deviation_at_high_wobble():
    # dy=0 move's perpendicular axis is pure vertical -- at least one substep's y should be nonzero.
    found_wobble = False
    for _ in range(20):
        offsets = MacroEngine._move_by_wave_offsets(1000, 0, 100)
        cumulative_y = 0
        for _dx, dy in offsets:
            cumulative_y += dy
            if cumulative_y != 0:
                found_wobble = True
                break
        if found_wobble:
            break
    assert found_wobble


def test_wave_offsets_negligible_perpendicular_deviation_at_low_wobble():
    offsets = MacroEngine._move_by_wave_offsets(1000, 0, 1)
    cumulative_y = 0
    max_deviation = 0
    for _dx, dy in offsets:
        cumulative_y += dy
        max_deviation = max(max_deviation, abs(cumulative_y))
    assert max_deviation <= 5  # 1% of the 25%-max amplitude fraction is tiny


def test_wave_offsets_work_for_a_diagonal_move():
    for _ in range(20):
        offsets = MacroEngine._move_by_wave_offsets(500, 500, 75)
        total_dx = sum(dx for dx, dy in offsets)
        total_dy = sum(dy for dx, dy in offsets)
        assert (total_dx, total_dy) == (500, 500)


def test_execute_step_fires_multiple_calls_when_wobble_is_set():
    engine = MacroEngine()
    step = _move_by_step_snap(300, 0, wobble_pct=100)

    engine._execute_step(step, 0, _InstantEvent(), _RuntimeState())

    assert input_inject.send_mouse_move_by_step.call_count > 1
    total_dx = sum(c.args[0] for c in input_inject.send_mouse_move_by_step.call_args_list)
    total_dy = sum(c.args[1] for c in input_inject.send_mouse_move_by_step.call_args_list)
    assert (total_dx, total_dy) == (300, 0)


def test_execute_step_with_wobble_respects_strength_jitter_too(monkeypatch):
    # jitter still varies total distance with wobble active too -- run several trials since a single draw could coincide with 1000.
    step = _move_by_step_snap(1000, 0, wobble_pct=50)
    totals = []
    for _ in range(5):
        input_inject.send_mouse_move_by_step.reset_mock()
        engine = MacroEngine()
        engine._execute_step(step, 50, _InstantEvent(), _RuntimeState())
        totals.append(sum(c.args[0] for c in input_inject.send_mouse_move_by_step.call_args_list))

    assert any(total != 1000 for total in totals)


def test_execute_step_with_wobble_is_interruptible_mid_gesture():
    engine = MacroEngine()
    step = _move_by_step_snap(1000, 0, wobble_pct=100)
    cancel_event = _InstantEvent()
    call_count = {"n": 0}

    def _cancel_after_first_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            cancel_event.set()

    input_inject.send_mouse_move_by_step.side_effect = _cancel_after_first_call

    engine._execute_step(step, 0, cancel_event, _RuntimeState())

    full_length = len(MacroEngine._move_by_wave_offsets(1000, 0, 100, engine._mouse_polling_rate_hz))
    assert 0 < input_inject.send_mouse_move_by_step.call_count < full_length
