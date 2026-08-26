"""theme.py -- Shattered Gaming Overlay's palette/style system for the Companion window.

Themes are plain data, not colors hardcoded into widget code. `Theme` is a
frozen dataclass of named colors and layout metrics; `THEMES` is the registry
new themes register into. Panels/widgets should never hardcode a color --
they pull named tokens off the active `Theme` (see `apply_theme` /
`get_theme`). Four themes ship today (three dark, one light) -- proof the
data-driven split works, and groundwork for a future AAA/"High Contrast" mode
or community-contributed palettes being just another `Theme(...)` value,
not a rewrite of every panel.

Contrast methodology (WCAG 2.2 relative-luminance formula -- linearize each
sRGB channel, L = 0.2126R + 0.7152G + 0.0722B, ratio = (Lmax+0.05)/(Lmin+0.05);
AA requires >= 4.5:1 for normal text, AAA >= 7:1). Every ratio quoted in this
file was computed with that exact formula (see the throwaway calculator used
while designing these palettes), never eyeballed. Translucent surfaces
(bg_card's 0.90 alpha etc.) are first composited over their base background
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

import dataclasses
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

    # --- metrics (unscaled -- ui_scale is applied on top via Style.scale_all_sizes) ---
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

    # --- optional brand gradient (decorative only, e.g. the sidebar header
    # accent bar -- see shell.py) -- None on every theme except one that
    # actually wants a rendered two-color gradient rather than a flat accent.
    # Dear ImGui's style colors are flat per-element; this is the deliberate,
    # narrow exception for the one spot a real gradient earns its place,
    # rather than reworking every widget's color pipeline for it.
    accent_gradient_start: RGBA | None = None
    accent_gradient_end: RGBA | None = None


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

# Gradient -- computed ratios:
#   text_primary/bg_base: 16.1:1 (AAA)  text_primary/bg_card: 14.2:1 (AAA)
#   text_secondary/bg_base: 8.6:1 (AAA)  text_secondary/bg_card: 7.6:1 (AAA)
#   text_disabled/bg_base: 3.6:1 (exempt)
#   white text on accent(blue)/accent_hover(violet)/accent_active(red):
#     5.5 / 6.2 / 5.2 :1 (AA -- accent_active's red was deliberately darkened
#     from the icon's brighter #E03040 to #D02A3A specifically to clear AA
#     with margin; the brighter red is still used for the purely decorative,
#     non-text gradient bar below, which isn't held to a text contrast ratio)
#   accent_text/bg_base: 8.7:1 (AAA)
#   success/warning/danger/info on bg_base: 10.3 / 10.4 / 5.4 / 9.4 :1 (AA/AAA)
# accent progresses blue (rest) -> violet (hover) -> red (active) using the
# same red/blue family as the app icon, rather than one flat accent hue.
GRADIENT = Theme(
    name="gradient",
    display_name="Gradient (Red/Blue)",
    bg_base=hex_rgba("#15131C"),
    bg_sidebar=hex_rgba("#100E15"),
    bg_popup=hex_rgba("#1A1721", 0.98),
    bg_card=hex_rgba("#251F2C", 0.90),
    bg_card_hover=hex_rgba("#2B2433", 0.94),
    bg_input=hex_rgba("#1C1822"),
    bg_input_hover=hex_rgba("#231E2A"),
    bg_input_active=hex_rgba("#28222F"),
    border=hex_rgba("#372F42", 0.80),
    border_strong=hex_rgba("#4A4058", 1.0),
    text_primary=hex_rgba("#F2EEF7"),
    text_secondary=hex_rgba("#B7AEC4"),
    text_disabled=hex_rgba("#726A80"),
    accent=hex_rgba("#385CE6"),
    accent_hover=hex_rgba("#8C4693"),
    accent_active=hex_rgba("#D02A3A"),
    accent_text=hex_rgba("#C9A0FF"),
    success=hex_rgba("#3DDC84"),
    warning=hex_rgba("#F5B942"),
    danger=hex_rgba("#F2555A"),
    info=hex_rgba("#5FC6E8"),
    nav_selected_bg=hex_rgba("#8C4693", 0.16),
    nav_hover_bg=(1.0, 1.0, 1.0, 0.05),
    accent_gradient_start=hex_rgba("#E03040"),  # matches the app icon's red
    accent_gradient_end=hex_rgba("#385CE6"),    # matches the app icon's blue
)

THEMES: Dict[str, Theme] = {t.name: t for t in (DARK, VIOLET, EMBER, SLATE, DAYLIGHT, GRADIENT)}


def get_theme(name: str) -> Theme:
    """Look up a theme by its `name` key, falling back to DARK if unknown."""
    return THEMES.get(name, DARK)


def theme_names() -> list:
    return list(THEMES.keys())


def gradient_endpoints(theme: Theme) -> Tuple[RGBA, RGBA]:
    """(start, end) for the sidebar header's decorative gradient bar --
    falls back to a flat `theme.accent` (start == end) for every theme that
    doesn't define an explicit brand gradient, so the bar degrades to an
    ordinary accent-colored rule rather than needing a special case at the
    call site."""
    start = theme.accent_gradient_start if theme.accent_gradient_start is not None else theme.accent
    end = theme.accent_gradient_end if theme.accent_gradient_end is not None else theme.accent
    return start, end


def apply_theme(theme: Theme, ui_scale: float) -> None:
    """Push `theme` + `ui_scale` onto the live ImGui style.

    Cheap enough (a few dozen struct writes) to call once per frame from the
    top of the render loop, which keeps this idempotent by construction --
    every field is always set from `theme`'s own unscaled base value first,
    then scaled once, so nothing compounds across frames even while the user
    is live-dragging the UI-scale slider.
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

    scale = max(0.5, min(ui_scale, 3.0))
    style.scale_all_sizes(scale)
    # Text size: Dear ImGui 1.92's dynamic font scaling replaces the old
    # io.FontGlobalScale with Style.FontScaleMain for a user-controlled
    # global scale factor.
    style.font_scale_main = scale

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
