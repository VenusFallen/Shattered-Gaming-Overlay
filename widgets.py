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
    # size.y == 0 means "fill remaining parent space" in plain BeginChild, not
    # "auto-size to content" -- without auto_resize_y a single-row card
    # stretches to fill the rest of its parent and forces a spurious scrollbar.
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
    """Draw `[toggle switch]  Label` and return (changed, new_value). State
    is conveyed by knob position plus the text label, never color alone.
    """
    # imgui_toggle.ToggleConstants isn't exported at runtime despite being in
    # the .pyi stub -- 0.1s is its documented default, inlined since it can't
    # be imported.
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
) -> Tuple[bool, Tuple[float, float, float, float], bool]:
    """A themed color swatch + label; clicking it opens a popup with one
    visual picker and a hex field -- deliberately no RGB sliders or alpha
    control, and an explicit Close button since the default popup only
    dismisses on an outside click.

    Still passes/returns a 4-tuple (alpha forced to 1.0) so existing
    RGBA-typed consumers don't need to change.

    Returns (changed, rgba, committed). `changed` fires every dragged frame
    (color_picker3's own `picked` flag); `committed` fires once, on release
    (is_item_deactivated_after_edit()) -- must be checked right after
    color_picker3, since the Close button drawn after it becomes the "last
    item" otherwise.
    """
    imgui.push_id(str_id)
    swatch_col = imgui.ImVec4(rgba[0], rgba[1], rgba[2], 1.0)
    if imgui.color_button("##swatch", swatch_col, imgui.ColorEditFlags_.no_alpha, imgui.ImVec2(28, 20)):
        imgui.open_popup("picker")
    imgui.same_line()
    imgui.text(label)

    changed = False
    committed = False
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
        committed = imgui.is_item_deactivated_after_edit()
        imgui.spacing()
        if imgui.button("Close", imgui.ImVec2(-1, 0)):
            imgui.close_current_popup()
        imgui.end_popup()

    imgui.pop_id()
    return changed, (new_rgb[0], new_rgb[1], new_rgb[2], 1.0), committed


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
    """Small rounded-rect "monitor" with clickable position markers, one per
    entry in `positions`. Returns the newly selected position, or `current`
    if nothing was clicked this frame."""
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
    """Render `label` as a clickable hyperlink (accent color, hand cursor,
    underline on hover) opening `url` in the OS browser. No native hyperlink
    widget exists here, so this is a Selectable sized to its text (an
    explicit width, not -1 -- see the imgui.selectable gotcha) re-themed to
    read as a link.
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
