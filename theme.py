"""theme.py -- palette/style system for the Companion window.

`Theme` is a frozen dataclass of named colors + layout metrics; `THEMES` is
the registry. Panels pull named tokens off the active `Theme`, never
hardcode colors (see `apply_theme`/`get_theme`). Seven themes ship (Dark,
Violet, Ember, Slate, High Contrast, Daylight, and the animated Color
Cycle).

Contrast ratios quoted in comments below use the WCAG 2.2 relative-luminance
formula (linearized sRGB, L = 0.2126R + 0.7152G + 0.0722B; AA >= 4.5:1,
AAA >= 7:1), computed against colors already composited over their base
background where translucent. `border`/`nav_*_bg` are decorative, not held
to text contrast; `text_disabled` is intentionally sub-AA (WCAG exempts
disabled content) -- never use it for interactive text.

DARK (default): text_primary 15.7:1/13.2:1 (base/card, AAA), text_secondary
8.7:1/7.3:1 (AAA), text_disabled 3.7:1 (exempt), white-on-accent family
4.8/4.6/6.0:1 (AA), accent_text 8.7:1 (AAA), status colors 10.1/10.3/5.4/9.2:1.
"""

from __future__ import annotations

import colorsys
import dataclasses
import math
from typing import Dict, Tuple

from imgui_bundle import imgui

RGBA = Tuple[float, float, float, float]


def hex_rgba(hex_code: str, alpha: float = 1.0) -> RGBA:
    """'#RRGGBB' (or 'RRGGBB') -> (r, g, b, a) floats in [0, 1]."""
    h = hex_code.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return (r, g, b, alpha)


@dataclasses.dataclass(frozen=True)
class Theme:
    """One complete, swappable palette + metrics set for the Companion window."""

    name: str
    display_name: str

    # --- surfaces ---
    bg_base: RGBA  # main content area background
    bg_sidebar: RGBA  # nav sidebar / chrome, a touch darker than bg_base
    bg_popup: RGBA  # popups, combos, tooltips
    bg_card: RGBA  # panel/card surfaces -- thin translucency (glassmorphism), not heavy blur
    bg_card_hover: RGBA
    bg_input: RGBA  # checkbox/slider/text-input frame background
    bg_input_hover: RGBA
    bg_input_active: RGBA
    border: RGBA
    border_strong: RGBA

    # --- text ---
    text_primary: RGBA
    text_secondary: RGBA
    text_disabled: RGBA  # exempt from AA -- see module docstring; never use for interactive text

    # --- accent / brand ---
    accent: RGBA
    accent_hover: RGBA
    accent_active: RGBA
    accent_text: RGBA  # accent used AS text (links, selected nav label) -- brighter than `accent`

    # --- status colors -- always paired with an icon + label, never the sole
    # carrier of meaning (see widgets.status_badge) ---
    success: RGBA
    warning: RGBA
    danger: RGBA
    info: RGBA

    # --- nav ---
    nav_selected_bg: RGBA
    nav_hover_bg: RGBA

    # --- metrics -- no in-app UI-scale multiplier; the app relies on Hello
    # ImGui/Windows' own DPI scaling instead of a custom slider ---
    window_rounding: float = 10.0
    card_rounding: float = 10.0
    frame_rounding: float = 6.0
    grab_rounding: float = 6.0
    scrollbar_rounding: float = 8.0
    border_size: float = 1.0
    window_padding: Tuple[float, float] = (16.0, 16.0)
    frame_padding: Tuple[float, float] = (10.0, 6.0)
    item_spacing: Tuple[float, float] = (10.0, 8.0)
    item_inner_spacing: Tuple[float, float] = (8.0, 6.0)
    indent_spacing: float = 20.0


DARK = Theme(
    name="dark",
    display_name="Dark (default)",
    bg_base=hex_rgba("#14161C"),
    bg_sidebar=hex_rgba("#101218"),
    bg_popup=hex_rgba("#181B22", 0.98),
    bg_card=hex_rgba("#242832", 0.90),
    bg_card_hover=hex_rgba("#2A2F3A", 0.94),
    bg_input=hex_rgba("#1B1E26"),
    bg_input_hover=hex_rgba("#232733"),
    bg_input_active=hex_rgba("#262B38"),
    border=hex_rgba("#333846", 0.80),
    border_strong=hex_rgba("#454B5C", 1.0),
    text_primary=hex_rgba("#EDEFF4"),
    text_secondary=hex_rgba("#AEB4C2"),
    text_disabled=hex_rgba("#6B7180"),
    accent=hex_rgba("#3D6FD1"),
    accent_hover=hex_rgba("#4272D4"),
    accent_active=hex_rgba("#3560B8"),
    accent_text=hex_rgba("#8FB4FF"),
    success=hex_rgba("#3DDC84"),
    warning=hex_rgba("#F5B942"),
    danger=hex_rgba("#F2555A"),
    info=hex_rgba("#5FC6E8"),
    nav_selected_bg=hex_rgba("#3D6FD1", 0.16),
    nav_hover_bg=(1.0, 1.0, 1.0, 0.05),
)

# Violet: text_primary 15.8/13.6:1 (AAA), text_secondary 8.8/7.5:1 (AAA),
# white-on-accent family 4.7/5.4/6.1:1 (AA), accent_text 9.4:1, status 10.3/10.4/5.4/9.4:1.
VIOLET = Theme(
    name="violet",
    display_name="Violet",
    bg_base=hex_rgba("#16131F"),
    bg_sidebar=hex_rgba("#100D18"),
    bg_popup=hex_rgba("#1B1728", 0.98),
    bg_card=hex_rgba("#282139", 0.90),
    bg_card_hover=hex_rgba("#2E2642", 0.94),
    bg_input=hex_rgba("#1D1A2B"),
    bg_input_hover=hex_rgba("#252036"),
    bg_input_active=hex_rgba("#2A2440"),
    border=hex_rgba("#3A3350", 0.80),
    border_strong=hex_rgba("#4C4468", 1.0),
    text_primary=hex_rgba("#F0EDF8"),
    text_secondary=hex_rgba("#B9AFCC"),
    text_disabled=hex_rgba("#726C87"),
    accent=hex_rgba("#7C5CE0"),
    accent_hover=hex_rgba("#7455CC"),
    accent_active=hex_rgba("#6A4CC4"),
    accent_text=hex_rgba("#C3AEFF"),
    success=hex_rgba("#3DDC84"),
    warning=hex_rgba("#F5B942"),
    danger=hex_rgba("#F2555A"),
    info=hex_rgba("#5FC6E8"),
    nav_selected_bg=hex_rgba("#7C5CE0", 0.16),
    nav_hover_bg=(1.0, 1.0, 1.0, 0.05),
)

# Ember: text_primary 15.9/13.6:1 (AAA), text_secondary 9.6/8.2:1 (AAA).
# Uses near-black button labels, not white -- amber/orange can't clear 4.5:1
# with white text at any reasonable brightness. Dark-on-accent 6.1/6.9/5.6:1
# (AA), accent_text 10.5:1, status 9.2/11.2/5.8/9.3:1.
EMBER = Theme(
    name="ember",
    display_name="Ember",
    bg_base=hex_rgba("#1A1512"),
    bg_sidebar=hex_rgba("#141110"),
    bg_popup=hex_rgba("#201A16", 0.98),
    bg_card=hex_rgba("#2E241C", 0.90),
    bg_card_hover=hex_rgba("#362A20", 0.94),
    bg_input=hex_rgba("#211A15"),
    bg_input_hover=hex_rgba("#28201A"),
    bg_input_active=hex_rgba("#2D241D"),
    border=hex_rgba("#3D3025", 0.80),
    border_strong=hex_rgba("#544234", 1.0),
    text_primary=hex_rgba("#F7EFE7"),
    text_secondary=hex_rgba("#CBBAA9"),
    text_disabled=hex_rgba("#7E7061"),
    accent=hex_rgba("#D97A34"),
    accent_hover=hex_rgba("#E08640"),
    accent_active=hex_rgba("#C67A40"),
    accent_text=hex_rgba("#FFB673"),
    success=hex_rgba("#4CD08A"),
    warning=hex_rgba("#F2C744"),
    danger=hex_rgba("#F2645A"),
    info=hex_rgba("#5FC6E8"),
    nav_selected_bg=hex_rgba("#D97A34", 0.16),
    nav_hover_bg=(1.0, 1.0, 1.0, 0.05),
)

# Slate: text_primary 15.8/13.3:1 (AAA), text_secondary 8.8/7.4:1 (AAA),
# dark-on-accent 6.3/7.1/4.7:1 (AA), accent_text 10.9:1, status 10.2/10.3/5.4/11.3:1.
SLATE = Theme(
    name="slate",
    display_name="Slate",
    bg_base=hex_rgba("#12161B"),
    bg_sidebar=hex_rgba("#0D1015"),
    bg_popup=hex_rgba("#171B21", 0.98),
    bg_card=hex_rgba("#212831", 0.90),
    bg_card_hover=hex_rgba("#272F39", 0.94),
    bg_input=hex_rgba("#191E24"),
    bg_input_hover=hex_rgba("#20262E"),
    bg_input_active=hex_rgba("#242B34"),
    border=hex_rgba("#333B45", 0.80),
    border_strong=hex_rgba("#454F5B", 1.0),
    text_primary=hex_rgba("#EBF0F4"),
    text_secondary=hex_rgba("#A9B6C2"),
    text_disabled=hex_rgba("#697683"),
    accent=hex_rgba("#3FA0C4"),
    accent_hover=hex_rgba("#47AAD0"),
    accent_active=hex_rgba("#3488A8"),
    accent_text=hex_rgba("#7FD4F0"),
    success=hex_rgba("#3DDC84"),
    warning=hex_rgba("#F5B942"),
    danger=hex_rgba("#F2555A"),
    info=hex_rgba("#6FDAF5"),
    nav_selected_bg=hex_rgba("#3FA0C4", 0.16),
    nav_hover_bg=(1.0, 1.0, 1.0, 0.05),
)

# Daylight (light theme): text_primary 16.6/18.0:1 (AAA), text_secondary
# 7.9/8.6:1 (AAA), white-on-accent 5.6/4.8/7.2:1 (AA), accent_text 8.6:1.
# Status hues are independently darkened/desaturated from the dark themes'
# swatches -- reused verbatim they'd fall to ~2-4:1 on a near-white bg.
# Status colors on bg_base: 5.6/5.8/5.1/5.1:1 (AA).
DAYLIGHT = Theme(
    name="daylight",
    display_name="Daylight (Light)",
    bg_base=hex_rgba("#F4F5F8"),
    bg_sidebar=hex_rgba("#EAEBEF"),
    bg_popup=hex_rgba("#FFFFFF", 0.98),
    bg_card=hex_rgba("#FFFFFF", 0.92),
    bg_card_hover=hex_rgba("#ECEEF3", 0.95),
    bg_input=hex_rgba("#EDEEF2"),
    bg_input_hover=hex_rgba("#E4E7ED"),
    bg_input_active=hex_rgba("#DCE0E8"),
    border=hex_rgba("#D8DBE2", 0.80),
    border_strong=hex_rgba("#C3C8D2", 1.0),
    text_primary=hex_rgba("#14161C"),
    text_secondary=hex_rgba("#454C59"),
    text_disabled=hex_rgba("#9AA1AD"),
    accent=hex_rgba("#2F5FD6"),
    accent_hover=hex_rgba("#3A6BDE"),
    accent_active=hex_rgba("#2650B8"),
    accent_text=hex_rgba("#173F9E"),
    success=hex_rgba("#177049"),
    warning=hex_rgba("#8F5000"),
    danger=hex_rgba("#C42B3C"),
    info=hex_rgba("#146E9E"),
    nav_selected_bg=hex_rgba("#2F5FD6", 0.12),
    nav_hover_bg=(0.0, 0.0, 0.0, 0.06),
)

# High Contrast: every ratio clears AAA, not just AA. text_primary 21.0/19.5:1,
# text_secondary 15.9/14.8:1, text_disabled 5.3:1 (exempt, still high),
# black-on-accent (yellow) 14.4/15.5/11.6:1, accent_text 14.4:1, status
# 15.5/10.6/7.6/13.7:1. Follows Windows' own High Contrast convention:
# black/white base + saturated yellow/green/red/cyan. Surfaces near-opaque
# (0.97 alpha) since translucency works against maximizing contrast.
HIGH_CONTRAST = Theme(
    name="high_contrast",
    display_name="High Contrast (AAA)",
    bg_base=hex_rgba("#000000"),
    bg_sidebar=hex_rgba("#000000"),
    bg_popup=hex_rgba("#000000", 0.99),
    bg_card=hex_rgba("#0D0D0D", 0.97),
    bg_card_hover=hex_rgba("#1A1A1A", 0.97),
    bg_input=hex_rgba("#000000"),
    bg_input_hover=hex_rgba("#141414"),
    bg_input_active=hex_rgba("#1F1F1F"),
    border=hex_rgba("#808080", 0.90),
    border_strong=hex_rgba("#FFFFFF", 1.0),
    text_primary=hex_rgba("#FFFFFF"),
    text_secondary=hex_rgba("#E0E0E0"),
    text_disabled=hex_rgba("#808080"),
    accent=hex_rgba("#FFD100"),
    accent_hover=hex_rgba("#FFDB4D"),
    accent_active=hex_rgba("#E6BC00"),
    accent_text=hex_rgba("#FFD100"),
    success=hex_rgba("#00FF66"),
    warning=hex_rgba("#FFA500"),
    danger=hex_rgba("#FF6B6B"),
    info=hex_rgba("#00E5FF"),
    # Higher alpha than other themes' 0.16 -- the subtle tint that works for
    # a glassmorphism theme isn't visible enough on its own for a theme
    # whose whole purpose is maximum clarity.
    nav_selected_bg=hex_rgba("#FFD100", 0.30),
    nav_hover_bg=(1.0, 1.0, 1.0, 0.10),
)

# Color Cycle -- animated theme: the whole palette recolors with the live
# hue (status colors stay fixed, so "danger" never drifts through green).
#
# Each non-status field's RGB is solved per frame via binary search
# (_cycle_role_color) for the HSV value that makes its WCAG luminance match
# DARK's real value for that field exactly, at whatever hue -- not a fixed
# (S, V) band. That precision matters: HSV's V is peak-channel brightness,
# not perceptual luminance, and blue (weighted only 0.0722 in WCAG vs.
# green's 0.7152) can't reach as high a luminance as other hues at the same
# V, so a fixed-band approach silently fails at blue for accent_text.
# Per-role saturation (_CYCLE_ROLE_SAT) is tuned to each role's own
# feasibility/contrast ceiling, not a shared value -- accent-family roles can
# push saturation to their max since it only makes them darker (which helps
# white-text contrast); accent_text is foreground text, so its ceiling is
# capped where its own contrast against the background starts eroding
# (sat=0.50, solved directly, not eyeballed). Worst case across a full
# 360-step hue sweep: text_primary 15.72/13.12:1 (AAA), text_secondary
# 8.70/7.26:1 (AAA), white-on-accent family 4.78/4.58/5.97:1 (AA),
# accent_text 5.53/4.61:1 (AA). Holds for any hue, not just shipped defaults.
#
# COLOR_CYCLE below is a placeholder never applied directly to the live
# style -- its accent fields are dummies. shell.py resolves the real,
# live Theme every frame via resolve_color_cycle_theme() and applies that
# instead. The placeholder only exists so THEMES/get_theme()/theme_names()
# work unmodified for the picker in panels/settings.py.
COLOR_CYCLE = dataclasses.replace(
    DARK,
    name="color_cycle",
    display_name="Color Cycle",
)

THEMES: Dict[str, Theme] = {
    t.name: t for t in (DARK, VIOLET, EMBER, SLATE, DAYLIGHT, HIGH_CONTRAST, COLOR_CYCLE)
}


def get_theme(name: str) -> Theme:
    """Look up a theme by its `name` key, falling back to DARK if unknown."""
    return THEMES.get(name, DARK)


def theme_names() -> list:
    return list(THEMES.keys())


def apply_theme(theme: Theme) -> None:
    """Push `theme` onto the live ImGui style. Cheap enough to call once per
    frame; every field is set fresh from `theme` each time so nothing
    compounds. No in-app UI-scale multiplier -- relies on Hello ImGui/
    Windows' own DPI scaling instead.
    """
    style = imgui.get_style()
    t = theme

    # --- metrics (unscaled base, always reset from theme data) ---
    style.window_rounding = t.window_rounding
    style.child_rounding = t.card_rounding
    style.popup_rounding = t.card_rounding
    style.frame_rounding = t.frame_rounding
    style.grab_rounding = t.grab_rounding
    style.scrollbar_rounding = t.scrollbar_rounding
    style.tab_rounding = t.frame_rounding
    style.window_border_size = t.border_size
    style.child_border_size = t.border_size
    style.popup_border_size = t.border_size
    style.frame_border_size = 0.0
    style.window_padding = imgui.ImVec2(*t.window_padding)
    style.frame_padding = imgui.ImVec2(*t.frame_padding)
    style.item_spacing = imgui.ImVec2(*t.item_spacing)
    style.item_inner_spacing = imgui.ImVec2(*t.item_inner_spacing)
    style.indent_spacing = t.indent_spacing

    # --- colors ---
    Col = imgui.Col_
    color_map = {
        Col.text: t.text_primary,
        Col.text_disabled: t.text_disabled,
        Col.window_bg: t.bg_base,
        Col.child_bg: (0.0, 0.0, 0.0, 0.0),  # cards opt in to bg_card explicitly, see widgets.card()
        Col.popup_bg: t.bg_popup,
        Col.border: t.border,
        Col.border_shadow: (0.0, 0.0, 0.0, 0.0),
        Col.frame_bg: t.bg_input,
        Col.frame_bg_hovered: t.bg_input_hover,
        Col.frame_bg_active: t.bg_input_active,
        Col.title_bg: t.bg_sidebar,
        Col.title_bg_active: t.bg_sidebar,
        Col.title_bg_collapsed: t.bg_sidebar,
        Col.menu_bar_bg: t.bg_sidebar,
        Col.scrollbar_bg: (0.0, 0.0, 0.0, 0.0),
        Col.scrollbar_grab: t.border_strong,
        Col.scrollbar_grab_hovered: t.accent,
        Col.scrollbar_grab_active: t.accent_active,
        Col.check_mark: t.accent_text,
        Col.slider_grab: t.accent,
        Col.slider_grab_active: t.accent_active,
        Col.button: t.accent,
        Col.button_hovered: t.accent_hover,
        Col.button_active: t.accent_active,
        Col.header: t.nav_selected_bg,
        Col.header_hovered: t.nav_hover_bg,
        Col.header_active: t.nav_selected_bg,
        Col.separator: t.border,
        Col.separator_hovered: t.accent,
        Col.separator_active: t.accent_active,
        Col.resize_grip: t.border_strong,
        Col.resize_grip_hovered: t.accent,
        Col.resize_grip_active: t.accent_active,
        Col.input_text_cursor: t.text_primary,
        Col.tab: t.bg_sidebar,
        Col.tab_hovered: t.nav_hover_bg,
        Col.tab_selected: t.bg_card,
        Col.tab_selected_overline: t.accent,
        Col.tab_dimmed: t.bg_sidebar,
        Col.tab_dimmed_selected: t.bg_card,
        Col.tab_dimmed_selected_overline: t.border,
        Col.docking_preview: t.nav_selected_bg,
        Col.docking_empty_bg: t.bg_base,
        Col.plot_lines: t.accent_text,
        Col.plot_lines_hovered: t.accent,
        Col.plot_histogram: t.accent,
        Col.plot_histogram_hovered: t.accent_hover,
        Col.table_header_bg: t.bg_sidebar,
        Col.table_border_strong: t.border_strong,
        Col.table_border_light: t.border,
        Col.table_row_bg: (0.0, 0.0, 0.0, 0.0),
        Col.table_row_bg_alt: (1.0, 1.0, 1.0, 0.02),
        Col.text_link: t.accent_text,
        Col.text_selected_bg: t.nav_selected_bg,
        Col.tree_lines: t.border,
        Col.drag_drop_target: t.accent,
        Col.drag_drop_target_bg: t.nav_selected_bg,
        Col.unsaved_marker: t.warning,
        Col.nav_cursor: t.accent,
        Col.nav_windowing_highlight: t.accent,
        Col.nav_windowing_dim_bg: (0.0, 0.0, 0.0, 0.5),
        Col.modal_window_dim_bg: (0.0, 0.0, 0.0, 0.55),
    }
    for col, rgba in color_map.items():
        style.set_color_(int(col), rgba)


# ---------------------------------------------------------------------------
# Color Cycle: live full-palette resolution (see COLOR_CYCLE's comment
# block above for the exact-luminance-solve rationale).
# ---------------------------------------------------------------------------


def _luminance(rgba: RGBA) -> float:
    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgba[0], rgba[1], rgba[2]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


# Saturation per role, grouped into bands. The luminance solve below
# guarantees brightness; saturation only bounds whether a hue can reach that
# target at all (see blue/accent_text note above).
_CYCLE_ROLE_SAT: Dict[str, float] = {
    "bg_base": 0.30,
    "bg_sidebar": 0.32,
    "bg_popup": 0.30,
    "bg_card": 0.26,
    "bg_card_hover": 0.26,
    "bg_input": 0.28,
    "bg_input_hover": 0.28,
    "bg_input_active": 0.28,
    "border": 0.14,
    "border_strong": 0.12,
    "text_primary": 0.05,
    "text_secondary": 0.08,
    "text_disabled": 0.06,
    "accent": 0.92,
    "accent_hover": 0.92,
    "accent_active": 0.95,
    "accent_text": 0.50,  # lower than the rest -- see the blue-feasibility note above
}

# Target luminance per role, computed from DARK at import time so it can't
# drift out of sync if DARK's palette changes.
_CYCLE_TARGET_LUM: Dict[str, float] = {name: _luminance(getattr(DARK, name)) for name in _CYCLE_ROLE_SAT}


def _cycle_role_color(role: str, hue: float) -> RGBA:
    """Color for `role` at `hue`: alpha from DARK's own value (keeps
    translucent surfaces translucent), RGB solved via binary search so WCAG
    luminance matches DARK's value for that field exactly, at any hue."""
    sat = _CYCLE_ROLE_SAT[role]
    target = _CYCLE_TARGET_LUM[role]
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if _luminance(colorsys.hsv_to_rgb(hue, sat, mid) + (1.0,)) < target:
            lo = mid
        else:
            hi = mid
    r, g, b = colorsys.hsv_to_rgb(hue, sat, (lo + hi) / 2.0)
    alpha = getattr(DARK, role)[3]
    return (r, g, b, alpha)


def color_cycle_phase(period_sec: float, elapsed_sec: float) -> float:
    """Sine-eased 0..1 phase for the back-and-forth drift -- sine (not a
    triangle wave) so direction reversals ease smoothly, like a breathing
    LED effect. `elapsed_sec` is caller-owned (shell.py) so Reduce Motion
    can freeze it without this function knowing about that setting.
    """
    period = max(period_sec, 0.001)  # guard against 0/negative period
    angle = (2.0 * math.pi * elapsed_sec / period) - (math.pi / 2.0)
    return (math.sin(angle) + 1.0) / 2.0


def resolve_color_cycle_theme(color_a: RGBA, color_b: RGBA, period_sec: float, elapsed_sec: float) -> Theme:
    """Build a fully-populated Theme for the current instant of the Color
    Cycle animation. `color_a`/`color_b` interpolate to a seed color whose
    hue drives every non-status field via `_cycle_role_color()`. Status
    colors and metrics stay DARK's fixed values. Called once per frame from
    shell.py, before `apply_theme()`.
    """
    phase = color_cycle_phase(period_sec, elapsed_sec)
    seed_r = color_a[0] + (color_b[0] - color_a[0]) * phase
    seed_g = color_a[1] + (color_b[1] - color_a[1]) * phase
    seed_b = color_a[2] + (color_b[2] - color_a[2]) * phase
    hue, _sat, _val = colorsys.rgb_to_hsv(seed_r, seed_g, seed_b)

    fields = {role: _cycle_role_color(role, hue) for role in _CYCLE_ROLE_SAT}
    fields["nav_selected_bg"] = (fields["accent"][0], fields["accent"][1], fields["accent"][2], 0.16)

    return dataclasses.replace(DARK, name="color_cycle", display_name="Color Cycle", **fields)
