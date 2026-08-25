"""panels/overlay.py -- Overlay panel: per-element enable toggles + basic
styling for whatever the future HUD overlay window renders (stats HUD,
accessibility crosshair, module status indicators). This panel only edits
app_state.OverlayState -- the actual click-through, non-injecting HUD
overlay window described in .claude/agents/ui-agent.md is separate, future
work and is NOT built here. Overlay visibility is intentionally never gated
by Window Select's focus filter (see engine-agent.md) -- nothing in this
panel reads or writes window_select state.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from panel_context import PanelContext

_CORNERS = ["Top Left", "Top Right", "Bottom Left", "Bottom Right"]
_CROSSHAIR_STYLES = ["Cross", "Dot", "Circle", "T-Shape"]


def _corner_combo(label: str, current: str) -> str:
    idx = _CORNERS.index(current) if current in _CORNERS else 0
    imgui.set_next_item_width(160)
    changed, idx = imgui.combo(label, idx, _CORNERS)
    return _CORNERS[idx] if changed else current


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

        s.corner = _corner_combo("Anchor corner##stats", s.corner)
        imgui.set_next_item_width(200)
        _, s.scale = imgui.slider_float("Scale##stats", s.scale, 0.5, 2.0, "%.2fx")
        _, s.color = imgui.color_edit4("Text color##stats", s.color)


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
        idx = _CROSSHAIR_STYLES.index(s.style) if s.style in _CROSSHAIR_STYLES else 0
        imgui.set_next_item_width(160)
        changed, idx = imgui.combo("Style##crosshair", idx, _CROSSHAIR_STYLES)
        if changed:
            s.style = _CROSSHAIR_STYLES[idx]
        imgui.set_next_item_width(200)
        _, s.size = imgui.slider_float("Size##crosshair", s.size, 4.0, 48.0, "%.0f px")
        imgui.set_next_item_width(200)
        _, s.thickness = imgui.slider_float("Thickness##crosshair", s.thickness, 1.0, 8.0, "%.1f px")
        _, s.color = imgui.color_edit4("Color##crosshair", s.color)


def _render_status_indicators(ctx: PanelContext) -> None:
    theme = ctx.theme
    s = ctx.state.overlay.status_indicators
    with widgets.card(theme, "overlay-status-indicators", size=(0, 0)):
        _, s.enabled = widgets.labeled_toggle(
            theme, f"{fa.ICON_FA_INFO_CIRCLE}  Module Status Indicators", s.enabled, ctx.state.settings.reduce_motion
        )
        widgets.muted_text(theme, "Shows which macro/remap/profile is currently armed.")
        if not s.enabled:
            return
        imgui.spacing()
        _, s.show_remap_status = widgets.labeled_toggle(
            theme, "Remap status", s.show_remap_status, ctx.state.settings.reduce_motion
        )
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(16, 0))
        imgui.same_line()
        _, s.show_macro_status = widgets.labeled_toggle(
            theme, "Macro status", s.show_macro_status, ctx.state.settings.reduce_motion
        )
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(16, 0))
        imgui.same_line()
        _, s.show_profile_name = widgets.labeled_toggle(
            theme, "Profile name", s.show_profile_name, ctx.state.settings.reduce_motion
        )

        s.corner = _corner_combo("Anchor corner##status", s.corner)
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
