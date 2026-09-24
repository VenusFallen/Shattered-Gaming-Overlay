"""Coverage for window_select.py's `_reacquire_selected_if_stale()`, which keeps a selected Window Select
target working across that process's own restarts by re-checking on every ambient enumeration refresh,
not just once at profile-load time. Never makes a real Win32 EnumWindows call -- `enumerate_target_windows()`
is monkeypatched wherever `force_refresh()`/`refresh_if_stale()` is exercised.
"""

from __future__ import annotations

import pytest

import window_select
from app_state import ProcessInfo, WindowSelectState


@pytest.fixture(autouse=True)
def _reset_refresh_throttle():
    # Module-level throttle state -- reset so tests don't depend on
    # execution order or leak into each other.
    window_select._last_refresh_monotonic = 0.0
    yield
    window_select._last_refresh_monotonic = 0.0


def test_reacquire_noop_when_selected_pid_still_live():
    selected = ProcessInfo(pid=100, exe_name="eft.exe", window_title="Escape from Tarkov")
    state = WindowSelectState(selected=selected, available=[selected])

    window_select._reacquire_selected_if_stale(state)

    assert state.selected is selected  # untouched


def test_reacquire_swaps_to_fresh_pid_when_stale():
    stale = ProcessInfo(pid=100, exe_name="eft.exe", window_title="Escape from Tarkov")
    fresh = ProcessInfo(pid=999, exe_name="eft.exe", window_title="Escape from Tarkov")
    state = WindowSelectState(selected=stale, available=[fresh])

    window_select._reacquire_selected_if_stale(state)

    assert state.selected == fresh
    assert state.selected.pid == 999


def test_reacquire_matches_exe_name_case_insensitively():
    stale = ProcessInfo(pid=100, exe_name="EFT.EXE", window_title="Escape from Tarkov")
    fresh = ProcessInfo(pid=999, exe_name="eft.exe", window_title="Escape from Tarkov")
    state = WindowSelectState(selected=stale, available=[fresh])

    window_select._reacquire_selected_if_stale(state)

    assert state.selected.pid == 999


def test_reacquire_leaves_stale_target_alone_when_no_match_found():
    stale = ProcessInfo(pid=100, exe_name="eft.exe", window_title="Escape from Tarkov")
    other = ProcessInfo(pid=200, exe_name="discord.exe", window_title="Discord")
    state = WindowSelectState(selected=stale, available=[other])

    window_select._reacquire_selected_if_stale(state)

    assert state.selected is stale  # nothing to swap to yet -- game still not running


def test_reacquire_is_a_noop_with_no_selection():
    state = WindowSelectState(selected=None, available=[ProcessInfo(pid=1, exe_name="a.exe", window_title="A")])

    window_select._reacquire_selected_if_stale(state)  # must not raise

    assert state.selected is None


def test_force_refresh_reacquires_stale_selection(monkeypatch):
    stale = ProcessInfo(pid=100, exe_name="eft.exe", window_title="Escape from Tarkov")
    fresh = ProcessInfo(pid=999, exe_name="eft.exe", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [fresh])
    state = WindowSelectState(selected=stale)

    window_select.force_refresh(state)

    assert state.available == [fresh]
    assert state.selected == fresh


def test_refresh_if_stale_reacquires_when_it_actually_refreshes(monkeypatch):
    stale = ProcessInfo(pid=100, exe_name="eft.exe", window_title="Escape from Tarkov")
    fresh = ProcessInfo(pid=999, exe_name="eft.exe", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [fresh])
    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 0)
    state = WindowSelectState(selected=stale)

    window_select.refresh_if_stale(state)  # first call always refreshes (throttle reset by fixture)

    assert state.selected == fresh


def test_refresh_if_stale_does_not_reacquire_within_the_throttle_window(monkeypatch):
    stale = ProcessInfo(pid=100, exe_name="eft.exe", window_title="Escape from Tarkov")
    fresh = ProcessInfo(pid=999, exe_name="eft.exe", window_title="Escape from Tarkov")
    calls = {"n": 0}

    def _enumerate():
        calls["n"] += 1
        return [fresh]

    monkeypatch.setattr(window_select, "enumerate_target_windows", _enumerate)
    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 0)
    state = WindowSelectState(selected=stale)

    window_select.refresh_if_stale(state)  # consumes the throttle window
    assert calls["n"] == 1
    assert state.selected == fresh

    # A second immediate call must not re-enumerate (throttled) -- selection
    # stays whatever the last real refresh resolved.
    window_select.refresh_if_stale(state)
    assert calls["n"] == 1


def test_reacquire_survives_enumeration_error(monkeypatch):
    stale = ProcessInfo(pid=100, exe_name="eft.exe", window_title="Escape from Tarkov")

    def _raise():
        raise OSError("EnumWindows failed")

    monkeypatch.setattr(window_select, "enumerate_target_windows", _raise)
    state = WindowSelectState(selected=stale)

    # Must not raise -- best-effort, matches this module's existing
    # OSError-swallowing convention.
    window_select.force_refresh(state)

    assert state.selected is stale
