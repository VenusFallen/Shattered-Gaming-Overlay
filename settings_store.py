"""Save/load global app preferences to settings.json under %LOCALAPPDATA%. Deliberately separate from
profiles.py: `AppState.settings` must stay identical regardless of active profile, so it's never part of
a `ProfileDef` payload. Same disk-I/O shape as profiles.py (best-effort, atomic tmp-file replace).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Tuple

from app_state import AppState

# Same %LOCALAPPDATA% location updater.py/profiles.py already use -- see profiles.py's PROFILES_FILE comment for why not Path(__file__).
SETTINGS_FILE = Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "Shattered Gaming Overlay" / "settings.json"

RGBA = Tuple[float, float, float, float]


def _read_disk() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # corrupt/unreadable -- fall back to defaults rather than crash on startup


def _write_disk(data: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(SETTINGS_FILE)
    except OSError:
        pass  # best-effort persistence -- never crash the app over a save failure


def _color_from_json(raw, fallback: RGBA) -> RGBA:
    try:
        r, g, b, a = raw
        return (float(r), float(g), float(b), float(a))
    except (TypeError, ValueError):
        return fallback


def load(app_state: AppState) -> None:
    """Call once at startup, right after `new_app_state()`. Only overwrites the specific fields `save()` writes."""
    data = _read_disk()
    if not data:
        return
    settings = app_state.settings
    if "theme_name" in data:
        settings.theme_name = str(data["theme_name"])
    if "reduce_motion" in data:
        settings.reduce_motion = bool(data["reduce_motion"])
    if "cycle_color_a" in data:
        settings.cycle_color_a = _color_from_json(data["cycle_color_a"], settings.cycle_color_a)
    if "cycle_color_b" in data:
        settings.cycle_color_b = _color_from_json(data["cycle_color_b"], settings.cycle_color_b)
    if "cycle_period_sec" in data:
        try:
            settings.cycle_period_sec = float(data["cycle_period_sec"])
        except (TypeError, ValueError):
            pass
    if "check_for_updates_on_launch" in data:
        settings.check_for_updates_on_launch = bool(data["check_for_updates_on_launch"])
    if "close_minimizes_to_tray" in data:
        settings.close_minimizes_to_tray = bool(data["close_minimizes_to_tray"])
    if "auto_switch_profiles" in data:
        settings.auto_switch_profiles = bool(data["auto_switch_profiles"])
    if "mouse_polling_rate_hz" in data:
        try:
            settings.mouse_polling_rate_hz = int(data["mouse_polling_rate_hz"])
        except (TypeError, ValueError):
            pass


def save(app_state: AppState) -> None:
    """Write the persist-worthy subset of `SettingsState` to disk. Excludes update-flow runtime fields and `cycle_elapsed_sec` (an animation clock that should always restart at 0)."""
    settings = app_state.settings
    data = {
        "theme_name": settings.theme_name,
        "reduce_motion": settings.reduce_motion,
        "cycle_color_a": list(settings.cycle_color_a),
        "cycle_color_b": list(settings.cycle_color_b),
        "cycle_period_sec": settings.cycle_period_sec,
        "check_for_updates_on_launch": settings.check_for_updates_on_launch,
        "close_minimizes_to_tray": settings.close_minimizes_to_tray,
        "auto_switch_profiles": settings.auto_switch_profiles,
        "mouse_polling_rate_hz": settings.mouse_polling_rate_hz,
    }
    _write_disk(data)
