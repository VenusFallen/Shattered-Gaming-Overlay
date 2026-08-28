"""settings_store.py -- save/load global app preferences to a single JSON
file on disk (`settings.json`, repo-root, gitignored -- same convention as
profiles.py's `profiles.json`).

Deliberately separate from profiles.py: `AppState.settings` is app-wide
preference (theme, reduce motion, the Color Cycle config, update-check
opt-in, and the titlebar close-button behavior) that must stay identical no
matter which profile is active or how many self-updates have run -- it is
never part of a `ProfileDef`'s saved payload, and profiles.py never touches
this file. See app_state.SettingsState's own field comments for why each
field below is or isn't included here.

Same disk-I/O shape as profiles.py (`_read_disk`/`_write_disk`: best-effort,
atomic tmp-file replace, never raises into the caller) -- not reused
directly since the two files serialize entirely different data, but kept
identical in spirit on purpose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from app_state import AppState

SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"

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
    """Call once at startup (see main.py), right after `new_app_state()`.

    Only ever overwrites the specific fields `save()` below writes -- every
    other `SettingsState` field (the runtime update-flow fields, the Color
    Cycle animation clock) stays whatever `new_app_state()` defaulted it to,
    same "load only what we saved" contract as profiles.py's `load_all()`.
    """
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


def save(app_state: AppState) -> None:
    """Write the persist-worthy subset of `SettingsState` to disk.

    Deliberately narrow -- `update_status`/`update_latest_version`/
    `update_download_pct`/`update_error_message`/`last_checked_display`/
    `auto_update_prompt_pending` are per-session runtime state written by
    `updater.update_manager`, never a user preference, and
    `cycle_elapsed_sec` is an animation clock that should always restart at
    0 on launch, not resume mid-cycle after a relaunch. None of those belong
    in a preferences file.
    """
    settings = app_state.settings
    data = {
        "theme_name": settings.theme_name,
        "reduce_motion": settings.reduce_motion,
        "cycle_color_a": list(settings.cycle_color_a),
        "cycle_color_b": list(settings.cycle_color_b),
        "cycle_period_sec": settings.cycle_period_sec,
        "check_for_updates_on_launch": settings.check_for_updates_on_launch,
        "close_minimizes_to_tray": settings.close_minimizes_to_tray,
    }
    _write_disk(data)
