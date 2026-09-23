"""Unit coverage for RemapperEngine's two Auto Toggle/Hold state machines (AutoToggleHoldEntry,
`RemapperState.auto_entries`) and every stuck-key cleanup path that can leave one active: entry
edit/disable/mode-switch/remove, profile switch, window-filter focus loss, and engine stop()/exit. Also
covers the standard RemapEntry regression where a remap held through a gate close or stop() stayed stuck.
Exercises RemapperEngine._handle()/update_snapshot() directly rather than a live hook -- a fresh
RemapperEngine() never calls start(). input_inject.send_key/send_mouse_button and
window_select.cached_foreground_pid are monkeypatched; nothing here touches real disk or hooks.
"""

from __future__ import annotations

from typing import Callable, List, Optional
from unittest.mock import Mock

import pytest

import input_inject
import remapper
import window_select
from app_state import AutoToggleHoldEntry, ProcessInfo, RemapEntry, RemapMode, RemapperState, WindowSelectState
from key_capture import KeyBind

SOURCE = KeyBind(vk_code=0x54, name="T")
SOURCE2 = KeyBind(vk_code=0x59, name="Y")
DEST = KeyBind(vk_code=0x43, name="C")
DEST2 = KeyBind(vk_code=0x56, name="V")
KEY = KeyBind(vk_code=0x47, name="G")
KEY2 = KeyBind(vk_code=0x48, name="H")


class FakeTimer:
    """Stand-in for threading.Timer: never actually sleeps. `start()`/
    `cancel()` just flip flags for assertions; a test fires the deferred
    callback itself by calling `.fn()` once it wants to simulate
    `_AUTO_HOLD_TAP_GAP_MS` having elapsed."""

    def __init__(self, interval: float, fn: Callable[[], None]) -> None:
        self.interval = interval
        self.fn = fn
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


@pytest.fixture
def engine() -> remapper.RemapperEngine:
    e = remapper.RemapperEngine()
    e._timer_factory = FakeTimer
    return e


@pytest.fixture(autouse=True)
def _stub_injection(monkeypatch):
    monkeypatch.setattr(input_inject, "send_key", Mock())
    monkeypatch.setattr(input_inject, "send_mouse_button", Mock())
    # No process targeted by default -- gate always open unless a test opts in.
    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 0)
    yield


def _toggle_entry(entry_id: str = "auto-1", key: KeyBind = KEY, enabled: bool = True) -> AutoToggleHoldEntry:
    return AutoToggleHoldEntry(id=entry_id, key=key, mode=RemapMode.TOGGLE, enabled=enabled)


def _hold_auto_entry(entry_id: str = "auto-1", key: KeyBind = KEY, enabled: bool = True) -> AutoToggleHoldEntry:
    return AutoToggleHoldEntry(id=entry_id, key=key, mode=RemapMode.HOLD, enabled=enabled)


def _sync(
    engine: remapper.RemapperEngine,
    entries: Optional[List[RemapEntry]] = None,
    auto_entries: Optional[List[AutoToggleHoldEntry]] = None,
    selected: Optional[ProcessInfo] = None,
) -> None:
    engine.update_snapshot(
        RemapperState(entries=entries or [], auto_entries=auto_entries or []),
        WindowSelectState(selected=selected),
    )


def _press(engine: remapper.RemapperEngine, vk: int) -> Optional[bool]:
    return engine._handle(vk, up=False, name="", time_ms=0)


def _release(engine: remapper.RemapperEngine, vk: int) -> Optional[bool]:
    return engine._handle(vk, up=True, name="", time_ms=0)


# ---------------------------------------------------------------------------
# Data model -- backward compat
# ---------------------------------------------------------------------------


def test_auto_entry_defaults_to_toggle_when_unspecified():
    entry = AutoToggleHoldEntry(id="a1", key=KEY)
    assert entry.mode == RemapMode.TOGGLE


def test_profiles_json_missing_mode_defaults_to_toggle():
    import profiles

    raw = {
        "id": "a1",
        "key": {"vk_code": KEY.vk_code, "name": KEY.name},
        "enabled": True,
        # no "mode" key -- profile saved before this section existed
    }
    entry = profiles._auto_entry_from_json(raw)
    assert entry.mode == RemapMode.TOGGLE


def test_profiles_json_round_trips_hold_mode():
    import profiles

    entry = _hold_auto_entry()
    raw = profiles._auto_entry_to_json(entry)
    assert raw["mode"] == "Hold"
    restored = profiles._auto_entry_from_json(raw)
    assert restored.mode == RemapMode.HOLD


def test_old_standard_entry_mode_field_is_dropped_not_migrated():
    # A profile saved while RemapEntry still had a mode field (briefly
    # shipped Toggle-on-a-remap) must still load as a plain 1:1 remap --
    # the mode key is ignored, not auto-migrated into a new Auto entry.
    import profiles

    raw = {
        "id": "r1",
        "source": {"vk_code": SOURCE.vk_code, "name": SOURCE.name},
        "destination": {"vk_code": DEST.vk_code, "name": DEST.name},
        "enabled": True,
        "mode": "Toggle",
    }
    entry = profiles._remap_entry_from_json(raw)
    assert not hasattr(entry, "mode")
    assert entry.source == SOURCE
    assert entry.destination == DEST


# ---------------------------------------------------------------------------
# Auto Toggle -- happy path (relocated from test_remapper_toggle.py)
# ---------------------------------------------------------------------------


def test_toggle_first_press_latches_key_down(engine):
    _sync(engine, auto_entries=[_toggle_entry()])
    suppressed = _press(engine, KEY.vk_code)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)


def test_toggle_second_press_releases_key(engine):
    # A real second press is always preceded by a real physical release --
    # can't press the same key twice without letting go first.
    _sync(engine, auto_entries=[_toggle_entry()])
    _press(engine, KEY.vk_code)
    _release(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    suppressed = _press(engine, KEY.vk_code)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_toggle_third_press_latches_down_again(engine):
    _sync(engine, auto_entries=[_toggle_entry()])
    _press(engine, KEY.vk_code)
    _release(engine, KEY.vk_code)
    _press(engine, KEY.vk_code)
    _release(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    _press(engine, KEY.vk_code)
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)


def test_toggle_release_is_a_noop(engine):
    _sync(engine, auto_entries=[_toggle_entry()])
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    suppressed = _release(engine, KEY.vk_code)
    assert suppressed is True  # still suppressed -- physical release never leaks through
    input_inject.send_key.assert_not_called()


def test_toggle_release_before_any_press_is_a_noop(engine):
    _sync(engine, auto_entries=[_toggle_entry()])
    suppressed = _release(engine, KEY.vk_code)
    assert suppressed is True
    input_inject.send_key.assert_not_called()


def test_standard_remap_still_mirrors_source_1to1(engine):
    # Regression: standard RemapEntry has no mode concept anymore, must
    # always mirror 1:1.
    entry = RemapEntry(id="r1", source=SOURCE, destination=DEST)
    _sync(engine, entries=[entry])

    _press(engine, SOURCE.vk_code)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=False)
    input_inject.send_key.reset_mock()

    _release(engine, SOURCE.vk_code)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


# ---------------------------------------------------------------------------
# Auto Toggle cleanup -- entry edited/disabled/removed while toggled on
# ---------------------------------------------------------------------------


def test_cleanup_forces_release_when_toggle_entry_disabled(engine):
    entry = _toggle_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    entry.enabled = False
    _sync(engine, auto_entries=[entry])

    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_cleanup_forces_release_when_toggle_entry_removed(engine):
    entry = _toggle_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    _sync(engine, auto_entries=[])

    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_cleanup_forces_release_when_mode_switched_off_toggle(engine):
    entry = _toggle_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    entry.mode = RemapMode.HOLD
    _sync(engine, auto_entries=[entry])

    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_cleanup_forces_release_when_key_changed(engine):
    entry = _toggle_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    entry.key = KEY2
    _sync(engine, auto_entries=[entry])

    # Released using the OLD key -- that's what's actually latched down, not
    # whatever the entry now points to.
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_cleanup_forces_release_on_profile_switch(engine):
    # apply_profile() replaces AppState.remapper.auto_entries wholesale with
    # a different profile's entries (different ids) -- update_snapshot()
    # can't tell that apart from a delete, which is exactly what should
    # happen: nothing from the old profile should stay latched into the new one.
    entry = _toggle_entry(entry_id="profileA-auto-1")
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    other_entry = _toggle_entry(entry_id="profileB-auto-1", key=KEY2)
    _sync(engine, auto_entries=[other_entry])

    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


# ---------------------------------------------------------------------------
# Auto Hold -- happy path (new)
# ---------------------------------------------------------------------------


def test_hold_press_taps_key_down_then_up_after_gap(engine):
    _sync(engine, auto_entries=[_hold_auto_entry()])

    suppressed = _press(engine, KEY.vk_code)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)
    input_inject.send_key.reset_mock()

    timer = engine._hold_pending["auto-1"].timer
    assert timer.interval == pytest.approx(remapper._AUTO_HOLD_TAP_GAP_MS / 1000.0)
    assert timer.started is True
    input_inject.send_key.assert_not_called()  # "up" not sent yet -- gap hasn't elapsed

    timer.fn()  # simulate the gap elapsing
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)
    assert "auto-1" not in engine._hold_pending


def test_hold_release_taps_key_down_then_up_after_gap_independently(engine):
    _sync(engine, auto_entries=[_hold_auto_entry()])
    _press(engine, KEY.vk_code)
    engine._hold_pending["auto-1"].timer.fn()  # let the press's tap finish
    input_inject.send_key.reset_mock()

    suppressed = _release(engine, KEY.vk_code)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)
    input_inject.send_key.reset_mock()

    timer = engine._hold_pending["auto-1"].timer
    timer.fn()
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_hold_full_gesture_produces_exactly_two_down_up_pairs(engine):
    _sync(engine, auto_entries=[_hold_auto_entry()])

    _press(engine, KEY.vk_code)
    engine._hold_pending["auto-1"].timer.fn()
    _release(engine, KEY.vk_code)
    engine._hold_pending["auto-1"].timer.fn()

    calls = input_inject.send_key.call_args_list
    assert [c.kwargs["key_up"] for c in calls] == [False, True, False, True]
    assert all(c.args[0] == KEY.vk_code for c in calls)


def test_hold_second_edge_before_first_tap_finishes_force_completes_it(engine):
    # A press followed immediately by a release, faster than the tap gap,
    # must not leave an orphaned timer or two overlapping "down" states --
    # the superseded tap is force-completed (released) before the new one starts.
    _sync(engine, auto_entries=[_hold_auto_entry()])

    _press(engine, KEY.vk_code)
    first_timer = engine._hold_pending["auto-1"].timer
    input_inject.send_key.reset_mock()

    _release(engine, KEY.vk_code)  # supersedes the still-pending press tap
    assert first_timer.cancelled is True

    calls = input_inject.send_key.call_args_list
    # Old tap force-released (up), then the new tap's down, in that order.
    assert [c.kwargs["key_up"] for c in calls] == [True, False]

    # The superseded timer firing late (race: cancel() didn't stop it in
    # time) must be a no-op -- it's no longer the registered pending tap.
    input_inject.send_key.reset_mock()
    first_timer.fn()
    input_inject.send_key.assert_not_called()


# ---------------------------------------------------------------------------
# OS key-repeat suppression -- a held physical key makes Windows resend "down" transitions through
# WH_KEYBOARD_LL, indistinguishable from a fresh press; simulated here via multiple `_press()` calls with no `_release()` between them.
# ---------------------------------------------------------------------------


def test_hold_ignores_os_repeat_while_key_stays_physically_down(engine):
    _sync(engine, auto_entries=[_hold_auto_entry()])

    _press(engine, KEY.vk_code)  # genuine press -- starts the one tap
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)
    input_inject.send_key.reset_mock()

    # Windows re-firing "down" while the key is still held, several times --
    # must NOT start a new tap or touch the pending timer each time.
    for _ in range(5):
        suppressed = _press(engine, KEY.vk_code)
        assert suppressed is True
    input_inject.send_key.assert_not_called()
    assert engine._hold_pending["auto-1"].timer.cancelled is False

    # The one real tap still completes normally.
    engine._hold_pending["auto-1"].timer.fn()
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_hold_repeat_after_tap_finishes_still_does_not_refire(engine):
    # Repeat ticks can keep arriving after the press's tap has already
    # completed (100ms gap is short) -- still must not start a fresh tap
    # until the real physical release/press cycle happens.
    _sync(engine, auto_entries=[_hold_auto_entry()])

    _press(engine, KEY.vk_code)
    engine._hold_pending["auto-1"].timer.fn()  # tap completes while still held
    input_inject.send_key.reset_mock()

    _press(engine, KEY.vk_code)  # another repeat tick
    input_inject.send_key.assert_not_called()
    assert "auto-1" not in engine._hold_pending

    # Physical release still fires its own independent tap correctly.
    _release(engine, KEY.vk_code)
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)


def test_hold_release_then_fresh_press_is_not_treated_as_a_repeat(engine):
    _sync(engine, auto_entries=[_hold_auto_entry()])

    _press(engine, KEY.vk_code)
    engine._hold_pending["auto-1"].timer.fn()
    _release(engine, KEY.vk_code)
    engine._hold_pending["auto-1"].timer.fn()
    input_inject.send_key.reset_mock()

    # A genuinely new press after a real release must start a new tap.
    suppressed = _press(engine, KEY.vk_code)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)


def test_toggle_ignores_os_repeat_while_key_stays_physically_down(engine):
    _sync(engine, auto_entries=[_toggle_entry()])

    _press(engine, KEY.vk_code)  # genuine press -- latches on
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)
    input_inject.send_key.reset_mock()

    # Repeats while still held must not flip the latch back off.
    for _ in range(5):
        suppressed = _press(engine, KEY.vk_code)
        assert suppressed is True
    input_inject.send_key.assert_not_called()
    assert engine._toggle_on["auto-1"] == KEY

    # Physical release (no-op for Toggle either way), then a genuine second
    # press releases the latch exactly once.
    _release(engine, KEY.vk_code)
    suppressed = _press(engine, KEY.vk_code)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_standard_remap_unaffected_by_repeat_tracking(engine):
    # Repeat suppression is scoped to Auto Toggle/Hold only -- a standard
    # remap must keep mirroring every physical down it receives, repeats
    # included (redundant downs onto an already-down destination are
    # harmless and expected fidelity, not a bug).
    entry = RemapEntry(id="r1", source=SOURCE, destination=DEST)
    _sync(engine, entries=[entry])

    _press(engine, SOURCE.vk_code)
    _press(engine, SOURCE.vk_code)  # simulated repeat
    calls = input_inject.send_key.call_args_list
    assert len(calls) == 2
    assert all(c.args[0] == DEST.vk_code and c.kwargs["key_up"] is False for c in calls)


# ---------------------------------------------------------------------------
# Auto Hold cleanup -- entry edited/disabled/removed while a tap is pending
# ---------------------------------------------------------------------------


def test_cleanup_forces_release_when_hold_entry_disabled_mid_tap(engine):
    entry = _hold_auto_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    timer = engine._hold_pending["auto-1"].timer
    input_inject.send_key.reset_mock()

    entry.enabled = False
    _sync(engine, auto_entries=[entry])

    assert timer.cancelled is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)
    assert "auto-1" not in engine._hold_pending

    # The cancelled timer firing late must be a no-op.
    input_inject.send_key.reset_mock()
    timer.fn()
    input_inject.send_key.assert_not_called()


def test_cleanup_forces_release_when_hold_entry_removed_mid_tap(engine):
    entry = _hold_auto_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    timer = engine._hold_pending["auto-1"].timer
    input_inject.send_key.reset_mock()

    _sync(engine, auto_entries=[])

    assert timer.cancelled is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_cleanup_forces_release_when_mode_switched_off_hold_mid_tap(engine):
    entry = _hold_auto_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    timer = engine._hold_pending["auto-1"].timer
    input_inject.send_key.reset_mock()

    entry.mode = RemapMode.TOGGLE
    _sync(engine, auto_entries=[entry])

    assert timer.cancelled is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_cleanup_forces_release_when_key_changed_mid_tap(engine):
    entry = _hold_auto_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    timer = engine._hold_pending["auto-1"].timer
    input_inject.send_key.reset_mock()

    entry.key = KEY2
    _sync(engine, auto_entries=[entry])

    assert timer.cancelled is True
    # Released using the OLD key.
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


# ---------------------------------------------------------------------------
# Cleanup -- window-filter focus loss
# ---------------------------------------------------------------------------


def test_cleanup_forces_release_of_toggle_on_focus_loss(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    entry = _toggle_entry()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    # Simulates window_select's own background thread having already
    # resolved the target exe to this pid -- see cached_target_pid()'s
    # docstring; the gate now compares against this, not a value snapshotted
    # in update_snapshot().
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 999)
    _sync(engine, auto_entries=[entry], selected=target)
    _press(engine, KEY.vk_code)  # latch on while the targeted process has focus
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    # Any physical event at all -- not just one matching an entry's key --
    # must notice the gate closing and force the release.
    result = engine._handle(0x41, up=False, name="A", time_ms=0)
    assert result is None  # gate closed -- this unrelated event passes through untouched
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_gate_reflects_a_live_target_pid_change_with_no_new_update_snapshot_call(engine, monkeypatch):
    # The gate must notice the target game restarting with a new pid without update_snapshot() being called again -- window_select's background thread keeps cached_target_pid() current instead.
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 999)
    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    _sync(engine, entries=[RemapEntry(id="r1", source=SOURCE, destination=DEST)], selected=target)

    _press(engine, SOURCE.vk_code)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=False)
    input_inject.send_key.reset_mock()

    # The game "restarted" with a new pid (1234) -- no _sync()/update_snapshot() call, only cached_target_pid() changing. cached_foreground_pid() still reports the OLD pid, so the gate should read as closed.
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 1234)
    result = engine._handle(0x51, up=False, name="Q", time_ms=0)  # unrelated key
    assert result is None  # gate closed -- 999 (foreground) != 1234 (new target)
    input_inject.send_key.reset_mock()  # gate-close force-released the held remap; not what's under test here

    # Once the relaunched game actually takes foreground focus (new pid),
    # the gate reopens -- again with no update_snapshot() call in between.
    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 1234)
    _press(engine, SOURCE.vk_code)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=False)


def test_gate_close_listener_fires_exactly_on_the_open_to_closed_transition(engine, monkeypatch):
    # Added for macro_engine.py's handle_gate_closed() -- the only way a live Hold/Toggle macro session learns its trigger was released while the targeted process was unfocused.
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    calls = []
    engine.add_gate_close_listener(lambda: calls.append(1))

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 999)
    _sync(engine, selected=target)
    engine._handle(0x41, up=False, name="A", time_ms=0)  # gate already open -- no transition
    assert calls == []

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    engine._handle(0x41, up=False, name="A", time_ms=0)  # open -> closed
    assert calls == [1]

    engine._handle(0x41, up=False, name="A", time_ms=0)  # still closed -- no repeat fire
    assert calls == [1]

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    engine._handle(0x41, up=False, name="A", time_ms=0)  # closed -> open -- not a close transition
    assert calls == [1]


def test_gate_close_listener_exception_does_not_break_the_hook(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")

    def _boom():
        raise RuntimeError("listener blew up")

    engine.add_gate_close_listener(_boom)
    entry = _toggle_entry()
    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 999)
    _sync(engine, auto_entries=[entry], selected=target)
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    # Must not raise, and must still run this module's own cleanup despite
    # the listener above throwing.
    result = engine._handle(0x41, up=False, name="A", time_ms=0)
    assert result is None
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_cleanup_forces_release_of_pending_hold_tap_on_focus_loss(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    entry = _hold_auto_entry()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 999)
    _sync(engine, auto_entries=[entry], selected=target)
    _press(engine, KEY.vk_code)
    timer = engine._hold_pending["auto-1"].timer
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    result = engine._handle(0x41, up=False, name="A", time_ms=0)
    assert result is None
    assert timer.cancelled is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_focus_regain_does_not_re_latch_toggle(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    entry = _toggle_entry()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 999)
    _sync(engine, auto_entries=[entry], selected=target)
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    engine._handle(0x41, up=False, name="A", time_ms=0)  # focus lost, forces release
    # User actually lets go of the physical key while tabbed away -- without
    # this, KEY would still read as physically down and the "fresh" press
    # below would be (correctly) treated as an OS repeat, not a new press.
    _release(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    engine._handle(0x41, up=False, name="A", time_ms=0)  # focus regained
    input_inject.send_key.assert_not_called()  # gate reopening alone injects nothing

    # Toggle state was cleared, not preserved -- next press starts a fresh latch.
    _press(engine, KEY.vk_code)
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=False)


# ---------------------------------------------------------------------------
# Cleanup -- engine stop()/app exit
# ---------------------------------------------------------------------------


def test_stop_forces_release_of_latched_toggle(engine):
    entry = _toggle_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    input_inject.send_key.reset_mock()

    engine._started = True  # bypass start() -- never install a real hook in this suite
    engine.stop()

    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


def test_stop_cancels_and_releases_pending_hold_tap(engine):
    entry = _hold_auto_entry()
    _sync(engine, auto_entries=[entry])
    _press(engine, KEY.vk_code)
    timer = engine._hold_pending["auto-1"].timer
    input_inject.send_key.reset_mock()

    engine._started = True
    engine.stop()

    assert timer.cancelled is True
    input_inject.send_key.assert_called_once_with(KEY.vk_code, key_up=True)


# ---------------------------------------------------------------------------
# Regression: a standard remap held through a gate close or stop() used to stay stuck, since
# _force_release_pending() only knew about _toggle_on and _handle() returns early while the gate is closed.
# ---------------------------------------------------------------------------


def test_cleanup_forces_release_of_held_standard_remap_on_focus_loss(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    entry = RemapEntry(id="r1", source=SOURCE, destination=DEST)

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    monkeypatch.setattr(window_select, "cached_target_pid", lambda: 999)
    _sync(engine, entries=[entry], selected=target)
    _press(engine, SOURCE.vk_code)  # source held down while the targeted process has focus
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    # Focus lost while still physically held, then the physical release
    # arrives after the gate has already closed -- the exact sequence that
    # used to leave the destination stuck.
    result = engine._handle(0x41, up=False, name="A", time_ms=0)
    assert result is None  # gate closed -- this unrelated event passes through untouched
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)
    input_inject.send_key.reset_mock()

    released = _release(engine, SOURCE.vk_code)
    assert released is None  # still inert -- gate stays closed, event passes through untouched
    input_inject.send_key.assert_not_called()  # nothing left to release a second time


def test_stop_forces_release_of_held_standard_remap(engine):
    entry = RemapEntry(id="r1", source=SOURCE, destination=DEST)
    _sync(engine, entries=[entry])
    _press(engine, SOURCE.vk_code)
    input_inject.send_key.reset_mock()

    engine._started = True  # bypass start() -- never install a real hook in this suite
    engine.stop()

    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)
