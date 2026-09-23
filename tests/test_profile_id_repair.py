"""Unit coverage for the uuid-based id generators (app_state._next_id / profiles._fallback_id) and
profiles.py's load-time duplicate-id repair pass (profiles._repair_duplicate_ids(), wired into load_all()).
Closes a real bug where a restarting in-memory counter let ids collide across sessions. Isolates
profiles.PROFILES_FILE to tmp_path -- never touches the real %LOCALAPPDATA%/profiles.json.
"""

from __future__ import annotations

import json

import pytest

import app_state
import profiles


@pytest.fixture(autouse=True)
def _isolate_profiles_file(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "PROFILES_FILE", tmp_path / "profiles.json")
    yield


# ---------------------------------------------------------------------------
# Id generators -- not a static/predictable counter anymore
# ---------------------------------------------------------------------------


def test_app_state_next_id_not_a_static_counter():
    ids = [app_state._next_id("profile") for _ in range(50)]
    assert len(set(ids)) == 50  # every call produced a distinct id
    assert all(i.startswith("profile-") for i in ids)
    # A plain restarting counter would reproduce this exact sequence every
    # run; a uuid-based generator won't.
    assert ids != [f"profile-{n}" for n in range(1, 51)]


def test_profiles_fallback_id_not_a_static_counter():
    ids = {profiles._fallback_id("profile") for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("profile-restored-") for i in ids)


# ---------------------------------------------------------------------------
# _repair_duplicate_ids -- pure function
# ---------------------------------------------------------------------------


def _minimal_profile(profile_id: str, name: str) -> app_state.ProfileDef:
    return app_state.ProfileDef(id=profile_id, name=name)


def test_repair_leaves_unique_ids_untouched():
    parsed = [
        (_minimal_profile("profile-1", "A"), {"entries": [], "macros": [], "window_select": None}),
        (_minimal_profile("profile-2", "B"), {"entries": [], "macros": [], "window_select": None}),
    ]
    changed = profiles._repair_duplicate_ids(parsed)
    assert changed is False
    assert [p.id for p, _ in parsed] == ["profile-1", "profile-2"]


def test_repair_reassigns_all_but_the_last_duplicate_occurrence():
    a = _minimal_profile("profile-1", "First")
    b = _minimal_profile("profile-1", "Second")
    c = _minimal_profile("profile-1", "Third")
    parsed = [
        (a, {"entries": [], "macros": [], "window_select": None}),
        (b, {"entries": [], "macros": [], "window_select": None}),
        (c, {"entries": [], "macros": [], "window_select": None}),
    ]

    changed = profiles._repair_duplicate_ids(parsed)

    assert changed is True
    # Last occurrence on disk keeps the original id...
    assert c.id == "profile-1"
    # ...every earlier occurrence gets reassigned, and all ids end up unique.
    assert a.id != "profile-1"
    assert b.id != "profile-1"
    assert len({a.id, b.id, c.id}) == 3
    # Names/payload identity untouched -- no data dropped or merged.
    assert [p.name for p, _ in parsed] == ["First", "Second", "Third"]


# ---------------------------------------------------------------------------
# load_all -- full self-heal against a synthetic corrupted profiles.json
# ---------------------------------------------------------------------------


def _write_raw_profiles_json(path, active_id: str, raw_profiles: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump({"active_id": active_id, "profiles": raw_profiles}, f)


def _raw_profile(profile_id: str, name: str, protected: bool = False) -> dict:
    return {
        "id": profile_id,
        "name": name,
        "protected": protected,
        "persist_remapper": False,
        "persist_macros": False,
        "persist_window_select": False,
        "target_executable": "",
        "remapper": {"entries": []},
        "macros": {"macros": []},
        "window_select": None,
        "overlay": {},
    }


def test_load_all_repairs_duplicate_ids_and_preserves_active_profile(tmp_path):
    # Default (protected) plus two colliding non-protected profiles; active_id points at the later-written one.
    raw = [
        _raw_profile("profile-default", "Default", protected=True),
        _raw_profile("profile-restored-1", "Tarkov"),
        _raw_profile("profile-restored-1", "Battlefield"),
    ]
    _write_raw_profiles_json(profiles.PROFILES_FILE, active_id="profile-restored-1", raw_profiles=raw)

    state = app_state.new_app_state()
    profiles.load_all(state)

    ids = [p.id for p in state.profiles.profiles]
    names = [p.name for p in state.profiles.profiles]
    assert len(ids) == len(set(ids))  # no more duplicates
    assert "Tarkov" in names and "Battlefield" in names  # both profiles survived

    # active_id still resolves to the profile actually active before repair (the later-written "Battlefield").
    active = next(p for p in state.profiles.profiles if p.id == state.profiles.active_id)
    assert active.name == "Battlefield"

    tarkov = next(p for p in state.profiles.profiles if p.name == "Tarkov")
    assert tarkov.id != active.id


def test_load_all_repair_persists_fixed_ids_back_to_disk(tmp_path):
    raw = [
        _raw_profile("profile-default", "Default", protected=True),
        _raw_profile("profile-dup", "One"),
        _raw_profile("profile-dup", "Two"),
    ]
    _write_raw_profiles_json(profiles.PROFILES_FILE, active_id="profile-dup", raw_profiles=raw)

    state = app_state.new_app_state()
    profiles.load_all(state)

    with profiles.PROFILES_FILE.open("r", encoding="utf-8") as f:
        on_disk = json.load(f)
    disk_ids = [p["id"] for p in on_disk["profiles"]]
    assert len(disk_ids) == len(set(disk_ids))


def test_load_all_skips_a_malformed_profile_entry_instead_of_crashing(tmp_path):
    # A hand-edited/corrupted entry with a wrong-typed field (remapper as a
    # string, not a dict) must not take down startup for every other profile.
    bad = _raw_profile("profile-bad", "Corrupt", protected=True)
    bad["remapper"] = "not-a-dict"
    good = _raw_profile("profile-good", "Good")
    _write_raw_profiles_json(profiles.PROFILES_FILE, active_id="profile-bad", raw_profiles=[bad, good])

    state = app_state.new_app_state()
    profiles.load_all(state)  # must not raise

    names = [p.name for p in state.profiles.profiles]
    assert "Corrupt" not in names
    assert "Good" in names
    # No protected profile survived the bad entry -- a fresh Default is synthesized.
    assert any(p.protected for p in state.profiles.profiles)


def test_load_all_repair_does_not_drop_either_profiles_payload(tmp_path):
    one = _raw_profile("profile-dup", "One")
    one["remapper"] = {"entries": [{"id": "r1", "source": {"vk_code": 65, "name": "A"},
                                     "destination": {"vk_code": 66, "name": "B"}, "enabled": True, "mode": "Hold"}]}
    two = _raw_profile("profile-dup", "Two")
    two["remapper"] = {"entries": [{"id": "r2", "source": {"vk_code": 67, "name": "C"},
                                     "destination": {"vk_code": 68, "name": "D"}, "enabled": True, "mode": "Hold"}]}
    raw = [_raw_profile("profile-default", "Default", protected=True), one, two]
    _write_raw_profiles_json(profiles.PROFILES_FILE, active_id="profile-dup", raw_profiles=raw)

    state = app_state.new_app_state()
    profiles.load_all(state)

    by_name = {p.name: p.id for p in state.profiles.profiles}
    with profiles._payload_lock:
        one_entries = profiles._payload_cache[by_name["One"]]["entries"]
        two_entries = profiles._payload_cache[by_name["Two"]]["entries"]
    assert one_entries[0].source.name == "A"
    assert two_entries[0].source.name == "C"
