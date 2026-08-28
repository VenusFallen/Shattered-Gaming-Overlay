"""app_state.py -- in-memory state shape for the Companion window.

No backend engine exists yet (remapper.py / macro_engine.py /
profiles.py / window_select.py are future work), so every panel below owns
its own local state for now. The shapes here are deliberately close to what
those future engine modules will eventually need, so wiring a real engine in
later is a small "swap the in-memory list for a call into profiles.py" change,
not a redesign:

  - RemapperState.entries          ~= what remapper.py will load/save per profile
  - MacrosState.macros             ~= what macro_engine.py will record/play back
  - ProfilesState.profiles         ~= what profiles.py will persist to disk
  - WindowSelectState              ~= what window_select.py will enumerate/track
  - OverlayState                   ~= what the future HUD overlay will read to
                                      decide what to render (future UI work,
                                      not engine work)

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
from theme import hex_rgba

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
    # blank/unset selection == global, unrestricted
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
# itself is separate future work; this is only the toggle/style
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
    # Background transparency for the Stats box's rounded-rect card --
    # independent of `color` (which is the text color only). 0 = fully
    # see-through, 1 = fully opaque. See panels/overlay.py's stats card for
    # the slider and hud_overlay.py's _draw_stats_box for the consumer.
    bg_alpha: float = 0.55


@dataclass
class CrosshairState:
    enabled: bool = False
    style: str = "Cross"
    size: float = 12.0
    thickness: float = 2.0
    color: Tuple[float, float, float, float] = (0.24, 0.86, 0.52, 1.0)


@dataclass
class StatusIndicatorsState:
    """Two themed count badges -- Remapper and Macros -- each showing how
    many of that module's entries are currently *enabled* (not the total
    configured count; a status indicator should reflect what's live right
    now). Replaces the earlier show_remap_status/show_macro_status/
    show_profile_name 3-toggle model, which didn't fit a count-based,
    two-badge design -- no profile-name indicator in this design."""

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
    """Mirrors R9Tools' informal `_appState` string states (see its
    panels/settings.py), formalized as an enum here per updater.py's module
    docstring. Read/written every frame by updater.UpdateManager.sync_to()
    -- see main.py's `_show_gui` for the call site (same spot
    remapper_engine.update_snapshot()/macro_engine's own equivalents are
    called) -- never set directly from a background thread."""

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
    # Color Cycle theme config (see theme.py's COLOR_CYCLE /
    # resolve_color_cycle_theme) -- only meaningful while theme_name ==
    # "color_cycle"; harmless dead data otherwise. Defaults reuse Dark's own
    # accent and Violet's own accent (both already contrast-validated
    # individually in theme.py) rather than arbitrary new hues.
    cycle_color_a: Tuple[float, float, float, float] = field(default_factory=lambda: hex_rgba("#3D6FD1"))
    cycle_color_b: Tuple[float, float, float, float] = field(default_factory=lambda: hex_rgba("#7C5CE0"))
    # Full back-and-forth cycle length in seconds -- a slow ambient drift,
    # never fast/strobing (see panels/settings.py's slider bounds, which
    # keep even the "fastest" end of the range in this same slow territory).
    cycle_period_sec: float = 20.0
    # Seconds fed into theme.color_cycle_phase() -- advanced by shell.py
    # each frame using imgui's delta_time, but ONLY while reduce_motion is
    # off (see shell.py's render_frame). Reduce Motion freezes the
    # animation by simply not advancing this, rather than the resolver
    # needing to know about reduce_motion itself.
    cycle_elapsed_sec: float = 0.0
    # Updates -- see updater.py (the self-updater, GitHub Releases
    # based) and panels/settings.py's _render_updates. `update_status` /
    # `update_latest_version` / `update_download_pct` / `update_error_message`
    # / `last_checked_display` are all written by updater.update_manager's
    # sync_to() call in main.py, never edited directly by any panel.
    check_for_updates_on_launch: bool = True
    # Titlebar close-button behavior: True (default) hides to tray via
    # titlebar.py's _close(), same as R9Tools' convention; False skips the
    # tray entirely and exits the app outright. See titlebar.py's _close()
    # docstring for the actual branch.
    close_minimizes_to_tray: bool = True
    last_checked_display: str = "Never checked"
    update_status: UpdateStatus = UpdateStatus.IDLE
    update_latest_version: str = ""
    update_download_pct: int = 0
    update_error_message: str = ""
    # True for exactly one automatic check-on-launch result per session, from
    # the moment a newer release is found until the user picks "Update Now"
    # or "Later" in the prompt panels/settings.py's render_auto_update_prompt
    # draws (see shell.py's call site) -- "Later" only clears this flag, it
    # never touches check_for_updates_on_launch (per R9Tools' README-
    # documented behavior: skip for the session, not disable the setting).
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


def new_app_state() -> AppState:
    """Build a fresh, empty AppState (nothing here is persisted -- there's
    no profiles.py yet to load from, so a new run always starts blank)."""
    return AppState()
