"""Coverage for soundboard_engine.py's playback engine. Never makes a real sounddevice/soundfile call -- sf.read and sd.OutputStream are monkeypatched, and tmp_path dummy files stand in for "exists" checks only.
"""

from __future__ import annotations

import threading
import time
from typing import List
from unittest.mock import MagicMock

import numpy as np
import pytest

import audio_devices
import soundfile as sf
import soundboard_engine as sb
from app_state import KeyBind, SoundboardState
from soundboard_engine import SoundboardEngine, _ClipSnap, clip_is_missing, resolve_output_device_index

KEY = KeyBind(vk_code=0x41, name="A")
KEY2 = KeyBind(vk_code=0x42, name="B")


@pytest.fixture(autouse=True)
def _no_real_devices(monkeypatch):
    # Empty by default -- resolve_output_device_index() should fall back to
    # None (system default) unless a test explicitly provides a device list.
    monkeypatch.setattr(audio_devices, "list_output_devices", lambda: [])
    yield


class _FakeStream:
    """Stand-in for sounddevice.OutputStream -- records what it was asked
    to do instead of touching a real device."""

    instances: List["_FakeStream"] = []

    def __init__(self, device=None, samplerate=None, channels=None, dtype=None):
        self.device = device
        self.samplerate = samplerate
        self.channels = channels
        self.dtype = dtype
        self.written = []
        self.entered = False
        self.exited = False
        self.aborted = False
        self.closed = False
        _FakeStream.instances.append(self)

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def write(self, data):
        self.written.append(data)
        return False

    def abort(self, ignore_errors=False):
        self.aborted = True

    def close(self, ignore_errors=False):
        self.closed = True


@pytest.fixture(autouse=True)
def _stub_stream(monkeypatch):
    _FakeStream.instances = []
    monkeypatch.setattr(sb.sd, "OutputStream", _FakeStream)
    yield


def _clip_snap(**overrides) -> _ClipSnap:
    defaults = dict(id="c1", trigger_vk=KEY.vk_code, file_path="C:\\fake\\clip.wav", volume=1.0, enabled=True)
    defaults.update(overrides)
    return _ClipSnap(**defaults)


def _sync(engine: SoundboardEngine, clips=None, output_device_name: str = "") -> None:
    state = SoundboardState()
    if clips:
        state.clips = clips
    state.output_device_name = output_device_name
    engine.update_snapshot(state)


def _press(engine: SoundboardEngine, vk: int) -> None:
    from remapper import EffectiveInputEvent

    engine.handle_effective_event(EffectiveInputEvent(vk_code=vk, up=False))


def _release(engine: SoundboardEngine, vk: int) -> None:
    from remapper import EffectiveInputEvent

    engine.handle_effective_event(EffectiveInputEvent(vk_code=vk, up=True))


# ---------------------------------------------------------------------------
# clip_is_missing
# ---------------------------------------------------------------------------


def test_clip_is_missing_blank_path():
    assert clip_is_missing("") is True


def test_clip_is_missing_nonexistent_path():
    assert clip_is_missing("C:\\definitely\\not\\a\\real\\file.wav") is True


def test_clip_is_missing_real_file(tmp_path):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"not real audio, just needs to exist")
    assert clip_is_missing(str(f)) is False


# ---------------------------------------------------------------------------
# resolve_output_device_index
# ---------------------------------------------------------------------------


def test_resolve_output_device_blank_name_returns_none():
    assert resolve_output_device_index("") is None


def test_resolve_output_device_no_match_returns_none(monkeypatch):
    monkeypatch.setattr(audio_devices, "list_output_devices", lambda: [audio_devices.AudioDevice(index=0, name="Speakers")])
    assert resolve_output_device_index("Headphones") is None


def test_resolve_output_device_matches_by_name(monkeypatch):
    monkeypatch.setattr(
        audio_devices,
        "list_output_devices",
        lambda: [audio_devices.AudioDevice(index=0, name="Speakers"), audio_devices.AudioDevice(index=3, name="CABLE Input")],
    )
    assert resolve_output_device_index("CABLE Input") == 3


# ---------------------------------------------------------------------------
# handle_effective_event -- trigger detection
# ---------------------------------------------------------------------------


def test_fires_only_on_press_not_release(tmp_path, monkeypatch):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"x")
    engine = SoundboardEngine()
    engine.start()
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (np.zeros((10, 1), dtype="float32"), 44100))
    _sync(engine, clips=[_soundclip(f)])

    _release(engine, KEY.vk_code)
    time.sleep(0.05)
    assert len(_FakeStream.instances) == 0

    _press(engine, KEY.vk_code)
    _wait_for(lambda: len(_FakeStream.instances) == 1)
    assert len(_FakeStream.instances) == 1


def test_disabled_clip_does_not_fire(tmp_path):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"x")
    engine = SoundboardEngine()
    engine.start()
    _sync(engine, clips=[_soundclip(f, enabled=False)])

    _press(engine, KEY.vk_code)
    time.sleep(0.05)
    assert len(_FakeStream.instances) == 0


def test_unbound_clip_never_matches(tmp_path):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"x")
    engine = SoundboardEngine()
    engine.start()
    from key_capture import UNBOUND

    clip = _soundclip(f)
    clip.hotkey = UNBOUND
    _sync(engine, clips=[clip])

    _press(engine, KEY.vk_code)
    time.sleep(0.05)
    assert len(_FakeStream.instances) == 0


def test_first_enabled_clip_bound_to_key_wins(tmp_path, monkeypatch):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"x")
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (np.zeros((10, 1), dtype="float32"), 44100))
    engine = SoundboardEngine()
    engine.start()
    clip_a = _soundclip(f, id_="a")
    clip_b = _soundclip(f, id_="b")
    _sync(engine, clips=[clip_a, clip_b])

    _press(engine, KEY.vk_code)
    _wait_for(lambda: len(_FakeStream.instances) >= 1)
    time.sleep(0.05)  # let anything else that might fire also happen
    assert len(_FakeStream.instances) == 1  # not two


def test_missing_file_does_not_attempt_playback(monkeypatch):
    read_mock = MagicMock()
    monkeypatch.setattr(sb.sf, "read", read_mock)
    engine = SoundboardEngine()
    engine.start()
    clip = _soundclip_missing()
    _sync(engine, clips=[clip])

    _press(engine, KEY.vk_code)
    time.sleep(0.05)
    read_mock.assert_not_called()
    assert len(_FakeStream.instances) == 0


def test_not_started_ignores_events(tmp_path):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"x")
    engine = SoundboardEngine()
    # engine.start() never called
    _sync(engine, clips=[_soundclip(f)])

    _press(engine, KEY.vk_code)
    time.sleep(0.05)
    assert len(_FakeStream.instances) == 0


# ---------------------------------------------------------------------------
# preview() -- the panel's Preview button, bypasses hotkey matching
# ---------------------------------------------------------------------------


def test_preview_plays_regardless_of_hotkey_binding(tmp_path, monkeypatch):
    f = tmp_path / "clip.wav"
    f.write_bytes(b"x")
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (np.zeros((10, 1), dtype="float32"), 44100))
    from key_capture import UNBOUND

    engine = SoundboardEngine()
    clip = _soundclip(f)
    clip.hotkey = UNBOUND  # no hotkey at all -- preview must still work

    engine.preview(clip)
    _wait_for(lambda: len(_FakeStream.instances) == 1)

    assert len(_FakeStream.instances) == 1


def test_preview_of_missing_file_does_not_attempt_playback(monkeypatch):
    read_mock = MagicMock()
    monkeypatch.setattr(sb.sf, "read", read_mock)
    engine = SoundboardEngine()
    clip = _soundclip_missing()

    engine.preview(clip)
    time.sleep(0.05)

    read_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _play -- the actual playback call
# ---------------------------------------------------------------------------


def test_play_opens_stream_with_correct_params_and_writes_data(monkeypatch):
    data = np.ones((100, 2), dtype="float32") * 0.5
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (data, 48000))
    engine = SoundboardEngine()
    clip = _clip_snap(volume=1.0)

    engine._play(clip)

    assert len(_FakeStream.instances) == 1
    stream = _FakeStream.instances[0]
    assert stream.samplerate == 48000
    assert stream.channels == 2
    assert stream.entered and stream.exited
    assert len(stream.written) == 1
    assert stream.written[0].shape == (100, 2)


def test_play_applies_volume_scaling(monkeypatch):
    data = np.ones((10, 1), dtype="float32")
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (data, 44100))
    engine = SoundboardEngine()
    clip = _clip_snap(volume=0.5)

    engine._play(clip)

    written = _FakeStream.instances[0].written[0]
    assert np.allclose(written, 0.5)


def test_play_clamps_volume_above_one(monkeypatch):
    data = np.ones((10, 1), dtype="float32")
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (data, 44100))
    engine = SoundboardEngine()
    clip = _clip_snap(volume=3.0)  # would clip well beyond [-1, 1] if not clamped

    engine._play(clip)

    written = _FakeStream.instances[0].written[0]
    assert np.all(written <= 1.0)


def test_play_removes_stream_from_active_set_after_finishing(monkeypatch):
    data = np.zeros((10, 1), dtype="float32")
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (data, 44100))
    engine = SoundboardEngine()
    clip = _clip_snap()

    engine._play(clip)

    assert engine._active_streams == set()


def test_play_survives_decode_failure(monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("bad file")

    monkeypatch.setattr(sb.sf, "read", _raise)
    engine = SoundboardEngine()
    clip = _clip_snap()

    engine._play(clip)  # must not raise

    assert len(_FakeStream.instances) == 0


def test_play_uses_resolved_output_device(monkeypatch):
    monkeypatch.setattr(
        audio_devices, "list_output_devices", lambda: [audio_devices.AudioDevice(index=7, name="CABLE Input")]
    )
    data = np.zeros((10, 1), dtype="float32")
    monkeypatch.setattr(sb.sf, "read", lambda *a, **k: (data, 44100))
    engine = SoundboardEngine()
    engine._output_device_name = "CABLE Input"
    clip = _clip_snap()

    engine._play(clip)

    assert _FakeStream.instances[0].device == 7


# ---------------------------------------------------------------------------
# stop() -- immediate cutoff
# ---------------------------------------------------------------------------


def test_stop_aborts_and_closes_active_streams():
    engine = SoundboardEngine()
    engine.start()
    fake = _FakeStream()
    engine._active_streams.add(fake)

    engine.stop()

    assert fake.aborted is True
    assert fake.closed is True
    assert engine._active_streams == set()


def test_stop_with_no_active_streams_is_a_noop():
    engine = SoundboardEngine()
    engine.start()

    engine.stop()  # must not raise

    assert engine._active_streams == set()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _soundclip(path, id_: str = "c1", enabled: bool = True):
    from app_state import SoundClip

    return SoundClip(id=id_, file_path=str(path), hotkey=KEY, enabled=enabled)


def _soundclip_missing(id_: str = "c1"):
    from app_state import SoundClip

    return SoundClip(id=id_, file_path="C:\\nope\\gone.wav", hotkey=KEY, enabled=True)


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "condition never became true within timeout"
