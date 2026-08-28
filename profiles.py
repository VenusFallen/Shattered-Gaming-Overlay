"""profiles.py -- save/load/delete named profiles (Remapper entries, Macro
defs, Window Select target) to a single JSON file on disk (`profiles.json`,
repo-root, already in .gitignore -- mirrors R9Tools' profiles.py convention,
minus its Interception/recoil/rapidfire-specific fields, which this project
deliberately has none of).

Safety pattern ("always starts disabled, you opt back in")
-------------------------------------------------------------
Mirrors R9Tools' `loadProfile()`, but per-module instead of unconditional:
on `apply_profile()`, every Remapper entry's `enabled` is forced False
unless the profile's `persist_remapper` flag is set, every Macro's
`enabled` is forced False unless `persist_macros` is set, and the saved
Window Select target is dropped back to Global (`selected = None`) unless
`persist_window_select` is set. This is the ONLY place that safety pattern
is applied -- both a fresh-launch `load_all()` and a user clicking Load in
panels/profiles.py go through this same `apply_profile()` call, so there is
exactly one code path that can restore a profile's live state.

Data shape
----------
`AppState.profiles.profiles` (a list of `ProfileDef`) only ever carries
per-profile *metadata* (id/name/protected/persist_* flags) -- it was
designed as "the editable mirror" of what this module owns (see
app_state.py's own docstring). The actual Remapper-entries/Macro-defs/
Window-Select-target *payload* for every profile is owned here, in a
module-level cache keyed by profile id, and is what actually gets
serialized to/from profiles.json. `AppState.remapper` / `AppState.macros` /
`AppState.window_select` only ever hold the CURRENTLY ACTIVE profile's live,
editable state -- `save_profile()` is what snapshots that live state back
into a profile's payload.
"""

from __future__ import annotations

import copy
import itertools
import json
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app_state import (
    AppState,
    MacroDef,
    MacroMode,
    MacroStep,
    MacroStepKind,
    ProcessInfo,
    ProfileDef,
    RemapEntry,
)
from key_capture import KeyBind, UNBOUND

PROFILES_FILE = Path(__file__).resolve().parent / "profiles.json"
DEFAULT_NAME = "Default"

_fallback_counter = itertools.count(1)


def _fallback_id(prefix: str) -> str:
    # Only ever used to backfill a missing/corrupt id read from disk --
    # profiles.json entries written by this module always carry a real id.
    return f"{prefix}-restored-{next(_fallback_counter)}"


# A profile's actual saved payload: Remapper entries, Macro defs, and the
# Window Select target (or None for Global). Kept separate from ProfileDef
# (metadata only) -- see module docstring.
_ProfilePayload = Dict[str, object]

_payload_lock = threading.Lock()
_payload_cache: Dict[str, _ProfilePayload] = {}


# ---------------------------------------------------------------------------
# JSON <-> dataclass conversion helpers
# ---------------------------------------------------------------------------


def _keybind_to_json(kb: KeyBind) -> dict:
    return {"vk_code": kb.vk_code, "name": kb.name}


def _keybind_from_json(d: Optional[dict]) -> KeyBind:
    if not d or d.get("vk_code") is None:
        return UNBOUND
    return KeyBind(vk_code=int(d["vk_code"]), name=str(d.get("name", "Unbound")))


def _remap_entry_to_json(e: RemapEntry) -> dict:
    return {
        "id": e.id,
        "source": _keybind_to_json(e.source),
        "destination": _keybind_to_json(e.destination),
        "enabled": e.enabled,
    }


def _remap_entry_from_json(d: dict) -> RemapEntry:
    return RemapEntry(
        id=str(d.get("id") or _fallback_id("remap")),
        source=_keybind_from_json(d.get("source")),
        destination=_keybind_from_json(d.get("destination")),
        enabled=bool(d.get("enabled", True)),
    )


def _step_to_json(s: MacroStep) -> dict:
    return {
        "id": s.id,
        "kind": s.kind.value,
        "key": _keybind_to_json(s.key),
        "mouse_button": s.mouse_button,
        "scroll_delta": s.scroll_delta,
        "delay_ms": s.delay_ms,
    }


def _enum_from_value(enum_cls, raw, default):
    try:
        return enum_cls(raw)
    except ValueError:
        return default


def _step_from_json(d: dict) -> MacroStep:
    return MacroStep(
        id=str(d.get("id") or _fallback_id("step")),
        kind=_enum_from_value(MacroStepKind, d.get("kind"), MacroStepKind.KEY_TAP),
        key=_keybind_from_json(d.get("key")),
        mouse_button=str(d.get("mouse_button", "Left")),
        scroll_delta=int(d.get("scroll_delta", 120)),
        delay_ms=int(d.get("delay_ms", 50)),
    )


def _macro_to_json(m: MacroDef) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "trigger": _keybind_to_json(m.trigger),
        "mode": m.mode.value,
        "enabled": m.enabled,
        "humanize_jitter_pct": m.humanize_jitter_pct,
        "steps": [_step_to_json(s) for s in m.steps],
    }


def _macro_from_json(d: dict) -> MacroDef:
    return MacroDef(
        id=str(d.get("id") or _fallback_id("macro")),
        name=str(d.get("name", "Macro")),
        trigger=_keybind_from_json(d.get("trigger")),
        mode=_enum_from_value(MacroMode, d.get("mode"), MacroMode.ONCE),
        enabled=bool(d.get("enabled", True)),
        humanize_jitter_pct=int(d.get("humanize_jitter_pct", 15)),
        steps=[_step_from_json(s) for s in d.get("steps", [])],
    )


def _window_select_payload_from_json(d: Optional[dict]) -> Optional[dict]:
    if not d:
        return None
    if d.get("pid") is None:
        return None
    return {
        "pid": int(d["pid"]),
        "exe_name": str(d.get("exe_name", "")),
        "window_title": str(d.get("window_title", "")),
    }


def _profile_from_json(raw: dict) -> Tuple[ProfileDef, _ProfilePayload]:
    profile = ProfileDef(
        id=str(raw.get("id") or _fallback_id("profile")),
        name=str(raw.get("name", "Profile")),
        protected=bool(raw.get("protected", False)),
        persist_remapper=bool(raw.get("persist_remapper", False)),
        persist_macros=bool(raw.get("persist_macros", False)),
        persist_window_select=bool(raw.get("persist_window_select", False)),
    )
    payload: _ProfilePayload = {
        "entries": [_remap_entry_from_json(e) for e in raw.get("remapper", {}).get("entries", [])],
        "macros": [_macro_from_json(m) for m in raw.get("macros", {}).get("macros", [])],
        "window_select": _window_select_payload_from_json(raw.get("window_select")),
    }
    return profile, payload


def _profile_payload_to_json(p: ProfileDef, payload: _ProfilePayload) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "protected": p.protected,
        "persist_remapper": p.persist_remapper,
        "persist_macros": p.persist_macros,
        "persist_window_select": p.persist_window_select,
        "remapper": {"entries": [_remap_entry_to_json(e) for e in payload.get("entries", [])]},  # type: ignore[arg-type]
        "macros": {"macros": [_macro_to_json(m) for m in payload.get("macros", [])]},  # type: ignore[arg-type]
        "window_select": payload.get("window_select"),
    }


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------


def _read_disk() -> dict:
    if not PROFILES_FILE.exists():
        return {"active_id": "", "profiles": []}
    try:
        with PROFILES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"active_id": "", "profiles": []}
        return data
    except (OSError, ValueError):
        # Corrupt/unreadable file -- fall back to empty rather than crash
        # the whole app on startup.
        return {"active_id": "", "profiles": []}


def _write_disk(data: dict) -> None:
    try:
        PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(PROFILES_FILE)
    except OSError:
        pass  # best-effort persistence -- never crash the app over a save failure


def _write_all(app_state: AppState) -> None:
    with _payload_lock:
        payloads = dict(_payload_cache)
    raw_profiles = [
        _profile_payload_to_json(p, payloads.get(p.id, {"entries": [], "macros": [], "window_select": None}))
        for p in app_state.profiles.profiles
    ]
    _write_disk({"active_id": app_state.profiles.active_id, "profiles": raw_profiles})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_all(app_state: AppState) -> None:
    """Call once at startup (see main.py), right after `new_app_state()`.

    If profiles.json doesn't exist yet or has no profiles, leaves
    `app_state.profiles` exactly as `new_app_state()` built it (a single
    protected, empty Default profile) -- there's nothing to load.
    """
    data = _read_disk()
    raw_profiles = data.get("profiles") or []
    if not raw_profiles:
        return

    profiles: List[ProfileDef] = []
    payloads: Dict[str, _ProfilePayload] = {}
    for raw in raw_profiles:
        if not isinstance(raw, dict):
            continue
        profile, payload = _profile_from_json(raw)
        profiles.append(profile)
        payloads[profile.id] = payload

    if not any(p.protected for p in profiles):
        default = ProfileDef(id=_fallback_id("profile"), name=DEFAULT_NAME, protected=True)
        profiles.insert(0, default)
        payloads[default.id] = {"entries": [], "macros": [], "window_select": None}

    app_state.profiles.profiles = profiles
    with _payload_lock:
        _payload_cache.clear()
        _payload_cache.update(payloads)

    active_id = str(data.get("active_id") or "")
    if not any(p.id == active_id for p in profiles):
        default = next((p for p in profiles if p.protected), None)
        active_id = default.id if default is not None else profiles[0].id

    apply_profile(app_state, active_id)


def apply_profile(app_state: AppState, profile_id: str) -> bool:
    """Restore `profile_id`'s saved Remapper/Macros/Window-Select payload
    into the live AppState, applying the persist_* safety pattern. Used both
    by `load_all()` at startup and by panels/profiles.py's Load button --
    exactly one code path restores a profile's state."""
    profile = next((p for p in app_state.profiles.profiles if p.id == profile_id), None)
    if profile is None:
        return False

    with _payload_lock:
        payload = _payload_cache.get(profile_id, {"entries": [], "macros": [], "window_select": None})
        payload = copy.deepcopy(payload)

    entries: List[RemapEntry] = payload.get("entries", [])  # type: ignore[assignment]
    if not profile.persist_remapper:
        for entry in entries:
            entry.enabled = False
    app_state.remapper.entries = entries
    app_state.remapper.capturing_entry_id = None
    app_state.remapper.capturing_field = None

    macros: List[MacroDef] = payload.get("macros", [])  # type: ignore[assignment]
    if not profile.persist_macros:
        for macro in macros:
            macro.enabled = False
    app_state.macros.macros = macros
    app_state.macros.selected_id = macros[0].id if macros else None
    app_state.macros.capturing_macro_id = None
    app_state.macros.capturing_step_id = None

    ws = payload.get("window_select")
    if profile.persist_window_select and ws:
        app_state.window_select.selected = ProcessInfo(
            pid=ws["pid"], exe_name=ws["exe_name"], window_title=ws["window_title"]  # type: ignore[index]
        )
    else:
        app_state.window_select.selected = None
    app_state.window_select.selected_has_focus = False

    app_state.profiles.active_id = profile_id
    _write_all(app_state)
    return True


def _save_payload_from_live(app_state: AppState, profile_id: str) -> None:
    entries = copy.deepcopy(app_state.remapper.entries)
    macros = copy.deepcopy(app_state.macros.macros)
    ws = None
    selected = app_state.window_select.selected
    if selected is not None:
        ws = {"pid": selected.pid, "exe_name": selected.exe_name, "window_title": selected.window_title}
    with _payload_lock:
        _payload_cache[profile_id] = {"entries": entries, "macros": macros, "window_select": ws}


def save_profile(app_state: AppState, profile_id: str) -> bool:
    """Overwrite `profile_id`'s saved payload with a snapshot of the CURRENT
    live AppState.remapper / AppState.macros / AppState.window_select.
    Refuses to overwrite the protected Default profile."""
    profile = next((p for p in app_state.profiles.profiles if p.id == profile_id), None)
    if profile is None or profile.protected:
        return False
    _save_payload_from_live(app_state, profile_id)
    _write_all(app_state)
    return True


def create_profile_from_current(app_state: AppState, name: str) -> Optional[ProfileDef]:
    """Create a new, non-protected profile named `name`, snapshotting the
    current live AppState as its saved payload (a "Save As new profile"
    flow), and make it the active profile. Returns None (no-op) for a
    blank/duplicate name or a name that collides with the protected
    Default profile."""
    name = name.strip()
    if not name or name.lower() == DEFAULT_NAME.lower():
        return None
    if any(p.name.lower() == name.lower() for p in app_state.profiles.profiles):
        return None

    profile = ProfileDef(id=_fallback_id("profile"), name=name, protected=False)
    app_state.profiles.profiles.append(profile)
    _save_payload_from_live(app_state, profile.id)
    app_state.profiles.active_id = profile.id
    _write_all(app_state)
    return profile


def sync_metadata(app_state: AppState) -> None:
    """Persist `app_state.profiles.profiles`' current metadata (name,
    persist_* flags) to disk without touching any profile's saved
    Remapper/Macros/Window-Select payload. Call after editing a
    `ProfileDef`'s persist_* toggles in the panel so that preference isn't
    lost if the app closes before the next Save/Load/Create/Delete."""
    _write_all(app_state)


def delete_profile(app_state: AppState, profile_id: str) -> bool:
    """Delete a non-protected profile. Refuses (returns False) for the
    protected Default profile or an unknown id."""
    target = next((p for p in app_state.profiles.profiles if p.id == profile_id), None)
    if target is None or target.protected:
        return False
    app_state.profiles.remove_profile(profile_id)  # already guards protected + reassigns active_id
    with _payload_lock:
        _payload_cache.pop(profile_id, None)
    _write_all(app_state)
    return True
