"""widgets.py -- shared, theme-driven UI helpers for the Companion window.

Every panel builds its layout out of these instead of calling raw ImGui
color/rounding pushes inline -- keeps "no hardcoded colors per panel" true in
practice, not just in theme.py.
"""

from __future__ import annotations

import contextlib
import webbrowser
from typing import Iterator, Optional, Tuple

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui
from imgui_bundle import imgui_toggle

from theme import Theme

# ---------------------------------------------------------------------------
# Layout: cards (the "thin translucency" panel surfaces)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def card(
    theme: Theme,
    str_id: str,
    size: Optional[Tuple[float, float]] = None,
    hovered: bool = False,
) -> Iterator[bool]:
    """A rounded, subtly translucent panel surface ('glass card').

    Usage:
        with widgets.card(theme, "remap-list") as visible:
            if visible:
                ... draw widgets ...
    """
    bg = theme.bg_card_hover if hovered else theme.bg_card
    imgui.push_style_color(imgui.Col_.child_bg, bg)
    imgui.push_style_color(imgui.Col_.border, theme.border)
    size_v = imgui.ImVec2(*size) if size else imgui.ImVec2(0, 0)
    child_flags = imgui.ChildFlags_.borders | imgui.ChildFlags_.always_use_window_padding
    # A height of 0 is meant as "hug my content" (every caller that wants a
    # fixed/scrollable height, e.g. window_select's 220px process list,
    # passes a real nonzero height instead) -- but plain BeginChild with
    # size.y == 0 actually means "fill the remaining space of the parent
    # window", not "auto-size to content". Without auto_resize_y, a
    # single-row card (e.g. one remap entry) silently stretched to fill the
    # entire rest of its parent pane, which pushed the parent's own cursor
    # past its available height by a few px and forced a spurious vertical
    # scrollbar on the parent -- confirmed live (parent's imgui.get_scroll_max_y()
    # was 0 before the card, >0 after) while chasing the sidebar nav bug.
    if size_v.y == 0:
        child_flags |= imgui.ChildFlags_.auto_resize_y
    visible = imgui.begin_child(str_id, size_v, child_flags)
    try:
        yield visible
    finally:
        imgui.end_child()
        imgui.pop_style_color(2)


def section_title(text: str) -> None:
    imgui.separator_text(text)


def muted_text(theme: Theme, text: str) -> None:
    imgui.text_colored(theme.text_secondary, text)


# ---------------------------------------------------------------------------
# Status: color is NEVER the only signal -- always icon + label together.
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "ok": fa.ICON_FA_CHECK_CIRCLE,
    "warn": fa.ICON_FA_EXCLAMATION_TRIANGLE,
    "error": fa.ICON_FA_TIMES_CIRCLE,
    "info": fa.ICON_FA_INFO_CIRCLE,
    "neutral": fa.ICON_FA_CIRCLE,
}


def status_badge(theme: Theme, level: str, label: str) -> None:
    """Render an icon+text status badge. `level` in
    {"ok", "warn", "error", "info", "neutral"}. Color reinforces the icon
    and label -- it never stands alone as the only way to tell states apart.
    """
    color_map = {
        "ok": theme.success,
        "warn": theme.warning,
        "error": theme.danger,
        "info": theme.info,
        "neutral": theme.text_secondary,
    }
    color = color_map.get(level, theme.text_secondary)
    icon = _STATUS_ICONS.get(level, _STATUS_ICONS["neutral"])
    imgui.text_colored(color, f"{icon}  {label}")


# ---------------------------------------------------------------------------
# Toggle: an accessible on/off switch with a visible, non-color label.
# ---------------------------------------------------------------------------


def labeled_toggle(
    theme: Theme,
    label: str,
    value: bool,
    reduce_motion: bool = False,
    tooltip: Optional[str] = None,
) -> Tuple[bool, bool]:
    """Draw `[toggle switch]  Label` and return (changed, new_value).

    State is conveyed by knob position (a spatial cue, not a color-only one)
    plus the plain-text label placed after it -- so state is never carried
    by color alone. An earlier version also drew literal 1/0 glyphs on the
    knob (ToggleFlags_.a11y) as a belt-and-suspenders accessibility signal,
    but that duplicated what position already conveys and looked like a
    rendering artifact -- removed.
    """
    # Note: imgui_toggle.ToggleConstants exists in the bundled .pyi stub but
    # is not actually exported by the compiled module at runtime (confirmed
    # by introspection) -- 0.1s is that constant's own documented default,
    # inlined here since there's nothing to import it from.
    _DEFAULT_ANIMATION_DURATION = 0.1

    flags = imgui_toggle.ToggleFlags_.none
    if not reduce_motion:
        flags |= imgui_toggle.ToggleFlags_.animated
    animation_duration = 0.0 if reduce_motion else _DEFAULT_ANIMATION_DURATION
    imgui.push_id(label)
    changed, new_value = imgui_toggle.toggle("##toggle", value, flags, animation_duration)
    imgui.pop_id()
    imgui.same_line()
    imgui.text(label)
    if tooltip and imgui.is_item_hovered():
        imgui.set_tooltip(tooltip)
    return changed, new_value


# ---------------------------------------------------------------------------
# Color picker: swatch + label, opens a hex-only popup (no RGB sliders, no
# alpha) with an explicit Close button.
# ---------------------------------------------------------------------------


def hex_color_picker(
    theme: Theme, str_id: str, label: str, rgba: Tuple[float, float, float, float]
) -> Tuple[bool, Tuple[float, float, float, float]]:
    """A themed color swatch + label; clicking the swatch opens a popup with
    a single large visual picker and one hex input field -- deliberately no
    R/G/B number fields and no alpha/transparency control, since the default
    ImGui color editor's RGB sliders + alpha bar are more than this app ever
    needs. Also includes an explicit Close
    button in the popup: Dear ImGui's default popup only dismisses on an
    outside click, which wasn't discoverable enough on its own.

    Callers still pass/receive a 4-tuple (alpha always forced to 1.0 on the
    way out) rather than narrowing to RGB -- existing RGBA-typed consumers
    (e.g. OverlayState.crosshair.color, hud_overlay.py's renderer) don't
    need to change, this widget just never exposes the 4th channel.
    """
    imgui.push_id(str_id)
    swatch_col = imgui.ImVec4(rgba[0], rgba[1], rgba[2], 1.0)
    if imgui.color_button("##swatch", swatch_col, imgui.ColorEditFlags_.no_alpha, imgui.ImVec2(28, 20)):
        imgui.open_popup("picker")
    imgui.same_line()
    imgui.text(label)

    changed = False
    new_rgb = [rgba[0], rgba[1], rgba[2]]
    if imgui.begin_popup("picker"):
        flags = (
            imgui.ColorEditFlags_.display_hex
            | imgui.ColorEditFlags_.no_alpha
            | imgui.ColorEditFlags_.no_options
            | imgui.ColorEditFlags_.no_side_preview
        )
        picked, new_rgb = imgui.color_picker3("##pickerwidget", new_rgb, flags)
        changed = changed or picked
        imgui.spacing()
        if imgui.button("Close", imgui.ImVec2(-1, 0)):
            imgui.close_current_popup()
        imgui.end_popup()

    imgui.pop_id()
    return changed, (new_rgb[0], new_rgb[1], new_rgb[2], 1.0)


# ---------------------------------------------------------------------------
# Screen position picker: a mini "monitor" with six clickable spots inside it
# (Top/Middle/Bottom x Left/Right) -- replaces a plain dropdown for anything
# anchoring an overlay element to a screen position, so the user clicks
# roughly where they want it to appear instead of picking a text label.
# ---------------------------------------------------------------------------

SCREEN_POSITIONS = ("Top Left", "Top Right", "Middle Left", "Middle Right", "Bottom Left", "Bottom Right")
# Superset used where more granular placement is useful (e.g. two status
# badges that might otherwise collide) -- Stats HUD uses SCREEN_POSITIONS,
# Status Indicators uses this one. Order matters for tab/visual grouping.
SCREEN_POSITIONS_WITH_MIDDLES = (
    "Top Left",
    "Top Middle",
    "Top Right",
    "Middle Left",
    "Middle Right",
    "Bottom Left",
    "Bottom Middle",
    "Bottom Right",
)

# Fractional (x, y) center of each spot's marker within the outer "screen"
# rect -- inset from the true corners so each marker reads as a small box
# sitting inside the screen, not clipped against its bezel.
_SCREEN_POSITION_FRACTIONS = {
    "Top Left": (0.18, 0.22),
    "Top Middle": (0.50, 0.22),
    "Top Right": (0.82, 0.22),
    "Middle Left": (0.18, 0.50),
    "Middle Right": (0.82, 0.50),
    "Bottom Left": (0.18, 0.78),
    "Bottom Middle": (0.50, 0.78),
    "Bottom Right": (0.82, 0.78),
}

_SCREEN_BOX_SIZE = (168.0, 96.0)  # ~16:9, big enough for six comfortably-clickable spots
_SCREEN_MARKER_SIZE = (26.0, 16.0)


def screen_position_picker(
    theme: Theme, str_id: str, current: str, positions: tuple = SCREEN_POSITIONS
) -> str:
    """Draws a small rounded-rect "monitor" with clickable position markers
    inside it (one per entry in `positions`); returns the newly selected
    position name (or `current` unchanged if nothing was clicked this
    frame). Pass `SCREEN_POSITIONS` (6, the four corners + two middles) or
    `SCREEN_POSITIONS_WITH_MIDDLES` (8, adds Top/Bottom Middle -- useful
    where more granular placement matters, e.g. avoiding two badges
    colliding)."""
    imgui.push_id(str_id)
    draw_list = imgui.get_window_draw_list()
    u32 = lambda rgba: imgui.color_convert_float4_to_u32(imgui.ImVec4(*rgba))  # noqa: E731

    box_w, box_h = _SCREEN_BOX_SIZE
    pos = imgui.get_cursor_screen_pos()
    p_max = imgui.ImVec2(pos.x + box_w, pos.y + box_h)
    draw_list.add_rect_filled(pos, p_max, u32(theme.bg_input), rounding=6.0)
    draw_list.add_rect(pos, p_max, u32(theme.border_strong), rounding=6.0, thickness=1.5)

    new_value = current
    mk_w, mk_h = _SCREEN_MARKER_SIZE
    for name in positions:
        fx, fy = _SCREEN_POSITION_FRACTIONS[name]
        cx, cy = pos.x + box_w * fx, pos.y + box_h * fy
        mk_min = imgui.ImVec2(cx - mk_w / 2.0, cy - mk_h / 2.0)
        mk_max = imgui.ImVec2(cx + mk_w / 2.0, cy + mk_h / 2.0)
        selected = name == current

        imgui.set_cursor_screen_pos(mk_min)
        imgui.push_id(name)
        clicked = imgui.invisible_button("##spot", imgui.ImVec2(mk_w, mk_h))
        hovered = imgui.is_item_hovered()
        imgui.pop_id()

        if selected:
            draw_list.add_rect_filled(mk_min, mk_max, u32(theme.accent), rounding=3.0)
        else:
            fill = theme.bg_input_hover if hovered else theme.bg_input_active
            draw_list.add_rect_filled(mk_min, mk_max, u32(fill), rounding=3.0)
            draw_list.add_rect(mk_min, mk_max, u32(theme.border), rounding=3.0, thickness=1.0)

        if hovered:
            imgui.set_tooltip(name)
        if clicked:
            new_value = name

    imgui.set_cursor_screen_pos(imgui.ImVec2(pos.x, pos.y + box_h + 4.0))
    imgui.pop_id()
    return new_value


# ---------------------------------------------------------------------------
# Key bind button (used by Remapper + Macros via key_capture.py)
# ---------------------------------------------------------------------------


def hyperlink(theme: Theme, label: str, url: str) -> None:
    """Render `label` styled as a clickable hyperlink (accent color, hand
    cursor, underline on hover) that opens `url` in the OS default browser
    when clicked. Dear ImGui has no native hyperlink widget in this build --
    this is a Selectable sized to its text (not the "fill available width"
    -1 convention, which this imgui_bundle build takes literally rather than
    as "fill width" -- see the module-level gotcha notes) and re-themed to
    read as a link instead of a list row.
    """
    text_width = imgui.calc_text_size(label).x
    imgui.push_style_color(imgui.Col_.text, theme.accent_text)
    imgui.push_style_color(imgui.Col_.header, (0.0, 0.0, 0.0, 0.0))
    imgui.push_style_color(imgui.Col_.header_hovered, (0.0, 0.0, 0.0, 0.0))
    imgui.push_style_color(imgui.Col_.header_active, (0.0, 0.0, 0.0, 0.0))
    clicked, _ = imgui.selectable(f"{label}##link", False, size=imgui.ImVec2(text_width, 0))
    if imgui.is_item_hovered():
        imgui.set_mouse_cursor(imgui.MouseCursor_.hand)
        min_p = imgui.get_item_rect_min()
        max_p = imgui.get_item_rect_max()
        underline_y = max_p.y - 1
        imgui.get_window_draw_list().add_line(
            imgui.ImVec2(min_p.x, underline_y),
            imgui.ImVec2(max_p.x, underline_y),
            imgui.get_color_u32(theme.accent_text),
            1.0,
        )
    imgui.pop_style_color(4)
    if clicked:
        webbrowser.open(url)


def bind_button(theme: Theme, str_id: str, display_name: str, capturing: bool) -> bool:
    """A button that shows the current bind, or a distinct "listening" state
    while capturing. Returns True if clicked (caller starts/owns capture).

    The "listening" state is communicated via icon + text change, not color
    alone, so it reads correctly even for a user who can't distinguish the
    accent color from the default button color.
    """
    imgui.push_id(str_id)
    if capturing:
        imgui.push_style_color(imgui.Col_.button, theme.accent_active)
        imgui.push_style_color(imgui.Col_.button_hovered, theme.accent_active)
        label = f"{fa.ICON_FA_KEYBOARD}  Press a key... (Esc to cancel)"
    else:
        label = f"{fa.ICON_FA_KEYBOARD}  {display_name}"
    clicked = imgui.button(label, imgui.ImVec2(0, 0))
    if capturing:
        imgui.pop_style_color(2)
    imgui.pop_id()
    return clicked
