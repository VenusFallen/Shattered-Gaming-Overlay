"""settings_store.py -- save/load global app preferences to settings.json
under %LOCALAPPDATA% (see SETTINGS_FILE's own comment for why).

Deliberately separate from profiles.py: `AppState.settings` (theme, reduce
motion, Color Cycle config, update-check opt-in, titlebar close behavior)
must stay identical regardless of active profile, so it's never part of a
`ProfileDef`'s payload and profiles.py never touches this file.

Same disk-I/O shape as profiles.py (`_read_disk`/`_write_disk`: best-effort,
atomic tmp-file replace, never raises into the caller) -- not shared code
since the two files serialize different data, but kept identical in spirit.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Tuple

from app_state import AppState

# NOT Path(__file__).resolve().parent -- see profiles.py's PROFILES_FILE
# comment. Same %LOCALAPPDATA% location updater.py's _log_dir() and
# profiles.py's PROFILES_FILE already use.
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
        # Corrupt/unreadable file -- fall back to defaults rather than
        # crash the whole app on startup, same reasoning as profiles.py.
        return {}


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
    """Call once at startup, right after `new_app_state()`. Only overwrites
    the specific fields `save()` writes -- every other `SettingsState` field
    stays whatever `new_app_state()` defaulted it to."""
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


def save(app_state: AppState) -> None:
    """Write the persist-worthy subset of `SettingsState` to disk.
    Deliberately excludes the update-flow runtime fields (per-session state
    owned by `updater.update_manager`) and `cycle_elapsed_sec` (an animation
    clock that should always restart at 0, not resume after a relaunch)."""
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
    }
    _write_disk(data)
