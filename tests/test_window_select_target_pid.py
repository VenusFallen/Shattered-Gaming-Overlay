"""tests/test_window_select_target_pid.py -- coverage for window_select.py's
`set_target_exe_name()`/`cached_target_pid()`/`_maybe_reacquire_target()`,
the live, background-thread-maintained pid resolution that makes
remapper.py's window-filter gate resilient to the targeted game restarting
mid-session with a new pid.

Found live 2026-09-15: `_reacquire_selected_if_stale()` (see
test_window_select_reacquire.py) only runs as part of `refresh_if_stale()`,
which is only ever called from the Companion window's own render callback --
and that callback stops firing entirely the instant the window is minimized,
which is exactly how the app is normally left for the rest of a gaming
session. remapper.py's gate used to read a pid snapshotted once in
`update_snapshot()` (also Companion-frame-driven), so if the targeted game
restarted with a new pid while the Companion window sat minimized, nothing
ever noticed -- the user had to un-minimize, wait for a frame, AND have that
frame's ambient refresh catch it, or just manually reselect.

This module's fix: `set_target_exe_name()` only needs to be called ONCE (by
remapper.py's `update_snapshot()`, itself still only Companion-frame-driven)
to arm the target; from then on, `cached_target_pid()` is kept fresh by the
SAME independent background thread that already tracks foreground focus
regardless of window state -- no further Companion-frame activity required.

Never spins the real background thread or makes a real Win32 EnumWindows
call -- `_maybe_reacquire_target()` is exercised directly (mirroring how
`_focus_poll_loop()` calls it), with `enumerate_target_windows()`
monkeypatched.
"""

from __future__ import annotations

import pytest

import window_select
from app_state import ProcessInfo


@pytest.fixture(autouse=True)
def _reset_target_state():
    window_select._target_exe_name = None
    window_select._cached_target_pid = 0
    window_select._last_target_reacquire_monotonic = 0.0
    yield
    window_select._target_exe_name = None
    window_select._cached_target_pid = 0
    window_select._last_target_reacquire_monotonic = 0.0


def test_cached_target_pid_is_zero_with_no_target_set():
    assert window_select.cached_target_pid() == 0


def test_set_target_exe_name_none_clears_any_prior_target():
    window_select.set_target_exe_name("eft.exe")
    window_select._cached_target_pid = 12345  # simulate an already-resolved pid

    window_select.set_target_exe_name(None)

    assert window_select.cached_target_pid() == 0


def test_maybe_reacquire_resolves_pid_for_the_armed_target(monkeypatch):
    fresh = ProcessInfo(pid=9999, exe_name="eft.exe", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [fresh])

    window_select.set_target_exe_name("eft.exe")
    window_select._maybe_reacquire_target(now=100.0)

    assert window_select.cached_target_pid() == 9999


def test_maybe_reacquire_matches_case_insensitively(monkeypatch):
    fresh = ProcessInfo(pid=9999, exe_name="EFT.EXE", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [fresh])

    window_select.set_target_exe_name("eft.exe")
    window_select._maybe_reacquire_target(now=100.0)

    assert window_select.cached_target_pid() == 9999


def test_maybe_reacquire_is_a_noop_when_no_target_armed(monkeypatch):
    calls = {"n": 0}

    def _enumerate():
        calls["n"] += 1
        return []

    monkeypatch.setattr(window_select, "enumerate_target_windows", _enumerate)

    window_select._maybe_reacquire_target(now=100.0)

    assert calls["n"] == 0  # never even enumerates with nothing to resolve


def test_maybe_reacquire_respects_the_throttle_interval(monkeypatch):
    calls = {"n": 0}

    def _enumerate():
        calls["n"] += 1
        return [ProcessInfo(pid=9999, exe_name="eft.exe", window_title="EFT")]

    monkeypatch.setattr(window_select, "enumerate_target_windows", _enumerate)
    window_select.set_target_exe_name("eft.exe")  # resets the throttle to force-due

    window_select._maybe_reacquire_target(now=100.0)
    assert calls["n"] == 1

    # Well within _TARGET_REACQUIRE_INTERVAL_SEC of the last resolve -- must
    # not re-enumerate yet.
    window_select._maybe_reacquire_target(now=100.5)
    assert calls["n"] == 1

    # Interval has elapsed -- due again.
    window_select._maybe_reacquire_target(now=100.0 + window_select._TARGET_REACQUIRE_INTERVAL_SEC + 0.1)
    assert calls["n"] == 2


def test_set_target_exe_name_forces_immediate_reacquire_on_target_change(monkeypatch):
    # A fresh target shouldn't have to wait out whatever's left of the OLD
    # target's throttle window.
    old_target = ProcessInfo(pid=1, exe_name="old.exe", window_title="Old")
    new_target = ProcessInfo(pid=2, exe_name="new.exe", window_title="New")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [old_target, new_target])

    window_select.set_target_exe_name("old.exe")
    window_select._maybe_reacquire_target(now=100.0)
    assert window_select.cached_target_pid() == 1

    window_select.set_target_exe_name("new.exe")
    # Immediately due despite being called right after the previous resolve.
    window_select._maybe_reacquire_target(now=100.01)
    assert window_select.cached_target_pid() == 2


def test_maybe_reacquire_survives_enumeration_error(monkeypatch):
    def _raise():
        raise OSError("EnumWindows failed")

    monkeypatch.setattr(window_select, "enumerate_target_windows", _raise)
    window_select.set_target_exe_name("eft.exe")

    window_select._maybe_reacquire_target(now=100.0)  # must not raise

    assert window_select.cached_target_pid() == 0


def test_full_scenario_app_boots_before_game_then_game_launches(monkeypatch):
    """The exact real-world sequence this fix was for: a profile loads
    (arming the target) before the game is running at all -- correctly
    inert -- then the game launches later, picked up on the next tick with
    no Companion-frame involved at either step."""
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [])
    window_select.set_target_exe_name("eft.exe")  # profile loaded, game not running yet
    window_select._maybe_reacquire_target(now=100.0)
    assert window_select.cached_target_pid() == 0  # correctly inert -- nothing to gate against yet

    game = ProcessInfo(pid=9999, exe_name="eft.exe", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [game])

    window_select._maybe_reacquire_target(now=100.0 + window_select._TARGET_REACQUIRE_INTERVAL_SEC + 0.1)

    assert window_select.cached_target_pid() == 9999
