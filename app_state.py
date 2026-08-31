"""app_state.py -- in-memory state shape for the Companion window.

Pure data, no I/O. Panels read/write these dataclasses directly; the engine
modules (remapper.py, macro_engine.py, profiles.py, window_select.py,
stats_poller.py, settings_store.py) own the actual hardware/disk/network
side and sync into or out of this shape once per frame.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from key_capture import KeyBind, UNBOUND
from theme import hex_rgba

_id_counter = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f"{prefix}-{next(_id_counter)}"


# ---------------------------------------------------------------------------
# Remapper
# ---------------------------------------------------------------------------


class RemapMode(Enum):
    HOLD = "Hold"
    TOGGLE = "Toggle"


@dataclass
class RemapEntry:
    id: str
    source: KeyBind = field(default_factory=lambda: UNBOUND)
    destination: KeyBind = field(default_factory=lambda: UNBOUND)
    enabled: bool = True
    # Hold (default): destination mirrors source down/up 1:1. Toggle: first
    # press latches destination down, next press releases it; source release
    # is a no-op. New field -- absent/unparsed on disk always resolves to
    # HOLD (see profiles.py's _remap_entry_from_json), never silently
    # changes an existing saved binding's behavior.
    mode: RemapMode = RemapMode.HOLD


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
    recording_macro_id: Optional[str] = None  # macro_recorder session in progress, if any

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
        if self.recording_macro_id == macro_id:
            self.recording_macro_id = None

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
    # Per-module "survive profile load" flags. profiles.py owns the real
    # payload; this is the editable mirror.
    persist_remapper: bool = False
    persist_macros: bool = False
    persist_window_select: bool = False
    # Exe name (e.g. "eft.exe") this profile auto-loads for -- see
    # profiles.check_auto_switch(). Blank (the default, and every profile
    # saved before this field existed) never participates in auto-switch.
    target_executable: str = ""


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
    """Enough to display and target a running process."""

    pid: int
    exe_name: str
    window_title: str


@dataclass
class WindowSelectState:
    # blank/unset selection == global, unrestricted
    selected: Optional[ProcessInfo] = None
    available: List[ProcessInfo] = field(default_factory=list)
    filter_text: str = ""
    # Whether `selected` currently holds real OS foreground focus -- kept
    # live by window_select.py.
    selected_has_focus: bool = False


# ---------------------------------------------------------------------------
# Overlay -- config the HUD overlay (hud_overlay.py) reads each frame to
# decide what to render.
# ---------------------------------------------------------------------------


@dataclass
class StatsHudState:
    enabled: bool = False
    show_cpu: bool = True
    show_gpu: bool = True
    show_ram: bool = True
    show_fps: bool = True
    # Sparkline only -- the 1%/0.1% Low text line stays under show_fps.
    show_fps_graph: bool = True
    corner: str = "Top Right"
    scale: float = 1.0
    color: Tuple[float, float, float, float] = (0.93, 0.94, 0.96, 1.0)
    # Card background transparency, independent of `color` (text only).
    # 0 = see-through, 1 = opaque.
    bg_alpha: float = 0.55


@dataclass
class CrosshairState:
    enabled: bool = False
    style: str = "Cross"
    size: float = 12.0
    thickness: float = 2.0
    # Cross/T-Shape: gap between center and each arm (0 = arms touch,
    # forming an unbroken plus/T). Circle + Dot: offset added to the ring's
    # radius, independent of the dot's size or the ring's thickness. Unused
    # by Dot and plain Circle.
    gap: float = 3.0
    color: Tuple[float, float, float, float] = (0.24, 0.86, 0.52, 1.0)


@dataclass
class StatusIndicatorsState:
    """Two themed count badges (Remapper, Macros), each showing how many of
    that module's entries are currently *enabled* -- not the total
    configured count."""

    enabled: bool = False
    show_remap_badge: bool = True
    show_macro_badge: bool = True
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


class UpdateStatus(Enum):
    """Synced from updater.UpdateManager.sync_to() each frame -- never set
    directly from a background thread."""

    IDLE = "idle"
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    READY = "ready"
    INSTALLING = "installing"
    ERROR = "error"


@dataclass
class SettingsState:
    theme_name: str = "dark"
    reduce_motion: bool = False
    # Color Cycle config -- only meaningful while theme_name == "color_cycle".
    # See theme.py's resolve_color_cycle_theme().
    cycle_color_a: Tuple[float, float, float, float] = field(default_factory=lambda: hex_rgba("#3D6FD1"))
    cycle_color_b: Tuple[float, float, float, float] = field(default_factory=lambda: hex_rgba("#7C5CE0"))
    cycle_period_sec: float = 20.0  # full back-and-forth cycle, seconds
    # Advanced each frame by shell.py, frozen while reduce_motion is on.
    cycle_elapsed_sec: float = 0.0
    check_for_updates_on_launch: bool = True
    # False: X button exits outright instead of hiding to tray. See
    # titlebar.py's _close().
    close_minimizes_to_tray: bool = True
    # Off by default -- silently swapping the whole Remapper/Macros config on
    # a focus change should be opt-in, not a surprise. See
    # profiles.check_auto_switch().
    auto_switch_profiles: bool = False
    last_checked_display: str = "Never checked"
    update_status: UpdateStatus = UpdateStatus.IDLE
    update_latest_version: str = ""
    update_download_pct: int = 0
    update_error_message: str = ""
    # True for one automatic check-on-launch result per session, until the
    # user picks Update Now/Later. "Later" only clears this, never touches
    # check_for_updates_on_launch.
    auto_update_prompt_pending: bool = False


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
    # Runtime-only, never persisted. Why PresentMon's FPS tracking last
    # failed, if it did -- synced from stats_poller each frame, surfaced by
    # panels/overlay.py's Stats HUD card.
    stats_fps_error: Optional[str] = None


def new_app_state() -> AppState:
    """Build a blank AppState. main.py loads settings_store/profiles data
    into it right after construction."""
    return AppState()
