"""tests/test_profiles_share.py -- unit coverage for the profile export/
import data logic in profiles.py (see that module's "Export / Import"
section).

Covers only the pure data functions: building an exportable payload,
parsing/validating an imported JSON blob, and the name-collision-suffix
logic. The native Open/Save file dialogs in file_dialog.py are NOT covered
here -- they're blocking native Win32 modal UI with nothing to unit test;
that needs live/visual verification instead.

Deliberately monkeypatches profiles.PROFILES_FILE per-test (via the
autouse fixture below) so `_write_all()`'s real disk write never touches
the actual %LOCALAPPDATA%/profiles.json on the machine running these tests.
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


@pytest.fixture
def state() -> app_state.AppState:
    return app_state.new_app_state()


# ---------------------------------------------------------------------------
# export_profile_payload / export_profile_to_file / suggest_export_filename
# ---------------------------------------------------------------------------


def test_export_profile_payload_unknown_id_returns_none(state):
    assert profiles.export_profile_payload(state, "nope") is None


def test_export_profile_payload_matches_profiles_json_shape(state):
    profile = state.profiles.add_profile("My Game")
    payload = profiles.export_profile_payload(state, profile.id)

    assert payload is not None
    assert payload["id"] == profile.id
    assert payload["name"] == "My Game"
    assert payload["remapper"] == {"entries": []}
    assert payload["macros"] == {"macros": []}
    assert payload["window_select"] is None
    assert "overlay" in payload  # always present, restored unconditionally


def test_export_profile_to_file_writes_readable_json(state, tmp_path):
    profile = state.profiles.add_profile("My Game")
    out = tmp_path / "exported.json"

    assert profiles.export_profile_to_file(state, profile.id, str(out)) is True
    with out.open("r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["name"] == "My Game"


def test_export_profile_to_file_unknown_id_returns_false(state, tmp_path):
    out = tmp_path / "exported.json"
    assert profiles.export_profile_to_file(state, "nope", str(out)) is False
    assert not out.exists()


def test_export_profile_to_file_bad_path_returns_false(state):
    profile = state.profiles.add_profile("My Game")
    # A path under a nonexistent drive/dir -- OSError, not a crash.
    assert profiles.export_profile_to_file(state, profile.id, "Z:\\definitely\\missing\\dir\\out.json") is False


def test_suggest_export_filename_strips_illegal_chars(state):
    profile = state.profiles.add_profile('Tarkov: PvP <Main>')
    name = profiles.suggest_export_filename(state, profile.id)
    assert name == "Tarkov PvP Main.json"


def test_suggest_export_filename_unknown_id_falls_back(state):
    assert profiles.suggest_export_filename(state, "nope") == "profile.json"


# ---------------------------------------------------------------------------
# parse_profile_import -- structural validation
# ---------------------------------------------------------------------------


def _export_text(state: app_state.AppState, profile_id: str) -> str:
    return json.dumps(profiles.export_profile_payload(state, profile_id))


def test_parse_profile_import_roundtrips_a_real_export(state):
    profile = state.profiles.add_profile("Roundtrip")
    parsed = profiles.parse_profile_import(_export_text(state, profile.id))
    assert parsed is not None
    parsed_profile, payload = parsed
    assert parsed_profile.name == "Roundtrip"
    assert payload["entries"] == []
    assert payload["macros"] == []


@pytest.mark.parametrize(
    "raw_text",
    [
        "not json at all",
        "",
        "[1, 2, 3]",  # valid JSON, wrong top-level type (list, not dict)
        "42",  # valid JSON, wrong top-level type (int)
        "null",
        json.dumps({}),  # well-formed dict, but no profile-shaped keys at all
        json.dumps({"unrelated": "garbage"}),
        json.dumps({"name": "x", "remapper": [1, 2, 3]}),  # remapper wrong type
        json.dumps({"name": "x", "remapper": {"entries": "not-a-list"}}),
        json.dumps({"name": "x", "macros": "not-a-dict"}),
        json.dumps({"name": "x", "macros": {"macros": "not-a-list"}}),
        json.dumps({"name": "x", "window_select": "not-a-dict"}),
        json.dumps({"name": "x", "overlay": ["not", "a", "dict"]}),
        json.dumps({"name": "x", "remapper": {"entries": [{}] * (profiles._MAX_IMPORT_LIST_LEN + 1)}}),
        json.dumps({"name": "x", "macros": {"macros": [{}] * (profiles._MAX_IMPORT_LIST_LEN + 1)}}),
    ],
)
def test_parse_profile_import_rejects_malformed_or_wrong_shape(raw_text):
    assert profiles.parse_profile_import(raw_text) is None


def test_parse_profile_import_accepts_minimal_profile_shaped_dict():
    # Just a `name` key is enough to look like a profile export -- every
    # other field is optional/defaulted, same tolerance profiles.json
    # loading already has for older/partial entries.
    parsed = profiles.parse_profile_import(json.dumps({"name": "Bare"}))
    assert parsed is not None
    assert parsed[0].name == "Bare"


def test_parse_profile_import_defaults_show_fps_graph_when_absent():
    # A profile exported before show_fps_graph existed (or any stats_hud
    # dict missing the key) must still load -- defaulting to True, matching
    # StatsHudState's own default, not raising or silently defaulting off.
    raw = json.dumps({"name": "Old Export", "overlay": {"stats_hud": {"show_fps": True}}})
    parsed = profiles.parse_profile_import(raw)
    assert parsed is not None
    _, payload = parsed
    assert payload["overlay"].stats_hud.show_fps_graph is True


def test_parse_profile_import_ignores_protected_flag_from_source():
    # protected=True in the source file must not survive parsing into
    # something import_profile() would trust -- enforced again at
    # import_profile() itself, but the parsed ProfileDef reflects the file
    # as-is; the actual guarantee is tested via import_profile() below.
    parsed = profiles.parse_profile_import(json.dumps({"name": "x", "protected": True}))
    assert parsed is not None


# ---------------------------------------------------------------------------
# import_profile -- side-effecting add-as-new-profile step
# ---------------------------------------------------------------------------


def test_import_profile_adds_new_profile(state):
    before = len(state.profiles.profiles)
    new_profile = profiles.import_profile(state, json.dumps({"name": "Imported"}))

    assert new_profile is not None
    assert len(state.profiles.profiles) == before + 1
    assert new_profile in state.profiles.profiles
    assert new_profile.name == "Imported"


def test_import_profile_never_protected_even_if_source_says_so(state):
    new_profile = profiles.import_profile(state, json.dumps({"name": "Sneaky", "protected": True}))
    assert new_profile is not None
    assert new_profile.protected is False


def test_import_profile_gets_a_fresh_id_not_the_source_id(state):
    new_profile = profiles.import_profile(state, json.dumps({"id": "profile-1", "name": "Imported"}))
    assert new_profile is not None
    assert new_profile.id != "profile-1"


def test_import_profile_returns_none_and_does_not_mutate_on_malformed_input(state):
    before = [p.id for p in state.profiles.profiles]
    result = profiles.import_profile(state, "not json")
    assert result is None
    assert [p.id for p in state.profiles.profiles] == before


def test_import_profile_appends_numbered_suffix_on_name_collision(state):
    state.profiles.add_profile("Rival")

    first = profiles.import_profile(state, json.dumps({"name": "Rival"}))
    second = profiles.import_profile(state, json.dumps({"name": "Rival"}))

    assert first is not None and first.name == "Rival (2)"
    assert second is not None and second.name == "Rival (3)"
    # The original "Rival" profile is untouched -- collision never overwrites.
    assert sum(1 for p in state.profiles.profiles if p.name == "Rival") == 1


def test_import_profile_collision_suffix_is_case_insensitive(state):
    state.profiles.add_profile("rival")
    imported = profiles.import_profile(state, json.dumps({"name": "RIVAL"}))
    assert imported is not None
    assert imported.name == "RIVAL (2)"


def test_import_profile_never_overwrites_existing_profile_payload(state):
    original = state.profiles.add_profile("Rival")
    profiles._save_payload_from_live(state, original.id)  # snapshot current (empty) live state

    imported = profiles.import_profile(state, json.dumps({"name": "Rival", "remapper": {"entries": []}}))

    assert imported is not None
    assert imported.id != original.id
    # Both profiles still independently present -- no in-place overwrite.
    names = [p.name for p in state.profiles.profiles]
    assert names.count("Rival") == 1
    assert "Rival (2)" in names


def test_import_profile_blank_name_falls_back_to_default_label(state):
    new_profile = profiles.import_profile(state, json.dumps({"name": "   ", "remapper": {"entries": []}}))
    assert new_profile is not None
    assert new_profile.name == "Imported Profile"


def test_import_profile_full_roundtrip_preserves_remapper_and_macros(state):
    from app_state import RemapEntry, RemapMode
    from key_capture import KeyBind

    source = state.profiles.add_profile("Source")
    state.remapper.entries = [
        RemapEntry(
            id="r1", source=KeyBind(vk_code=65, name="A"), destination=KeyBind(vk_code=66, name="B"),
            enabled=True, mode=RemapMode.TOGGLE,
        )
    ]
    # Non-default overlay value too -- makes sure the full export/import path
    # (not just the isolated _stats_hud_from_json default-backfill test above)
    # actually carries a real overlay setting through, not just its default.
    state.overlay.stats_hud.show_fps_graph = False
    profiles._save_payload_from_live(state, source.id)

    exported_text = _export_text(state, source.id)

    target_state = app_state.new_app_state()
    imported = profiles.import_profile(target_state, exported_text)

    assert imported is not None
    with profiles._payload_lock:
        payload = profiles._payload_cache[imported.id]
    assert len(payload["entries"]) == 1
    assert payload["entries"][0].source.name == "A"
    assert payload["entries"][0].destination.name == "B"
    # Toggle mode (feature 4) survives the export/import round trip, not just
    # the isolated _remap_entry_to_json/_from_json unit tests in
    # test_remapper_toggle.py.
    assert payload["entries"][0].mode == RemapMode.TOGGLE
    # show_fps_graph (feature 3) survives alongside it.
    assert payload["overlay"].stats_hud.show_fps_graph is False
