"""Coverage for audio_devices.py's WASAPI filtering. Never makes a real sounddevice/PortAudio call --
sd.query_hostapis()/sd.query_devices() are monkeypatched with a fixture modeling the real device-list
shape: every device repeated once per host API, only WASAPI entries meant to survive filtering.
"""

from __future__ import annotations

import pytest

import sounddevice as sd
from audio_devices import AudioDevice, list_output_devices

_HOSTAPIS = [
    {"name": "MME"},
    {"name": "Windows DirectSound"},
    {"name": "Windows WASAPI"},
    {"name": "Windows WDM-KS"},
]

# Index 2 is WASAPI, matching _HOSTAPIS above -- a mic and two playback devices, each appearing under
# every host API, only the WASAPI (hostapi=2) copies should survive filtering.
_DEVICES = [
    # MME duplicates (hostapi=0) -- must be filtered out
    {"name": "Microphone (Realtek)", "hostapi": 0, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers (Realtek)", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    # DirectSound duplicates (hostapi=1) -- must be filtered out
    {"name": "Microphone (Realtek)", "hostapi": 1, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers (Realtek)", "hostapi": 1, "max_input_channels": 0, "max_output_channels": 2},
    # WASAPI (hostapi=2) -- these are the only ones that should survive
    {"name": "Microphone (Realtek)", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Stereo Mix (Realtek)", "hostapi": 2, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers (Realtek)", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2},
    {"name": "Headphones (Realtek)", "hostapi": 2, "max_input_channels": 0, "max_output_channels": 2},
    # WDM-KS duplicates (hostapi=3) -- must be filtered out
    {"name": "Microphone (Realtek)", "hostapi": 3, "max_input_channels": 2, "max_output_channels": 0},
    {"name": "Speakers (Realtek)", "hostapi": 3, "max_input_channels": 0, "max_output_channels": 2},
]


@pytest.fixture(autouse=True)
def _stub_sounddevice(monkeypatch):
    monkeypatch.setattr(sd, "query_hostapis", lambda: _HOSTAPIS)
    monkeypatch.setattr(sd, "query_devices", lambda: _DEVICES)
    yield


def test_list_output_devices_only_returns_wasapi_playback_devices():
    result = list_output_devices()

    names = [d.name for d in result]
    assert names == ["Speakers (Realtek)", "Headphones (Realtek)"]
    assert all(isinstance(d, AudioDevice) for d in result)


def test_list_output_devices_index_points_at_the_real_device_list_position():
    result = list_output_devices()

    # "Speakers (Realtek)" WASAPI copy is at raw index 6 in _DEVICES.
    speakers = next(d for d in result if d.name == "Speakers (Realtek)")
    assert speakers.index == 6


def test_no_wasapi_hostapi_returns_empty_list(monkeypatch):
    monkeypatch.setattr(sd, "query_hostapis", lambda: [{"name": "MME"}])

    assert list_output_devices() == []


def test_query_devices_failure_returns_empty_list_not_raise(monkeypatch):
    def _raise():
        raise RuntimeError("PortAudio not initialized")

    monkeypatch.setattr(sd, "query_devices", _raise)

    assert list_output_devices() == []


def test_query_hostapis_failure_returns_empty_list_not_raise(monkeypatch):
    def _raise():
        raise RuntimeError("PortAudio not initialized")

    monkeypatch.setattr(sd, "query_hostapis", _raise)

    assert list_output_devices() == []
