"""WASAPI device enumeration for the Soundboard feature. Pure enumeration only -- no playback, no streaming,
no state. Every physical/virtual device shows up once per host API (MME, DirectSound, WASAPI, WDM-KS) in
sounddevice's raw list, so this filters to WASAPI specifically (modern, shared-mode by default). Device
indices are only stable for the current process's lifetime -- callers should resolve by name, not persist
a raw index (see soundboard_store.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import sounddevice as sd

_WASAPI_HOSTAPI_NAME = "Windows WASAPI"


@dataclass(frozen=True)
class AudioDevice:
    """One WASAPI-visible device. `index` is sounddevice's own device index for THIS process's lifetime only."""

    index: int
    name: str


def _wasapi_hostapi_index() -> Optional[int]:
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        return None
    for i, api in enumerate(hostapis):
        if api.get("name") == _WASAPI_HOSTAPI_NAME:
            return i
    return None


def list_output_devices() -> List[AudioDevice]:
    """WASAPI playback devices for the Soundboard's output target. Returns [] (never raises) if WASAPI is unavailable or enumeration fails."""
    hostapi = _wasapi_hostapi_index()
    if hostapi is None:
        return []
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    return [
        AudioDevice(index=i, name=d["name"])
        for i, d in enumerate(devices)
        if d.get("hostapi") == hostapi and d.get("max_output_channels", 0) > 0
    ]
