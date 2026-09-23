"""In-memory state shape for the Companion window. Pure data, no I/O --
engine modules sync into/out of this shape once per frame."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from key_capture import KeyBind, UNBOUND
from theme import hex_rgba


def _next_id(prefix: str) -> str:
    # uuid4, not a counter -- a counter restarts at 1 every launch and collides across sessions.
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Remapper
# ---------------------------------------------------------------------------


class RemapMode(Enum):
    HOLD = "Hold"
    TOGGLE = "Toggle"


@dataclass
class RemapEntry:
    """A plain 1:1 remap: destination mirrors source down/up."""

    id: str
    source: KeyBind = field(default_factory=lambda: UNBOUND)
    destination: KeyBind = field(default_factory=lambda: UNBOUND)
    enabled: bool = True
    name: str = ""  # cosmetic only, never read by matching/injection


@dataclass
class AutoToggleHoldEntry:
    """One key acting on itself: Toggle latches it down on the first press
    and releases on the next; Hold taps it on both press and release edges.
    See remapper.py for the actual state machines."""

    id: str
    name: str = ""
    key: KeyBind = field(default_factory=lambda: UNBOUND)
    mode: RemapMode = RemapMode.TOGGLE
    enabled: bool = True


@dataclass
class RemapperState:
    entries: List[RemapEntry] = field(default_factory=list)
    auto_entries: List[AutoToggleHoldEntry] = field(default_factory=list)
    # which entry/field is currently mid-capture ("source" | "destination"), else None
    capturing_entry_id: Optional[str] = None
    capturing_field: Optional[str] = None
    capturing_auto_id: Optional[str] = None  # Auto Toggle/Hold entry mid-capture, if any

    def add_entry(self) -> RemapEntry:
        entry = RemapEntry(id=_next_id("remap"))
        self.entries.append(entry)
        return entry

    def remove_entry(self, entry_id: str) -> None:
        self.entries = [e for e in self.entries if e.id != entry_id]
        if self.capturing_entry_id == entry_id:
            self.capturing_entry_id = None
            self.capturing_field = None

    def add_auto_entry(self) -> AutoToggleHoldEntry:
        entry = AutoToggleHoldEntry(id=_next_id("auto"))
        self.auto_entries.append(entry)
        return entry

    def remove_auto_entry(self, entry_id: str) -> None:
        self.auto_entries = [e for e in self.auto_entries if e.id != entry_id]
        if self.capturing_auto_id == entry_id:
            self.capturing_auto_id = None


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
    MOUSE_MOVE_TO = "Move Mouse To"  # absolute jump to a recorded point
    MOUSE_MOVE_BY = "Move Mouse By"  # fixed relative (dx, dy) nudge; unrestricted mode-wise, see macro_engine.py


@dataclass
class MacroStep:
    id: str
    kind: MacroStepKind = MacroStepKind.KEY_TAP
    key: KeyBind = field(default_factory=lambda: UNBOUND)
    mouse_button: str = "Left"
    scroll_delta: int = 120
    delay_ms: int = 50
    # MOUSE_MOVE_TO: absolute virtual-desktop target coordinate.
    # MOUSE_MOVE_BY: fixed relative (dx, dy) nudge. Unused by every other kind.
    move_x: int = 0
    move_y: int = 0
    # MOUSE_MOVE_BY only: path wobble 0-100, independent of MacroDef.humanize_jitter_pct (distance). 0 = straight line.
    path_wobble_pct: int = 0
    # MOUSE_MOVE_TO only: 0-100, how fast the cursor glides to the target (see macro_engine.py's glide speed range).
    # Move To always glides in small steps timed at SettingsState.mouse_polling_rate_hz -- never an instant teleport.
    move_speed_pct: int = 50


@dataclass
class MacroDef:
    id: str
    name: str = "New Macro"
    trigger: KeyBind = field(default_factory=lambda: UNBOUND)
    trigger_modifiers: List[KeyBind] = field(default_factory=list)  # extra keys that must be held for trigger to fire, e.g. Shift+R
    mode: MacroMode = MacroMode.ONCE
    enabled: bool = True
    humanize_jitter_pct: int = 15
    steps: List[MacroStep] = field(default_factory=list)

    def has_move_by_step(self) -> bool:
        return any(s.kind == MacroStepKind.MOUSE_MOVE_BY for s in self.steps)

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
    capturing_move_step_id: Optional[str] = None  # per-step Move-To position capture in progress, if any
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
# Soundboard
# ---------------------------------------------------------------------------


@dataclass
class SoundClip:
    """One importable audio file bound to a hotkey. file_path is recorded
    as given, not copied into app-owned storage."""

    id: str
    name: str = "New Sound"
    file_path: str = ""
    hotkey: KeyBind = field(default_factory=lambda: UNBOUND)
    volume: float = 1.0  # 0.0-1.0, this clip's own gain
    enabled: bool = True


@dataclass
class SoundboardState:
    clips: List[SoundClip] = field(default_factory=list)
    output_device_name: str = ""
    selected_clip_id: Optional[str] = None
    capturing_clip_id: Optional[str] = None  # per-clip hotkey capture in progress, if any
    device_filter_text: str = ""  # scratch filter text for the output device picker

    def add_clip(self) -> SoundClip:
        clip = SoundClip(id=_next_id("sound"))
        self.clips.append(clip)
        self.selected_clip_id = clip.id
        return clip

    def remove_clip(self, clip_id: str) -> None:
        self.clips = [c for c in self.clips if c.id != clip_id]
        if self.selected_clip_id == clip_id:
            self.selected_clip_id = self.clips[0].id if self.clips else None
        if self.capturing_clip_id == clip_id:
            self.capturing_clip_id = None

    def find(self, clip_id: Optional[str]) -> Optional[SoundClip]:
        return next((c for c in self.clips if c.id == clip_id), None)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


@dataclass
class ProfileDef:
    id: str
    name: str
    protected: bool = False  # the "Default" profile -- cannot be deleted/renamed
    # Per-module "survive profile load" flags; profiles.py owns the real payload.
    persist_remapper: bool = False
    persist_macros: bool = False
    persist_window_select: bool = False
    target_executable: str = ""  # exe name this profile auto-loads for, see profiles.check_auto_switch()


@dataclass
class ProfilesState:
    profiles: List[ProfileDef] = field(default_factory=list)
    active_id: str = ""
    new_profile_draft: str = ""  # scratch buffer for the "create profile" name field
    auto_switch_filter_text: str = ""  # scratch filter text for the auto-switch target-executable picker
    # Momentary "Saved!" indicator: which profile and until what monotonic timestamp to show it.
    save_flash_id: Optional[str] = None
    save_flash_until: float = 0.0

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
    selected: Optional[ProcessInfo] = None  # None == global, unrestricted
    available: List[ProcessInfo] = field(default_factory=list)
    filter_text: str = ""
    selected_has_focus: bool = False  # kept live by window_select.py


# ---------------------------------------------------------------------------
# Overlay -- config the HUD overlay (hud_overlay.py) reads each frame
# ---------------------------------------------------------------------------


@dataclass
class StatsHudState:
    enabled: bool = False
    show_cpu: bool = True
    show_gpu: bool = True
    show_ram: bool = True
    show_fps: bool = True
    show_fps_graph: bool = True  # sparkline only -- the 1%/0.1% Low text line stays under show_fps
    corner: str = "Top Right"
    scale: float = 1.0
    color: Tuple[float, float, float, float] = (0.93, 0.94, 0.96, 1.0)
    bg_alpha: float = 0.55  # card background transparency, independent of color (text only)


@dataclass
class CrosshairState:
    enabled: bool = False
    style: str = "Cross"
    size: float = 12.0
    thickness: float = 2.0
    gap: float = 3.0  # Cross/T-Shape: gap between center and arm. Circle+Dot: ring radius offset. Unused by Dot/Circle.
    color: Tuple[float, float, float, float] = (0.24, 0.86, 0.52, 1.0)


@dataclass
class StatusIndicatorsState:
    """Two themed count badges (Remapper, Macros), showing how many entries are currently enabled."""

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
    """Synced from updater.UpdateManager.sync_to() each frame."""

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
    # Color Cycle config, only meaningful while theme_name == "color_cycle". See theme.py's resolve_color_cycle_theme().
    cycle_color_a: Tuple[float, float, float, float] = field(default_factory=lambda: hex_rgba("#3D6FD1"))
    cycle_color_b: Tuple[float, float, float, float] = field(default_factory=lambda: hex_rgba("#7C5CE0"))
    cycle_period_sec: float = 20.0  # full back-and-forth cycle, seconds
    cycle_elapsed_sec: float = 0.0  # advanced each frame by shell.py, frozen while reduce_motion is on
    check_for_updates_on_launch: bool = True
    close_minimizes_to_tray: bool = True  # False: X button exits outright, see titlebar.py's _close()
    auto_switch_profiles: bool = False  # off by default -- swapping config on focus change is opt-in
    mouse_polling_rate_hz: int = 1000  # used by Macro's Move To/Move By stepping; matches the physical mouse's own polling rate
    last_checked_display: str = "Never checked"
    update_status: UpdateStatus = UpdateStatus.IDLE
    update_latest_version: str = ""
    update_download_pct: int = 0
    update_error_message: str = ""
    auto_update_prompt_pending: bool = False  # one automatic check-on-launch prompt per session, until Update Now/Later


# ---------------------------------------------------------------------------
# Top-level app state
# ---------------------------------------------------------------------------

PANELS: Tuple[str, ...] = (
    "dashboard",
    "overlay",
    "macros",
    "remapper",
    "soundboard",
    "profiles",
    "settings",
    "about",
)


@dataclass
class AppState:
    remapper: RemapperState = field(default_factory=RemapperState)
    macros: MacrosState = field(default_factory=MacrosState)
    soundboard: SoundboardState = field(default_factory=SoundboardState)
    profiles: ProfilesState = field(default_factory=default_profiles_state)
    window_select: WindowSelectState = field(default_factory=WindowSelectState)
    overlay: OverlayState = field(default_factory=OverlayState)
    settings: SettingsState = field(default_factory=SettingsState)
    active_panel: str = "dashboard"
    stats_fps_error: Optional[str] = None  # runtime-only, why PresentMon last failed, synced from stats_poller


def new_app_state() -> AppState:
    return AppState()
