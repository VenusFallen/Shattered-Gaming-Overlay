"""tests/test_remapper_toggle.py -- unit coverage for RemapperEngine's
Toggle-mode remap latch (RemapEntry.mode) and every stuck-key cleanup path
that can leave one held: entry edit/disable/remove, profile switch (which
looks identical here -- apply_profile() just replaces AppState.remapper.
entries wholesale, see remapper.py's module docstring), window-filter focus
loss, and engine stop()/exit. Also covers the matching Hold-mode regression
found while building Toggle (fixed 2026-08-31): a Hold remap held through a
gate close or stop() used to stay stuck too.

Exercises RemapperEngine._handle() and update_snapshot() directly rather
than a live WH_KEYBOARD_LL hook or input_hooks' KeyEvent/MouseButtonEvent
wrappers -- _handle() is the one place a physical key/mouse-button event
actually gets matched, and there's no other per-event entry point without a
real hook installed (which this suite must never touch -- see
agent-rules.md and this task's testing constraints). A fresh
RemapperEngine() never calls start(), so no hook is ever installed.
input_inject.send_key/send_mouse_button are monkeypatched so nothing here
ever calls real SendInput, and window_select.cached_foreground_pid is
monkeypatched so nothing depends on real OS focus. Never touches
profiles.json/settings.json -- AppState/RemapperState/WindowSelectState are
built in memory only.
"""

from __future__ import annotations

from typing import List, Optional
from unittest.mock import Mock

import pytest

import input_inject
import remapper
import window_select
from app_state import ProcessInfo, RemapEntry, RemapMode, RemapperState, WindowSelectState
from key_capture import KeyBind

SOURCE = KeyBind(vk_code=0x54, name="T")
SOURCE2 = KeyBind(vk_code=0x59, name="Y")
DEST = KeyBind(vk_code=0x43, name="C")
DEST2 = KeyBind(vk_code=0x56, name="V")


@pytest.fixture
def engine() -> remapper.RemapperEngine:
    return remapper.RemapperEngine()


@pytest.fixture(autouse=True)
def _stub_injection(monkeypatch):
    monkeypatch.setattr(input_inject, "send_key", Mock())
    monkeypatch.setattr(input_inject, "send_mouse_button", Mock())
    # No process targeted by default -- gate always open unless a test opts in.
    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 0)
    yield


def _toggle_entry(entry_id: str = "remap-1", source: KeyBind = SOURCE, dest: KeyBind = DEST, enabled: bool = True) -> RemapEntry:
    return RemapEntry(id=entry_id, source=source, destination=dest, enabled=enabled, mode=RemapMode.TOGGLE)


def _sync(engine: remapper.RemapperEngine, entries: List[RemapEntry], selected: Optional[ProcessInfo] = None) -> None:
    engine.update_snapshot(RemapperState(entries=entries), WindowSelectState(selected=selected))


def _press(engine: remapper.RemapperEngine, vk: int = SOURCE.vk_code) -> Optional[bool]:
    return engine._handle(vk, up=False, name="", time_ms=0)


def _release(engine: remapper.RemapperEngine, vk: int = SOURCE.vk_code) -> Optional[bool]:
    return engine._handle(vk, up=True, name="", time_ms=0)


# ---------------------------------------------------------------------------
# Mode field -- backward compat
# ---------------------------------------------------------------------------


def test_mode_defaults_to_hold_when_unspecified():
    entry = RemapEntry(id="r1", source=SOURCE, destination=DEST)
    assert entry.mode == RemapMode.HOLD


def test_profiles_json_missing_mode_defaults_to_hold():
    import profiles

    raw = {
        "id": "r1",
        "source": {"vk_code": SOURCE.vk_code, "name": SOURCE.name},
        "destination": {"vk_code": DEST.vk_code, "name": DEST.name},
        "enabled": True,
        # no "mode" key -- profile saved before Toggle mode existed
    }
    entry = profiles._remap_entry_from_json(raw)
    assert entry.mode == RemapMode.HOLD


def test_profiles_json_round_trips_toggle_mode():
    import profiles

    entry = _toggle_entry()
    raw = profiles._remap_entry_to_json(entry)
    assert raw["mode"] == "Toggle"
    restored = profiles._remap_entry_from_json(raw)
    assert restored.mode == RemapMode.TOGGLE


# ---------------------------------------------------------------------------
# Toggle state machine -- happy path
# ---------------------------------------------------------------------------


def test_toggle_first_press_latches_destination_down(engine):
    _sync(engine, [_toggle_entry()])
    suppressed = _press(engine)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=False)


def test_toggle_second_press_releases_destination(engine):
    _sync(engine, [_toggle_entry()])
    _press(engine)
    input_inject.send_key.reset_mock()

    suppressed = _press(engine)
    assert suppressed is True
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


def test_toggle_third_press_latches_down_again(engine):
    _sync(engine, [_toggle_entry()])
    _press(engine)
    _press(engine)
    input_inject.send_key.reset_mock()

    _press(engine)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=False)


def test_toggle_release_is_a_noop(engine):
    _sync(engine, [_toggle_entry()])
    _press(engine)
    input_inject.send_key.reset_mock()

    suppressed = _release(engine)
    assert suppressed is True  # still suppressed -- physical release never leaks through
    input_inject.send_key.assert_not_called()


def test_toggle_release_before_any_press_is_a_noop(engine):
    _sync(engine, [_toggle_entry()])
    suppressed = _release(engine)
    assert suppressed is True
    input_inject.send_key.assert_not_called()


def test_hold_mode_still_mirrors_source_1to1(engine):
    # Regression: adding Toggle must not change today's default Hold behavior.
    entry = RemapEntry(id="r1", source=SOURCE, destination=DEST, mode=RemapMode.HOLD)
    _sync(engine, [entry])

    _press(engine)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=False)
    input_inject.send_key.reset_mock()

    _release(engine)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


# ---------------------------------------------------------------------------
# Cleanup -- entry edited/disabled/removed while toggled on
# ---------------------------------------------------------------------------


def test_cleanup_forces_release_when_entry_disabled(engine):
    entry = _toggle_entry()
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    entry.enabled = False
    _sync(engine, [entry])

    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


def test_cleanup_forces_release_when_entry_removed(engine):
    entry = _toggle_entry()
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    _sync(engine, [])

    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


def test_cleanup_forces_release_when_mode_switched_off_toggle(engine):
    entry = _toggle_entry()
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    entry.mode = RemapMode.HOLD
    _sync(engine, [entry])

    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


def test_cleanup_forces_release_when_destination_changed(engine):
    entry = _toggle_entry()
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    entry.destination = DEST2
    _sync(engine, [entry])

    # Released using the OLD destination -- that's what's actually latched
    # down, not whatever the entry now points to.
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


def test_cleanup_forces_release_on_profile_switch(engine):
    # apply_profile() replaces AppState.remapper.entries wholesale with a
    # different profile's entries (different ids) -- update_snapshot() can't
    # tell that apart from a delete, which is exactly what should happen:
    # nothing from the old profile should stay latched into the new one.
    entry = _toggle_entry(entry_id="profileA-remap-1")
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    other_entry = _toggle_entry(entry_id="profileB-remap-1", source=SOURCE2, dest=DEST2)
    _sync(engine, [other_entry])

    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


def test_source_only_rebind_does_not_force_release(engine):
    # Design decision: rebinding only the source key of an already-latched
    # Toggle entry doesn't touch the destination key's actual held state, so
    # it must not spuriously release it.
    entry = _toggle_entry()
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    entry.source = SOURCE2
    _sync(engine, [entry])
    input_inject.send_key.assert_not_called()

    # The still-latched toggle correctly releases off the NEW source key.
    engine._handle(SOURCE2.vk_code, up=False, name="", time_ms=0)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


# ---------------------------------------------------------------------------
# Cleanup -- window-filter focus loss
# ---------------------------------------------------------------------------


def test_cleanup_forces_release_on_focus_loss(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    entry = _toggle_entry()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    _sync(engine, [entry], selected=target)
    _press(engine)  # latch on while the targeted process has focus
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    # Any physical event at all -- not just one matching a remap source --
    # must notice the gate closing and force the release.
    result = engine._handle(0x41, up=False, name="A", time_ms=0)
    assert result is None  # gate closed -- this unrelated event passes through untouched
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


def test_focus_regain_does_not_re_latch(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    entry = _toggle_entry()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    _sync(engine, [entry], selected=target)
    _press(engine)
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    engine._handle(0x41, up=False, name="A", time_ms=0)  # focus lost, forces release
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    engine._handle(0x41, up=False, name="A", time_ms=0)  # focus regained
    input_inject.send_key.assert_not_called()  # gate reopening alone injects nothing

    # Toggle state was cleared, not preserved -- next press starts a fresh latch.
    _press(engine)
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=False)


# ---------------------------------------------------------------------------
# Cleanup -- engine stop()/app exit
# ---------------------------------------------------------------------------


def test_stop_forces_release_of_latched_toggle(engine):
    entry = _toggle_entry()
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    engine._started = True  # bypass start() -- never install a real hook in this suite
    engine.stop()

    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)


# ---------------------------------------------------------------------------
# Regression (fixed 2026-08-31): Hold-mode remap held through a gate close or
# stop() used to stay stuck -- _force_release_pending() only knew about
# _toggle_on, and _handle() returns early while the gate is closed, before
# Hold's own release-on-physical-up path ever ran. See remapper.py's module
# docstring, "Stuck-key prevention" section.
# ---------------------------------------------------------------------------


def _hold_entry(entry_id: str = "remap-1", source: KeyBind = SOURCE, dest: KeyBind = DEST, enabled: bool = True) -> RemapEntry:
    return RemapEntry(id=entry_id, source=source, destination=dest, enabled=enabled, mode=RemapMode.HOLD)


def test_cleanup_forces_release_of_held_hold_remap_on_focus_loss(engine, monkeypatch):
    target = ProcessInfo(pid=999, exe_name="game.exe", window_title="Game")
    entry = _hold_entry()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    _sync(engine, [entry], selected=target)
    _press(engine)  # source held down while the targeted process has focus
    input_inject.send_key.reset_mock()

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 111)
    # Focus lost while still physically held, then the physical release
    # arrives after the gate has already closed -- the exact sequence that
    # used to leave the destination stuck.
    result = engine._handle(0x41, up=False, name="A", time_ms=0)
    assert result is None  # gate closed -- this unrelated event passes through untouched
    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)
    input_inject.send_key.reset_mock()

    released = _release(engine)
    assert released is None  # still inert -- gate stays closed, event passes through untouched
    input_inject.send_key.assert_not_called()  # nothing left to release a second time


def test_stop_forces_release_of_held_hold_remap(engine):
    entry = _hold_entry()
    _sync(engine, [entry])
    _press(engine)
    input_inject.send_key.reset_mock()

    engine._started = True  # bypass start() -- never install a real hook in this suite
    engine.stop()

    input_inject.send_key.assert_called_once_with(DEST.vk_code, key_up=True)
