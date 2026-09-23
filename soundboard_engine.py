"""Plays an imported audio file on its bound hotkey. Subscribes to `remapper.remapper_engine`'s post-remap
effective-event stream (same pattern as `macro_engine.py`) rather than installing its own hook, so a remapped
key correctly arms a bound clip and this module goes inert alongside the remapper's window-filter gate for
free. Fires on physical press, not release (unlike Macro's Once trigger) so a pad responds instantly.
"""

from __future__ import annotations

import os
import threading
import traceback
from dataclasses import dataclass
from typing import List, Optional, Set

import numpy as np
import sounddevice as sd
import soundfile as sf

import audio_devices

try:  # pragma: no cover - only used for type hints
    from app_state import SoundboardState, SoundClip
except Exception:  # pragma: no cover
    SoundboardState = object  # type: ignore
    SoundClip = object  # type: ignore

from remapper import EffectiveInputEvent


def clip_is_missing(file_path: str) -> bool:
    """True if `file_path` is blank or doesn't currently exist on disk -- clips are never copied into app storage, only their location is recorded, so a path can go stale."""
    if not file_path:
        return True
    return not os.path.isfile(file_path)


def resolve_output_device_index(name: str) -> Optional[int]:
    """Re-resolves a saved output device NAME to sounddevice's current index (indices aren't stable across processes, so a name is what's persisted). Returns None (system default) if blank or no longer present."""
    if not name:
        return None
    for device in audio_devices.list_output_devices():
        if device.name == name:
            return device.index
    return None


@dataclass(frozen=True)
class _ClipSnap:
    id: str
    trigger_vk: Optional[int]
    file_path: str
    volume: float
    enabled: bool


class SoundboardEngine:
    """start()/stop()/update_snapshot()/handle_effective_event() are thread-safe from the Companion thread; handle_effective_event is registered as a remapper.py listener."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clips: List[_ClipSnap] = []
        self._output_device_name: str = ""
        self._active_streams: Set[sd.OutputStream] = set()
        self._started = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        """Immediately silences any clips currently playing via `abort()`, not `stop()`, so shutdown cuts audio off instead of letting an in-flight clip finish."""
        self._started = False
        with self._lock:
            streams = list(self._active_streams)
            self._active_streams.clear()
        for stream in streams:
            try:
                stream.abort(ignore_errors=True)
                stream.close(ignore_errors=True)
            except Exception:
                traceback.print_exc()

    def update_snapshot(self, soundboard_state: "SoundboardState") -> None:
        """Call once per Companion-window frame."""
        snaps = [
            _ClipSnap(
                id=clip.id,
                trigger_vk=clip.hotkey.vk_code,
                file_path=clip.file_path,
                volume=clip.volume,
                enabled=clip.enabled,
            )
            for clip in soundboard_state.clips
        ]
        with self._lock:
            self._clips = snaps
            self._output_device_name = soundboard_state.output_device_name

    # ------------------------------------------------------------------
    # Trigger detection -- called from remapper.py's hook thread
    # ------------------------------------------------------------------

    def handle_effective_event(self, event: EffectiveInputEvent) -> None:
        if not self._started or event.vk_code is None or event.up:
            return  # fires on press only -- see module docstring
        with self._lock:
            clips = self._clips
        for clip in clips:
            if not clip.enabled or clip.trigger_vk is None:
                continue
            if clip.trigger_vk != event.vk_code:
                continue
            self._trigger(clip)
            break  # first enabled clip bound to this key wins, same convention as macro_engine.py

    def _trigger(self, clip: _ClipSnap) -> None:
        if clip_is_missing(clip.file_path):
            return  # broken/missing path -- surfaced via the UI's red exclamation, not a crash here
        threading.Thread(target=self._play, args=(clip,), daemon=True, name=f"SGO-Sound-{clip.id}").start()

    def preview(self, clip: "SoundClip") -> None:
        """Play `clip` immediately, bypassing hotkey matching -- used by the Soundboard panel's Preview button."""
        snap = _ClipSnap(id=clip.id, trigger_vk=None, file_path=clip.file_path, volume=clip.volume, enabled=True)
        self._trigger(snap)

    # ------------------------------------------------------------------
    # Playback (dedicated background thread per trigger -- never the hook thread)
    # ------------------------------------------------------------------

    def _play(self, clip: _ClipSnap) -> None:
        # Opens its own OutputStream rather than using sd.play()/stop(), whose single-current-stream
        # model would cut off an already-playing clip when a second one fires; independent streams
        # against the same WASAPI shared-mode device mix automatically at the OS level.
        try:
            data, samplerate = sf.read(clip.file_path, dtype="float32", always_2d=True)
        except Exception:
            traceback.print_exc()
            return

        data = np.clip(data * max(0.0, clip.volume), -1.0, 1.0)

        with self._lock:
            output_name = self._output_device_name
        device_index = resolve_output_device_index(output_name)

        try:
            stream = sd.OutputStream(device=device_index, samplerate=samplerate, channels=data.shape[1], dtype="float32")
        except Exception:
            traceback.print_exc()
            return

        with self._lock:
            self._active_streams.add(stream)
        try:
            with stream:
                stream.write(data)
        except Exception:
            traceback.print_exc()
        finally:
            with self._lock:
                self._active_streams.discard(stream)


# Process-wide singleton -- main.py starts/stops this alongside the Companion window's lifecycle
# and wires it to remapper_engine's effective-event stream.
soundboard_engine = SoundboardEngine()
