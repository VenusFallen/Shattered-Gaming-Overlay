"""app_state.py -- in-memory state shape for the Companion window.

No backend engine exists yet (engine-agent's remapper.py / macro_engine.py /
profiles.py / window_select.py are future work), so every panel below owns
its own local state for now. The shapes here are deliberately close to what
engine-agent's modules will eventually need, so wiring a real engine in
later is a small "swap the in-memory list for a call into profiles.py" change,
not a redesign:

  - RemapperState.entries          ~= what remapper.py will load/save per profile
  - MacrosState.macros             ~= what macro_engine.py will record/play back
  - ProfilesState.profiles         ~= what profiles.py will persist to disk
  - WindowSelectState              ~= what window_select.py will enumerate/track
  - OverlayState                   ~= what the future HUD overlay will read to
                                      decide what to render (ui-agent's own
                                      future work, not engine-agent's)

Nothing here talks to input_hooks.py/input_inject.py directly except via
key_capture.py's bind-capture widget helper -- these dataclasses are pure
data, no I/O.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from key_capture import KeyBind, UNBOUND

_id_counter = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f"{prefix}-{next(_id_counter)}"


# ---------------------------------------------------------------------------
# Remapper
# ---------------------------------------------------------------------------


@dataclass
class RemapEntry:
    id: str
    source: KeyBind = field(default_factory=lambda: UNBOUND)
    destination: KeyBind = field(default_factory=lambda: UNBOUND)
    enabled: bool = True


@dataclass
class RemapperState:
    entries: List[RemapEntry] = field(default_factory=list)
    # which entry/field is currently mid-capture ("source" | "destination"), else None
    capturing_entry_id: Optional[str] = None
    capturing_field: Optional[str] = None

    def add_entry(self) -> RemapEntry:
        entry = RemapEntry(id=_next_id("remap"))
        self.entries.append(entry)
        return entry

    def remove_entry(self, entry_id: str) -> None:
        self.entries = [e for e in self.entries if e.id != entry_id]
        if self.capturing_entry_id == entry_id:
            self.capturing_entry_id = None
            self.capturing_field = None


# ---------------------------------------------------------------------------
# Macros
# ---------------------------------------------------------------------------


class MacroMode(Enum):
    ONCE = "Once"
    HOLD = "Hold"
    TOGGLE = "Toggle"


class MacroStepKind(Enum):
    KEY_DOWN = "Key Down"
    KEY_UP = "Key Up"
    KEY_TAP = "Key Tap"
    MOUSE_DOWN = "Mouse Down"
    MOUSE_UP = "Mouse Up"
    MOUSE_CLICK = "Mouse Click"
    SCROLL = "Scroll"
    DELAY = "Delay"


@dataclass
class MacroStep:
    id: str
    kind: MacroStepKind = MacroStepKind.KEY_TAP
    key: KeyBind = field(default_factory=lambda: UNBOUND)
    mouse_button: str = "Left"
    scroll_delta: int = 120
    delay_ms: int = 50


@dataclass
class MacroDef:
    id: str
    name: str = "New Macro"
    trigger: KeyBind = field(default_factory=lambda: UNBOUND)
    mode: MacroMode = MacroMode.ONCE
    enabled: bool = True
    humanize_jitter_pct: int = 15
    steps: List[MacroStep] = field(default_factory=list)

    def add_step(self) -> MacroStep:
        step = MacroStep(id=_next_id("step"))
        self.steps.append(step)
        return step


@dataclass
class MacrosState:
    macros: List[MacroDef] = field(default_factory=list)
    selected_id: Optional[str] = None
    capturing_macro_id: Optional[str] = None  # trigger-bind capture in progress, if any
    capturing_step_id: Optional[str] = None  # per-step key-bind capture in progress, if any

    def add_macro(self) -> MacroDef:
        macro = MacroDef(id=_next_id("macro"), name=f"Macro {len(self.macros) + 1}")
        self.macros.append(macro)
        self.selected_id = macro.id
        return macro

    def remove_macro(self, macro_id: str) -> None:
        self.macros = [m for m in self.macros if m.id != macro_id]
        if self.selected_id == macro_id:
            self.selected_id = self.macros[0].id if self.macros else None
        if self.capturing_macro_id == macro_id:
            self.capturing_macro_id = None

    def find(self, macro_id: Optional[str]) -> Optional[MacroDef]:
        return next((m for m in self.macros if m.id == macro_id), None)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@dataclass
class ProfileDef:
    id: str
    name: str
    protected: bool = False  # the "Default" profile -- cannot be deleted/renamed
    # Per-module "survive profile load" flags -- mirrors R9Tools' pattern of
    # deciding this per-module rather than assuming; profiles.py is the
    # future source of truth for these, this is just the editable mirror.
    persist_remapper: bool = False
    persist_macros: bool = False
    persist_window_select: bool = False


@dataclass
class ProfilesState:
    profiles: List[ProfileDef] = field(default_factory=list)
    active_id: str = ""
    new_profile_draft: str = ""  # scratch buffer for the "create profile" name field

    def add_profile(self, name: str) -> ProfileDef:
        profile = ProfileDef(id=_next_id("profile"), name=name)
        self.profiles.append(profile)
        return profile

    def remove_profile(self, profile_id: str) -> None:
        target = next((p for p in self.profiles if p.id == profile_id), None)
        if target is None or target.protected:
            return
        self.profiles = [p for p in self.profiles if p.id != profile_id]
        if self.active_id == profile_id:
            default = next((p for p in self.profiles if p.protected), None)
            self.active_id = default.id if default else (self.profiles[0].id if self.profiles else "")


def default_profiles_state() -> ProfilesState:
    state = ProfilesState()
    default_profile = ProfileDef(id=_next_id("profile"), name="Default", protected=True)
    state.profiles.append(default_profile)
    state.active_id = default_profile.id
    return state


# ---------------------------------------------------------------------------
# Window select
# ---------------------------------------------------------------------------


@dataclass
class ProcessInfo:
    """Mirrors what window_select.py's future psutil/win32gui enumeration
    will hand back: enough to display and to target a process."""

    pid: int
    exe_name: str
    window_title: str


@dataclass
class WindowSelectState:
    # blank/unset selection == global, unrestricted (per engine-agent.md)
    selected: Optional[ProcessInfo] = None
    available: List[ProcessInfo] = field(default_factory=list)
    filter_text: str = ""
    # Display-only: whether `selected` currently holds real OS foreground
    # focus. No engine exists yet to compute this live; this field exists so
    # the panel's "inert while unfocused" messaging has somewhere real to
    # read from once window_select.py is wired in.
    selected_has_focus: bool = False


# ---------------------------------------------------------------------------
# Overlay (Companion-side config for the future HUD overlay -- rendering
# itself is a separate, future ui-agent task; this is only the toggle/style
# state the HUD would read)
# ---------------------------------------------------------------------------


@dataclass
class StatsHudState:
    enabled: bool = False
    show_cpu: bool = True
    show_gpu: bool = True
    show_ram: bool = True
    show_fps: bool = True
    corner: str = "Top Right"
    scale: float = 1.0
    color: Tuple[float, float, float, float] = (0.93, 0.94, 0.96, 1.0)


@dataclass
class CrosshairState:
    enabled: bool = False
    style: str = "Cross"
    size: float = 12.0
    thickness: float = 2.0
    color: Tuple[float, float, float, float] = (0.24, 0.86, 0.52, 1.0)


@dataclass
class StatusIndicatorsState:
    enabled: bool = False
    show_remap_status: bool = True
    show_macro_status: bool = True
    show_profile_name: bool = True
    corner: str = "Bottom Left"
    scale: float = 1.0


@dataclass
class OverlayState:
    stats_hud: StatsHudState = field(default_factory=StatsHudState)
    crosshair: CrosshairState = field(default_factory=CrosshairState)
    status_indicators: StatusIndicatorsState = field(default_factory=StatusIndicatorsState)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class SettingsState:
    theme_name: str = "dark"
    ui_scale: float = 1.0
    reduce_motion: bool = False
    # Placeholder Updates area -- build-agent's self-updater doesn't exist yet.
    updates_channel: str = "Stable"
    check_for_updates_on_launch: bool = True
    last_checked_display: str = "Never checked"


# ---------------------------------------------------------------------------
# Top-level app state
# ---------------------------------------------------------------------------

PANELS: Tuple[str, ...] = (
    "dashboard",
    "overlay",
    "macros",
    "remapper",
    "profiles",
    "settings",
    "about",
)


@dataclass
class AppState:
    remapper: RemapperState = field(default_factory=RemapperState)
    macros: MacrosState = field(default_factory=MacrosState)
    profiles: ProfilesState = field(default_factory=default_profiles_state)
    window_select: WindowSelectState = field(default_factory=WindowSelectState)
    overlay: OverlayState = field(default_factory=OverlayState)
    settings: SettingsState = field(default_factory=SettingsState)
    active_panel: str = "dashboard"


def new_app_state() -> AppState:
    """Build a fresh, empty AppState (nothing here is persisted -- there's
    no profiles.py yet to load from, so a new run always starts blank)."""
    return AppState()
