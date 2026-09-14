"""tests/test_profiles_window_select_restore.py -- coverage for
profiles.py's `_resolve_window_select_target()`/`apply_profile()` re-
resolving a persisted Window Select target by exe name instead of trusting
its saved pid directly.

Found live 2026-09-13: a pid saved in profiles.json is only valid for the
OS process instance that was running at save time -- pids aren't stable
across that game's own restarts, so after rebooting the app (or the game),
a profile's saved Window Select target silently stopped actually gating the
Remapper/Macros (remapper.py's window-filter gate stays permanently closed
since no window ever has that dead pid again), indistinguishable from
"nothing selected" without opening Settings to notice.

Deliberately monkeypatches profiles.PROFILES_FILE per-test (see
test_profiles_share.py's own fixture, same convention) so `_write_all()`'s
real disk write never touches the actual %LOCALAPPDATA%/profiles.json, and
monkeypatches profiles.window_select.enumerate_target_windows() so this
suite never depends on real running processes or makes real Win32 calls.
"""

from __future__ import annotations

import pytest

import app_state
import profiles
import window_select
from app_state import ProcessInfo


@pytest.fixture(autouse=True)
def _isolate_profiles_file(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES_FILE", tmp_path / "profiles.json")
    yield


@pytest.fixture
def state() -> app_state.AppState:
    return app_state.new_app_state()


def _make_profile_with_window_select(state: app_state.AppState, ws_payload: dict) -> app_state.ProfileDef:
    profile = state.profiles.add_profile("My Game")
    profile.persist_window_select = True
    profiles._payload_cache[profile.id] = {
        "entries": [],
        "auto_entries": [],
        "macros": [],
        "window_select": ws_payload,
        "overlay": app_state.OverlayState(),
    }
    return profile


def test_apply_profile_re_resolves_stale_pid_to_the_currently_running_one(state, monkeypatch):
    # Saved with an old pid from a previous launch of the game.
    stale_ws = {"pid": 1111, "exe_name": "eft.exe", "window_title": "Escape from Tarkov"}
    profile = _make_profile_with_window_select(state, stale_ws)

    # The game is running again now, but Windows gave it a new pid.
    fresh = ProcessInfo(pid=9999, exe_name="eft.exe", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [fresh])

    assert profiles.apply_profile(state, profile.id) is True

    assert state.window_select.selected == fresh
    assert state.window_select.selected.pid == 9999  # NOT the stale 1111


def test_apply_profile_matches_exe_name_case_insensitively(state, monkeypatch):
    stale_ws = {"pid": 1111, "exe_name": "EFT.EXE", "window_title": "Escape from Tarkov"}
    profile = _make_profile_with_window_select(state, stale_ws)

    fresh = ProcessInfo(pid=9999, exe_name="eft.exe", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [fresh])

    profiles.apply_profile(state, profile.id)

    assert state.window_select.selected.pid == 9999


def test_apply_profile_falls_back_to_saved_pid_when_exe_not_currently_running(state, monkeypatch):
    stale_ws = {"pid": 1111, "exe_name": "eft.exe", "window_title": "Escape from Tarkov"}
    profile = _make_profile_with_window_select(state, stale_ws)

    # Game isn't running at all right now -- nothing to re-resolve against.
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [])

    profiles.apply_profile(state, profile.id)

    assert state.window_select.selected == ProcessInfo(pid=1111, exe_name="eft.exe", window_title="Escape from Tarkov")


def test_apply_profile_ignores_other_running_processes(state, monkeypatch):
    stale_ws = {"pid": 1111, "exe_name": "eft.exe", "window_title": "Escape from Tarkov"}
    profile = _make_profile_with_window_select(state, stale_ws)

    other = ProcessInfo(pid=2222, exe_name="discord.exe", window_title="Discord")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [other])

    profiles.apply_profile(state, profile.id)

    # Falls back to the saved pid -- must not accidentally latch onto an
    # unrelated running process.
    assert state.window_select.selected.pid == 1111


def test_apply_profile_survives_enumeration_error(state, monkeypatch):
    stale_ws = {"pid": 1111, "exe_name": "eft.exe", "window_title": "Escape from Tarkov"}
    profile = _make_profile_with_window_select(state, stale_ws)

    def _raise():
        raise OSError("EnumWindows failed")

    monkeypatch.setattr(window_select, "enumerate_target_windows", _raise)

    # Must not raise -- best-effort, same as window_select.py's own
    # refresh_if_stale()/force_refresh() OSError handling.
    profiles.apply_profile(state, profile.id)

    assert state.window_select.selected.pid == 1111


def test_apply_profile_without_persist_window_select_stays_global(state, monkeypatch):
    stale_ws = {"pid": 1111, "exe_name": "eft.exe", "window_title": "Escape from Tarkov"}
    profile = _make_profile_with_window_select(state, stale_ws)
    profile.persist_window_select = False

    fresh = ProcessInfo(pid=9999, exe_name="eft.exe", window_title="Escape from Tarkov")
    monkeypatch.setattr(window_select, "enumerate_target_windows", lambda: [fresh])

    profiles.apply_profile(state, profile.id)

    # persist_window_select off -- re-resolution must not even be attempted;
    # selection drops back to Global per the existing safety pattern.
    assert state.window_select.selected is None
