"""tests/test_profiles_auto_switch.py -- unit coverage for
profiles.check_auto_switch(), the focus-driven profile auto-switch entry
point (see profiles.py's module docstring).

Deliberately does not go through apply_profile()'s real load/persist/disk-
write path -- that's already exercised by manual/live profile Load testing,
and letting it run here would touch the real profiles.json under
%LOCALAPPDATA%. window_select.cached_foreground_pid() and
psutil.Process().name() are stubbed so none of this depends on real OS
process/focus state.

_auto_switch_last_pid is module-level state in profiles.py (see that
module's own comment on it) -- the autouse fixture below resets it before
and after every test so runs can't leak pid state into each other.
"""

from __future__ import annotations

from unittest.mock import Mock

import psutil
import pytest

import app_state
import profiles
import window_select


class _FakeProcess:
    """Stand-in for psutil.Process -- only .name() is ever called by
    check_auto_switch()."""

    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


@pytest.fixture(autouse=True)
def _reset_auto_switch_last_pid():
    profiles._auto_switch_last_pid = 0
    yield
    profiles._auto_switch_last_pid = 0


@pytest.fixture
def state() -> app_state.AppState:
    return app_state.new_app_state()


def _add_profile(state: app_state.AppState, name: str, target_executable: str) -> app_state.ProfileDef:
    profile = state.profiles.add_profile(name)
    profile.target_executable = target_executable
    return profile


def test_noop_when_toggle_off(state, monkeypatch):
    _add_profile(state, "Game", "game.exe")
    state.settings.auto_switch_profiles = False

    fg_pid = Mock(return_value=4321)
    monkeypatch.setattr(window_select, "cached_foreground_pid", fg_pid)
    apply_mock = Mock()
    monkeypatch.setattr(profiles, "apply_profile", apply_mock)

    profiles.check_auto_switch(state)

    # Toggle-off is the first check -- shouldn't even read focus.
    fg_pid.assert_not_called()
    apply_mock.assert_not_called()


def test_noop_when_pid_unchanged(state, monkeypatch):
    _add_profile(state, "Game", "game.exe")
    state.settings.auto_switch_profiles = True
    profiles._auto_switch_last_pid = 4321

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 4321)
    process_mock = Mock(side_effect=AssertionError("psutil.Process should not run when pid is unchanged"))
    monkeypatch.setattr(psutil, "Process", process_mock)
    apply_mock = Mock()
    monkeypatch.setattr(profiles, "apply_profile", apply_mock)

    profiles.check_auto_switch(state)

    process_mock.assert_not_called()
    apply_mock.assert_not_called()


def test_noop_when_pid_is_zero(state, monkeypatch):
    # foreground_pid()/cached_foreground_pid() return 0 for the transient
    # no-foreground-window moment right at a focus switch.
    _add_profile(state, "Game", "game.exe")
    state.settings.auto_switch_profiles = True
    profiles._auto_switch_last_pid = 4321  # prior real pid, so 0 counts as "changed"

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 0)
    process_mock = Mock(side_effect=AssertionError("psutil.Process should not run for pid 0"))
    monkeypatch.setattr(psutil, "Process", process_mock)
    apply_mock = Mock()
    monkeypatch.setattr(profiles, "apply_profile", apply_mock)

    profiles.check_auto_switch(state)

    process_mock.assert_not_called()
    apply_mock.assert_not_called()


def test_loads_matching_profile_case_insensitive(state, monkeypatch):
    game = _add_profile(state, "Game", "game.exe")
    other = _add_profile(state, "Other", "")
    state.settings.auto_switch_profiles = True
    state.profiles.active_id = other.id

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 777)
    monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProcess("Game.EXE"))
    apply_mock = Mock()
    monkeypatch.setattr(profiles, "apply_profile", apply_mock)

    profiles.check_auto_switch(state)

    apply_mock.assert_called_once_with(state, game.id)


def test_noop_when_no_profile_matches(state, monkeypatch):
    _add_profile(state, "Game", "game.exe")
    state.settings.auto_switch_profiles = True

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 999)
    monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProcess("explorer.exe"))
    apply_mock = Mock()
    monkeypatch.setattr(profiles, "apply_profile", apply_mock)

    profiles.check_auto_switch(state)

    apply_mock.assert_not_called()


def test_noop_when_match_already_active(state, monkeypatch):
    game = _add_profile(state, "Game", "game.exe")
    state.settings.auto_switch_profiles = True
    state.profiles.active_id = game.id

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 555)
    monkeypatch.setattr(psutil, "Process", lambda pid: _FakeProcess("game.exe"))
    apply_mock = Mock()
    monkeypatch.setattr(profiles, "apply_profile", apply_mock)

    profiles.check_auto_switch(state)

    apply_mock.assert_not_called()


@pytest.mark.parametrize("exc", [psutil.NoSuchProcess(1234), psutil.AccessDenied(1234)])
def test_handles_process_lookup_failure(state, monkeypatch, exc):
    _add_profile(state, "Game", "game.exe")
    state.settings.auto_switch_profiles = True

    monkeypatch.setattr(window_select, "cached_foreground_pid", lambda: 1234)
    monkeypatch.setattr(psutil, "Process", Mock(side_effect=exc))
    apply_mock = Mock()
    monkeypatch.setattr(profiles, "apply_profile", apply_mock)

    profiles.check_auto_switch(state)  # must not raise

    apply_mock.assert_not_called()
