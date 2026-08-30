"""panels/overlay.py -- Overlay panel: per-element enable toggles + styling
for what hud_overlay.py renders (Stats HUD, accessibility crosshair, module
status indicators). Only edits app_state.OverlayState -- the HUD window
itself lives in hud_overlay.py. Visibility is never gated by Window
Select's focus filter; nothing here reads or writes window_select state.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from panel_context import PanelContext

_CROSSHAIR_STYLES = ["Cross", "Dot", "Circle", "Circle + Dot", "T-Shape"]

_PREVIEW_CARD = 64.0
_PREVIEW_ICON_R = 20.0


def _draw_crosshair_preview(draw_list, cx: float, cy: float, style: str, col: int) -> None:
    """Small static icon of `style`, mirroring hud_overlay.py's real
    _draw_crosshair proportions but without its black readability outline --
    not needed on a themed card background."""
    r = _PREVIEW_ICON_R
    gap = 3.0
    thick = 2.0
    p = imgui.ImVec2

    if style == "Dot":
        draw_list.add_circle_filled(p(cx, cy), r * 0.4, col)
    elif style == "Circle":
        draw_list.add_circle(p(cx, cy), r, col, thickness=thick)
    elif style == "Circle + Dot":
        draw_list.add_circle(p(cx, cy), r, col, thickness=thick)
        draw_list.add_circle_filled(p(cx, cy), max(1.5, r * 0.15), col)
    elif style == "T-Shape":
        draw_list.add_line(p(cx - gap - r, cy), p(cx - gap, cy), col, thick)
        draw_list.add_line(p(cx + gap, cy), p(cx + gap + r, cy), col, thick)
        draw_list.add_line(p(cx, cy + gap), p(cx, cy + gap + r), col, thick)
    else:  # Cross
        draw_list.add_line(p(cx - gap - r, cy), p(cx - gap, cy), col, thick)
        draw_list.add_line(p(cx + gap, cy), p(cx + gap + r, cy), col, thick)
        draw_list.add_line(p(cx, cy - gap - r), p(cx, cy - gap), col, thick)
        draw_list.add_line(p(cx, cy + gap), p(cx, cy + gap + r), col, thick)


def _crosshair_style_picker(theme, current: str) -> str:
    """Horizontal row of clickable preview cards -- replaces a plain dropdown
    so the user sees each style before picking it."""
    new_style = current
    draw_list = imgui.get_window_draw_list()
    u32 = lambda rgba: imgui.color_convert_float4_to_u32(imgui.ImVec4(*rgba))  # noqa: E731

    for i, style in enumerate(_CROSSHAIR_STYLES):
        if i > 0:
            imgui.same_line()
        selected = style == current
        imgui.push_id(f"chstyle-{style}")

        pos = imgui.get_cursor_screen_pos()
        p_max = imgui.ImVec2(pos.x + _PREVIEW_CARD, pos.y + _PREVIEW_CARD)
        card_bg = theme.bg_input_active if selected else theme.bg_input
        draw_list.add_rect_filled(pos, p_max, u32(card_bg), rounding=8.0)

        icon_col = u32(theme.accent_text)
        _draw_crosshair_preview(draw_list, pos.x + _PREVIEW_CARD / 2, pos.y + _PREVIEW_CARD / 2, style, icon_col)

        clicked = imgui.invisible_button(f"##btn-{style}", imgui.ImVec2(_PREVIEW_CARD, _PREVIEW_CARD))
        border_col = theme.accent if selected else theme.border
        draw_list.add_rect(pos, p_max, u32(border_col), rounding=8.0, thickness=2.0 if selected else 1.0)

        if imgui.is_item_hovered():
            imgui.set_tooltip(style)
        imgui.pop_id()

        if clicked:
            new_style = style

    return new_style


def _render_stats_hud(ctx: PanelContext) -> None:
    theme = ctx.theme
    s = ctx.state.overlay.stats_hud
    with widgets.card(theme, "overlay-stats-hud", size=(0, 0)):
        _, s.enabled = widgets.labeled_toggle(
            theme, f"{fa.ICON_FA_TACHOMETER_ALT}  Stats HUD", s.enabled, ctx.state.settings.reduce_motion
        )
        widgets.muted_text(theme, "CPU/GPU usage & temp, VRAM, RAM, FPS of the focused window.")
        if not s.enabled:
            return
        imgui.spacing()
        _, s.show_cpu = widgets.labeled_toggle(theme, "CPU", s.show_cpu, ctx.state.settings.reduce_motion)
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(16, 0))
        imgui.same_line()
        _, s.show_gpu = widgets.labeled_toggle(theme, "GPU", s.show_gpu, ctx.state.settings.reduce_motion)
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(16, 0))
        imgui.same_line()
        _, s.show_ram = widgets.labeled_toggle(theme, "RAM", s.show_ram, ctx.state.settings.reduce_motion)
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(16, 0))
        imgui.same_line()
        _, s.show_fps = widgets.labeled_toggle(theme, "FPS", s.show_fps, ctx.state.settings.reduce_motion)
        if s.show_fps and ctx.state.stats_fps_error:
            imgui.spacing()
            widgets.status_badge(theme, "warn", ctx.state.stats_fps_error)
            widgets.muted_text(
                theme,
                "FPS only works against DirectX/DXGI games -- it can't read OpenGL "
                "titles (including this app's own window) or software the OS reports "
                "as still starting up.",
            )

        widgets.muted_text(theme, "Position")
        s.corner = widgets.screen_position_picker(theme, "stats-position", s.corner)
        imgui.set_next_item_width(200)
        _, s.scale = imgui.slider_float("Scale##stats", s.scale, 0.5, 2.0, "%.2fx")
        _, s.color, _ = widgets.hex_color_picker(theme, "stats-color", "Text color", s.color)
        imgui.set_next_item_width(200)
        _, s.bg_alpha = imgui.slider_float("Background opacity##stats", s.bg_alpha, 0.0, 1.0, "%.2f")


def _render_crosshair(ctx: PanelContext) -> None:
    theme = ctx.theme
    s = ctx.state.overlay.crosshair
    with widgets.card(theme, "overlay-crosshair", size=(0, 0)):
        _, s.enabled = widgets.labeled_toggle(
            theme, f"{fa.ICON_FA_CROSSHAIRS}  Accessibility Crosshair", s.enabled, ctx.state.settings.reduce_motion
        )
        widgets.muted_text(theme, "A visual aid rendered center-screen over the game.")
        if not s.enabled:
            return
        imgui.spacing()
        widgets.muted_text(theme, "Style")
        s.style = _crosshair_style_picker(theme, s.style)
        imgui.spacing()
        imgui.set_next_item_width(200)
        _, s.size = imgui.slider_float("Size##crosshair", s.size, 4.0, 48.0, "%.0f px")
        imgui.set_next_item_width(200)
        _, thickness_int = imgui.slider_int("Thickness##crosshair", int(s.thickness), 1, 8, "%d px")
        s.thickness = float(thickness_int)
        imgui.set_next_item_width(200)
        _, s.gap = imgui.slider_float("Gap##crosshair", s.gap, 0.0, 24.0, "%.0f px")
        if imgui.is_item_hovered():
            imgui.set_tooltip(
                "Cross/T-Shape: space at the center -- 0 makes an unbroken plus/T.\n"
                "Circle + Dot: how far the ring sits from the center dot."
            )
        _, s.color, _ = widgets.hex_color_picker(theme, "crosshair-color", "Color", s.color)


def _render_status_indicators(ctx: PanelContext) -> None:
    theme = ctx.theme
    s = ctx.state.overlay.status_indicators
    with widgets.card(theme, "overlay-status-indicators", size=(0, 0)):
        _, s.enabled = widgets.labeled_toggle(
            theme, f"{fa.ICON_FA_INFO_CIRCLE}  Module Status Indicators", s.enabled, ctx.state.settings.reduce_motion
        )
        widgets.muted_text(
            theme,
            "Two themed badges showing how many Remap entries / Macros are currently enabled.",
        )
        if not s.enabled:
            return
        imgui.spacing()
        _, s.show_remap_badge = widgets.labeled_toggle(
            theme, "Remapper badge", s.show_remap_badge, ctx.state.settings.reduce_motion
        )
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(16, 0))
        imgui.same_line()
        _, s.show_macro_badge = widgets.labeled_toggle(
            theme, "Macros badge", s.show_macro_badge, ctx.state.settings.reduce_motion
        )

        widgets.muted_text(theme, "Position")
        s.corner = widgets.screen_position_picker(
            theme, "status-position", s.corner, widgets.SCREEN_POSITIONS_WITH_MIDDLES
        )
        imgui.set_next_item_width(200)
        _, s.scale = imgui.slider_float("Scale##status", s.scale, 0.5, 2.0, "%.2fx")


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    imgui.text(f"{fa.ICON_FA_CROSSHAIRS}  Overlay")
    widgets.muted_text(
        theme,
        "Configures the passive HUD overlay that renders on top of the game -- click-through, "
        "never interactive. Tune it here even while the game (not this window) has focus.",
    )
    imgui.spacing()

    _render_stats_hud(ctx)
    imgui.spacing()
    _render_crosshair(ctx)
    imgui.spacing()
    _render_status_indicators(ctx)
