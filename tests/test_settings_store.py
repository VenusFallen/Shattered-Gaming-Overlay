"""Coverage for settings_store.py's mouse_polling_rate_hz round-trip (the rest of the module has no
dedicated test coverage yet -- this only covers what this session added).
"""

from __future__ import annotations

import pytest

import settings_store
from app_state import AppState


@pytest.fixture(autouse=True)
def _isolate_settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "SETTINGS_FILE", tmp_path / "settings.json")


def test_mouse_polling_rate_hz_round_trips_through_save_and_load():
    saved = AppState()
    saved.settings.mouse_polling_rate_hz = 500
    settings_store.save(saved)

    loaded = AppState()
    settings_store.load(loaded)

    assert loaded.settings.mouse_polling_rate_hz == 500


def test_mouse_polling_rate_hz_defaults_when_missing_from_disk():
    settings_store.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings_store.SETTINGS_FILE.write_text("{}", encoding="utf-8")

    loaded = AppState()
    settings_store.load(loaded)

    assert loaded.settings.mouse_polling_rate_hz == 1000
