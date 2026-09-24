"""Coverage for soundboard_store.py's save/load round trip. Monkeypatches SOUNDBOARD_FILE per-test so _write_disk() never touches the real %LOCALAPPDATA%/soundboard.json."""

from __future__ import annotations

import pytest

import app_state
import soundboard_store
from key_capture import KeyBind, UNBOUND

KEY = KeyBind(vk_code=0x41, name="A")


@pytest.fixture(autouse=True)
def _isolate_soundboard_file(tmp_path, monkeypatch):
    monkeypatch.setattr(soundboard_store, "SOUNDBOARD_FILE", tmp_path / "soundboard.json")
    yield


@pytest.fixture
def state() -> app_state.AppState:
    return app_state.new_app_state()


def test_load_with_no_file_leaves_defaults(state):
    soundboard_store.load(state)

    assert state.soundboard.clips == []
    assert state.soundboard.output_device_name == ""


def test_save_then_load_round_trips_a_clip(state):
    clip = state.soundboard.add_clip()
    clip.name = "Air Horn"
    clip.file_path = "C:\\Sounds\\airhorn.wav"
    clip.hotkey = KEY
    clip.volume = 0.75
    clip.enabled = False
    state.soundboard.output_device_name = "CABLE Input (VB-Audio Virtual Cable)"

    soundboard_store.save(state)

    fresh = app_state.new_app_state()
    soundboard_store.load(fresh)

    assert len(fresh.soundboard.clips) == 1
    loaded = fresh.soundboard.clips[0]
    assert loaded.id == clip.id
    assert loaded.name == "Air Horn"
    assert loaded.file_path == "C:\\Sounds\\airhorn.wav"
    assert loaded.hotkey == KEY
    assert loaded.volume == 0.75
    assert loaded.enabled is False
    assert fresh.soundboard.output_device_name == "CABLE Input (VB-Audio Virtual Cable)"


def test_load_defaults_missing_hotkey_to_unbound(tmp_path):
    soundboard_store.SOUNDBOARD_FILE.write_text(
        '{"clips": [{"id": "c1", "name": "X", "file_path": "a.wav"}], "output_device_name": ""}',
        encoding="utf-8",
    )
    fresh = app_state.new_app_state()

    soundboard_store.load(fresh)

    assert fresh.soundboard.clips[0].hotkey == UNBOUND
    assert fresh.soundboard.clips[0].volume == 1.0
    assert fresh.soundboard.clips[0].enabled is True


def test_load_generates_id_for_clip_missing_one(tmp_path):
    soundboard_store.SOUNDBOARD_FILE.write_text(
        '{"clips": [{"name": "X", "file_path": "a.wav"}]}',
        encoding="utf-8",
    )
    fresh = app_state.new_app_state()

    soundboard_store.load(fresh)

    assert fresh.soundboard.clips[0].id  # non-empty, generated
    assert fresh.soundboard.clips[0].id.startswith("sound-restored-")


def test_load_survives_corrupt_json(tmp_path):
    soundboard_store.SOUNDBOARD_FILE.write_text("{not valid json", encoding="utf-8")
    fresh = app_state.new_app_state()

    soundboard_store.load(fresh)  # must not raise

    assert fresh.soundboard.clips == []


def test_load_skips_non_dict_entries_in_clips_list(tmp_path):
    soundboard_store.SOUNDBOARD_FILE.write_text(
        '{"clips": ["not-a-dict", 123, {"id": "c1", "name": "Real", "file_path": "a.wav"}]}',
        encoding="utf-8",
    )
    fresh = app_state.new_app_state()

    soundboard_store.load(fresh)

    assert len(fresh.soundboard.clips) == 1
    assert fresh.soundboard.clips[0].name == "Real"


def test_load_skips_a_clip_with_a_malformed_nested_field_instead_of_crashing(tmp_path):
    # hotkey as a bare string (not a dict) must not crash startup for every
    # other clip -- same tolerance as profiles.py's own loader.
    soundboard_store.SOUNDBOARD_FILE.write_text(
        '{"clips": [{"id": "c1", "name": "Bad", "file_path": "a.wav", "hotkey": "garbage"}, '
        '{"id": "c2", "name": "Good", "file_path": "b.wav"}]}',
        encoding="utf-8",
    )
    fresh = app_state.new_app_state()

    soundboard_store.load(fresh)  # must not raise

    names = [c.name for c in fresh.soundboard.clips]
    assert "Bad" not in names
    assert "Good" in names


def test_save_with_no_clips_writes_empty_list(state):
    soundboard_store.save(state)

    fresh = app_state.new_app_state()
    soundboard_store.load(fresh)

    assert fresh.soundboard.clips == []
