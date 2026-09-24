"""Save/load the Soundboard feature's config (clips, shared output device) to soundboard.json under
%LOCALAPPDATA%. Deliberately its own file, not folded into settings.json or profiles.json: it's global
like SettingsState (profiles.py never touches it) but a structurally different concern. Same disk-I/O
shape as profiles.py/settings_store.py (best-effort, atomic tmp-file replace, never raises).
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from app_state import AppState, SoundClip
from key_capture import KeyBind, UNBOUND

# Same %LOCALAPPDATA% location profiles.py/settings_store.py already use.
SOUNDBOARD_FILE = Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "Shattered Gaming Overlay" / "soundboard.json"


def _next_id() -> str:
    # uuid4-based, same as app_state.py/profiles.py -- a restart-resetting counter caused a real id-collision bug elsewhere; don't repeat it.
    return f"sound-restored-{uuid.uuid4().hex[:8]}"


def _keybind_to_json(kb: KeyBind) -> dict:
    return {"vk_code": kb.vk_code, "name": kb.name}


def _keybind_from_json(d: Optional[dict]) -> KeyBind:
    if not d or d.get("vk_code") is None:
        return UNBOUND
    return KeyBind(vk_code=int(d["vk_code"]), name=str(d.get("name", "Unbound")))


def _clip_to_json(clip: SoundClip) -> dict:
    return {
        "id": clip.id,
        "name": clip.name,
        "file_path": clip.file_path,
        "hotkey": _keybind_to_json(clip.hotkey),
        "volume": clip.volume,
        "enabled": clip.enabled,
    }


def _clip_from_json(d: dict) -> SoundClip:
    return SoundClip(
        id=str(d.get("id") or _next_id()),
        name=str(d.get("name", "New Sound")),
        file_path=str(d.get("file_path", "")),
        hotkey=_keybind_from_json(d.get("hotkey")),
        volume=float(d.get("volume", 1.0)),
        enabled=bool(d.get("enabled", True)),
    )


def _read_disk() -> dict:
    if not SOUNDBOARD_FILE.exists():
        return {}
    try:
        with SOUNDBOARD_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # corrupt/unreadable -- fall back to defaults rather than crash on startup


def _write_disk(data: dict) -> None:
    try:
        SOUNDBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SOUNDBOARD_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(SOUNDBOARD_FILE)
    except OSError:
        pass  # best-effort persistence -- never crash the app over a save failure


def _safe_clip_from_json(c: dict) -> Optional[SoundClip]:
    try:
        return _clip_from_json(c)
    except (TypeError, ValueError, AttributeError, KeyError, IndexError):
        # A malformed nested field (e.g. hotkey not a dict) must drop just this
        # one clip, not crash startup -- same tolerance profiles.py's loader has.
        return None


def load(app_state: AppState) -> None:
    """Call once at startup, right after `new_app_state()`. Leaves `app_state.soundboard` at its default (empty) if soundboard.json doesn't exist yet."""
    data = _read_disk()
    if not data:
        return
    soundboard = app_state.soundboard
    clips = [_safe_clip_from_json(c) for c in data.get("clips", []) if isinstance(c, dict)]
    soundboard.clips = [c for c in clips if c is not None]
    soundboard.output_device_name = str(data.get("output_device_name", ""))


def save(app_state: AppState) -> None:
    """Write the current SoundboardState to disk. Call after any edit that should survive a restart -- not a periodic autosave."""
    soundboard = app_state.soundboard
    data = {
        "clips": [_clip_to_json(c) for c in soundboard.clips],
        "output_device_name": soundboard.output_device_name,
    }
    _write_disk(data)
