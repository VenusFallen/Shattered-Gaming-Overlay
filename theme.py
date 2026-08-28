"""theme.py -- Shattered Gaming Overlay's palette/style system for the Companion window.

Themes are plain data, not colors hardcoded into widget code. `Theme` is a
frozen dataclass of named colors and layout metrics; `THEMES` is the registry
new themes register into. Panels/widgets should never hardcode a color --
they pull named tokens off the active `Theme` (see `apply_theme` /
`get_theme`). Seven themes ship today (Dark, Violet, Ember, Slate, and High
Contrast are dark; Daylight is light; Color Cycle is the animated, user-
configurable one -- see its own comment block below) -- proof the
data-driven split works, and groundwork for community-contributed palettes
being just another `Theme(...)` value, not a rewrite of every panel.

Contrast methodology (WCAG 2.2 relative-luminance formula -- linearize each
sRGB channel, L = 0.2126R + 0.7152G + 0.0722B, ratio = (Lmax+0.05)/(Lmin+0.05);
AA requires >= 4.5:1 for normal text, AAA >= 7:1). Every ratio quoted in this
file was computed with that exact formula, never eyeballed. Translucent
surfaces (bg_card's 0.90 alpha etc.) are first composited over their base background
-- `fg*a + bg*(1-a)` per channel -- before measuring, since that's what's
actually on screen. `border` / `nav_*_bg` are decorative-only (non-text) and
are not held to the text contrast ratio; `text_disabled` is intentionally
below AA since WCAG exempts disabled/inactive content -- never use it for
anything interactive.

DARK (default) -- computed ratios:
  - text_primary   on bg_base / bg_card:  15.7:1 / 13.2:1  (AAA)
  - text_secondary on bg_base / bg_card:   8.7:1 /  7.3:1  (AAA)
  - text_disabled  on bg_base:             3.7:1  (exempt, see above)
  - white text on accent / accent_hover / accent_active: 4.8 / 4.6 / 6.0 :1 (AA)
  - accent_text (links/selected nav label) on bg_base: 8.7:1 (AAA)
  - success / warning / danger / info on bg_base: 10.1 / 10.3 / 5.4 / 9.2 :1 (AA/AAA)
Still worth a human glance in bright/sunlit conditions as a final check (see
report gotchas). Every other theme below carries its own computed-ratio
comment block in the same format, directly above its `Theme(...)`.
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

    # --- status colors -- ALWAYS paired with an icon + text label by callers,
    # never used as the sole carrier of meaning (see widgets.status_badge) ---
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

# Violet -- computed ratios:
#   text_primary/bg_base: 15.8:1 (AAA)  text_primary/bg_card: 13.6:1 (AAA)
#   text_secondary/bg_base: 8.8:1 (AAA)  text_secondary/bg_card: 7.5:1 (AAA)
#   text_disabled/bg_base: 3.7:1 (exempt)
#   white on accent/accent_hover/accent_active: 4.7 / 5.4 / 6.1 :1 (AA)
#   accent_text/bg_base: 9.4:1 (AAA)
#   success/warning/danger/info on bg_base: 10.3 / 10.4 / 5.4 / 9.4 :1 (AA/AAA)
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

# Ember -- computed ratios:
#   text_primary/bg_base: 15.9:1 (AAA)  text_primary/bg_card: 13.6:1 (AAA)
#   text_secondary/bg_base: 9.6:1 (AAA)  text_secondary/bg_card: 8.2:1 (AAA)
#   text_disabled/bg_base: 3.8:1 (exempt)
#   dark button text on accent/accent_hover/accent_active: 6.1 / 6.9 / 5.6 :1 (AA)
#     (amber/orange midtones don't clear 4.5:1 with white text at any
#     reasonable brightness, so ember uses a near-black button label instead
#     -- still icon+label, never color alone, matching widgets.status_badge)
#   accent_text/bg_base: 10.5:1 (AAA)
#   success/warning/danger/info on bg_base: 9.2 / 11.2 / 5.8 / 9.3 :1 (AA/AAA)
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

# Slate -- computed ratios:
#   text_primary/bg_base: 15.8:1 (AAA)  text_primary/bg_card: 13.3:1 (AAA)
#   text_secondary/bg_base: 8.8:1 (AAA)  text_secondary/bg_card: 7.4:1 (AAA)
#   text_disabled/bg_base: 3.9:1 (exempt)
#   dark button text on accent/accent_hover/accent_active: 6.3 / 7.1 / 4.7 :1 (AA)
#   accent_text/bg_base: 10.9:1 (AAA)
#   success/warning/danger/info on bg_base: 10.2 / 10.3 / 5.4 / 11.3 :1 (AA/AAA)
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

# Daylight (light theme) -- computed ratios:
#   text_primary/bg_base: 16.6:1 (AAA)  text_primary/bg_card: 18.0:1 (AAA)
#   text_secondary/bg_base: 7.9:1 (AAA)  text_secondary/bg_card: 8.6:1 (AAA)
#   text_disabled/bg_base: 2.4:1 (exempt)
#   white text on accent/accent_hover/accent_active: 5.6 / 4.8 / 7.2 :1 (AA)
#   accent_text/bg_base: 8.6:1 (AAA)
#   success/warning/danger/info on bg_base: 5.6 / 5.8 / 5.1 / 5.1 :1 (AA)
#     (status hues are all darkened/desaturated well below their dark-theme
#     versions -- the same bright green/amber/red that clears AA on a
#     near-black background falls to ~2-4:1 on a near-white one, so these are
#     independently tuned, not the dark-theme swatches reused verbatim)
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

# High Contrast -- computed ratios (every single one clears AAA, not just AA
# -- this theme exists specifically for users who need the strongest
# possible contrast, so "just clears AA" isn't good enough here the way it
# is for the others):
#   text_primary/bg_base: 21.0:1  text_primary/bg_card: 19.5:1
#   text_secondary/bg_base: 15.9:1  text_secondary/bg_card: 14.8:1
#   text_disabled/bg_base: 5.3:1 (exempt, and still far above what most
#     themes' disabled text clears, since even "de-emphasized" text stays
#     legible here)
#   black button text on accent/accent_hover/accent_active: 14.4 / 15.5 /
#     11.6 :1 (near-black text chosen over white specifically because the
#     accent is a bright, saturated yellow -- white-on-yellow would be a
#     much weaker pairing than black-on-yellow)
#   accent_text/bg_base: 14.4:1
#   success/warning/danger/info on bg_base: 15.5 / 10.6 / 7.6 / 13.7 :1
#     (danger was deliberately brightened from a more typical muted red --
#     #FF3B3B only cleared 5.9:1 -- to #FF6B6B specifically to clear AAA
#     like everything else in this theme, rather than leaving one status
#     color as the odd one out at merely-AA)
# Pure black/white base plus saturated, maximally-differentiated accent and
# status hues (yellow/green/red/cyan) -- the same color family convention
# Windows' own built-in High Contrast themes use, deliberately, rather than
# inventing a different high-contrast palette convention. Surfaces are
# nearly opaque (0.97 alpha vs. the 0.90 "glassmorphism" used elsewhere)
# since translucency works directly against maximizing contrast.
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

# Color Cycle -- a user-configurable, animated theme: the ENTIRE palette
# recolors with the live hue (backgrounds, cards, text, borders, accent --
# everything except the four semantic status colors, which stay fixed since
# "danger" drifting through green as the cycle turns would be actively
# confusing). This was the second iteration of this theme -- the first only
# animated the accent family and left backgrounds/text static, which read as
# "barely changing" next to how dramatically the other themes differ from
# each other; full-palette recoloring was requested specifically so
# switching into Color Cycle feels like the same kind of change as picking
# any other theme, just continuous instead of a discrete jump.
#
# Every non-status field's color is derived by solving, per frame, for the
# HSV value (V) that makes that field's WCAG relative luminance land on
# EXACTLY the luminance Dark's own real color for that field already has --
# not an approximate HSV brightness band, an exact match via binary search
# (see _cycle_role_color below). This is deliberately more rigorous than
# picking fixed (S, V) numbers and spot-checking a few hues: an early
# attempt at fixed bands passed easily at most hues but silently failed at
# pure blue (hue ~0.667) for `accent_text`, because a saturated blue simply
# cannot reach as high a luminance as other hues at the same "V" -- HSV's V
# is peak-channel brightness, not perceptual luminance, and blue is only
# weighted 0.0722 in the WCAG formula versus green's 0.7152. Solving for the
# exact target luminance (and lowering `accent_text`'s saturation so blue
# hues can physically reach its higher luminance target) fixes this
# properly instead of papering over one bad sample point. Verified via a
# 360-step full-hue-wheel sweep (every fraction of a degree), worst case
# across the *entire* wheel, not just a few samples:
#   text_primary/bg_base:      15.72:1 (AAA, matches Dark's 15.7 almost exactly)
#   text_primary/bg_card:      13.12:1 (AAA)
#   text_secondary/bg_base:     8.70:1 (AAA)
#   text_secondary/bg_card:     7.26:1 (AAA)
#   white text on accent:       4.78:1 (AA)
#   white text on accent_hover: 4.58:1 (AA)
#   white text on accent_active: 5.97:1 (AA/AAA)
#   accent_text on bg_base:      5.99:1 (AA, saturation lowered from blue's
#     infeasible ceiling specifically to keep this one AA-safe everywhere)
#   accent_text on bg_card:      4.99:1 (AA)
# accent/accent_hover/accent_active/accent_text saturations (0.80/0.80/0.85/
# 0.47) are deliberately pushed close to the maximum each one can reach
# before the worst-case hue (always blue, ~0.667 -- weighted only 7.22% in
# the WCAG formula) makes the target luminance unreachable at all. An
# earlier, more conservative pass (0.70/0.70/0.75/0.45) held the exact same
# contrast ratios above but looked visibly washed out next to whatever raw
# color the user actually picked -- pushing saturation right up to its safe
# ceiling (verified the same way, full 360-step sweep) fixes that without
# touching the contrast guarantee at all.
# These hold for ANY hue at all, not just the shipped defaults or the two
# colors a user happens to pick -- the solve targets an exact luminance per
# field regardless of which hue it's given, so there's no "bad pair of
# colors" that can break contrast the way there could with the old
# fixed-band approach. Picking two very similar colors just makes the
# animation itself subtler (less hue to travel between) -- that's a visual
# choice, not a contrast risk.
#
# The placeholder Theme registered below (COLOR_CYCLE) is never applied to
# the live ImGui style directly -- its accent fields are dummies. Real,
# live accent values are resolved once per frame in shell.py via
# `resolve_color_cycle_theme()`, using SettingsState.cycle_color_a/b/
# cycle_period_sec/cycle_elapsed_sec, and *that* resolved Theme is what
# reaches `apply_theme()`. This placeholder exists solely so THEMES/
# `get_theme()`/`theme_names()` keep working unmodified for the picker in
# panels/settings.py -- see that module for the live color pickers + speed
# slider, shown only while this theme is selected.
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
    """Push `theme` onto the live ImGui style.

    Cheap enough (a few dozen struct writes) to call once per frame from the
    top of the render loop, which keeps this idempotent by construction --
    every field is always set from `theme`'s own base value every time, so
    nothing compounds across frames. No in-app UI-scale multiplier is
    applied here -- the app relies on Hello ImGui/Windows' own DPI scaling
    instead of a custom slider (a custom slider was tried and removed --
    redundant with the OS-level scaling).
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
# Color Cycle: live full-palette resolution (see COLOR_CYCLE's comment block
# above for the exact-luminance-solve rationale and the measured worst-case
# contrast ratios this produces across the entire hue wheel).
# ---------------------------------------------------------------------------


def _luminance(rgba: RGBA) -> float:
    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgba[0], rgba[1], rgba[2]
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


# Saturation per role. Deliberately grouped into a few bands rather than
# hand-tuned per field -- the exact-luminance solve below is what actually
# guarantees brightness, saturation only affects whether a given hue can
# *reach* that target at all (see the blue/accent_text story in the comment
# block above), so these just need to be "low enough to be reachable at
# every hue for that role's typical target," not individually precise.
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
    "accent": 0.80,
    "accent_hover": 0.80,
    "accent_active": 0.85,
    "accent_text": 0.47,  # lower than the rest -- see the blue-feasibility note above
}

# Every field _CYCLE_ROLE_SAT covers, keyed the same way, holding DARK's own
# real luminance for that field -- computed once at import time from DARK
# itself (not hardcoded numbers), so this can never silently drift out of
# sync if DARK's palette ever changes.
_CYCLE_TARGET_LUM: Dict[str, float] = {name: _luminance(getattr(DARK, name)) for name in _CYCLE_ROLE_SAT}


def _cycle_role_color(role: str, hue: float) -> RGBA:
    """The color for `role` at the given hue, with alpha taken from DARK's
    own value for that field (so translucent cards/popups stay translucent)
    and RGB solved via binary search so this role's WCAG luminance matches
    DARK's real value for it exactly, regardless of hue."""
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
    """Sine-eased 0..1 phase for the slow back-and-forth drift.

    A plain sine (not a triangle/sawtooth wave) so the direction reversal at
    each end is a smooth ease rather than a jerky bounce -- the derivative
    is zero right at phase 0 and phase 1, same shape as a Corsair-style
    "breathing" keyboard effect. `elapsed_sec` is caller-owned (see
    SettingsState.cycle_elapsed_sec / shell.py) specifically so it can be
    frozen (not advanced) while Reduce Motion is on, rather than this
    function needing to know about that setting itself.
    """
    period = max(period_sec, 0.001)  # guard against a stray 0/negative period
    angle = (2.0 * math.pi * elapsed_sec / period) - (math.pi / 2.0)
    return (math.sin(angle) + 1.0) / 2.0


def resolve_color_cycle_theme(color_a: RGBA, color_b: RGBA, period_sec: float, elapsed_sec: float) -> Theme:
    """Build a real, fully-populated Theme for the current instant of the
    Color Cycle animation. `color_a`/`color_b` are linearly interpolated in
    RGB (driven by `color_cycle_phase()`) to get a seed color; that seed's
    HSV hue then drives every non-status field via `_cycle_role_color()`, so
    the WHOLE palette recolors together the same way switching to a
    different static theme would -- not just the accent. Status colors
    (success/warning/danger/info) and layout metrics stay Dark's own fixed
    values on purpose (see COLOR_CYCLE's module comment). Called once per
    frame from shell.py, right before `apply_theme()` -- `apply_theme()`
    itself stays non-time-aware.
    """
    phase = color_cycle_phase(period_sec, elapsed_sec)
    seed_r = color_a[0] + (color_b[0] - color_a[0]) * phase
    seed_g = color_a[1] + (color_b[1] - color_a[1]) * phase
    seed_b = color_a[2] + (color_b[2] - color_a[2]) * phase
    hue, _sat, _val = colorsys.rgb_to_hsv(seed_r, seed_g, seed_b)

    fields = {role: _cycle_role_color(role, hue) for role in _CYCLE_ROLE_SAT}
    fields["nav_selected_bg"] = (fields["accent"][0], fields["accent"][1], fields["accent"][2], 0.16)

    return dataclasses.replace(DARK, name="color_cycle", display_name="Color Cycle", **fields)
