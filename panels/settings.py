"""panels/settings.py -- Settings panel: theme, UI scale, reduce motion,
Target Window (folded in from the former standalone Window Select tab -- see
panels/window_select.py), and a placeholder Updates area. Theme/scale/
reduce-motion are fully live (see theme.apply_theme, called every frame from
shell.py using this state). Updates has no backend yet -- build-agent's
self-updater against GitHub Releases doesn't exist -- so that section is
clearly marked as a preview.

About (program description/version/credit) lives in its own top-level nav
entry now -- see panels/about.py -- not here.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import theme as theme_module
import widgets
from panel_context import PanelContext
from panels import window_select


def _render_appearance(ctx: PanelContext) -> None:
    theme = ctx.theme
    settings = ctx.state.settings
    with widgets.card(theme, "settings-appearance", size=(0, 0)):
        widgets.section_title("Appearance")

        names = theme_module.theme_names()
        display_names = [theme_module.get_theme(n).display_name for n in names]
        current_idx = names.index(settings.theme_name) if settings.theme_name in names else 0
        imgui.set_next_item_width(220)
        changed, current_idx = imgui.combo("Theme", current_idx, display_names)
        if changed:
            settings.theme_name = names[current_idx]

        imgui.spacing()
        imgui.set_next_item_width(260)
        changed, settings.ui_scale = imgui.slider_float("UI scale", settings.ui_scale, 0.75, 2.0, "%.2fx")

        imgui.spacing()
        _, settings.reduce_motion = widgets.labeled_toggle(
            theme,
            "Reduce motion",
            settings.reduce_motion,
            settings.reduce_motion,
            tooltip="Disables toggle-switch animation and other future motion effects.",
        )


def _render_updates(ctx: PanelContext) -> None:
    theme = ctx.theme
    settings = ctx.state.settings
    with widgets.card(theme, "settings-updates", size=(0, 0)):
        widgets.section_title("Updates")
        widgets.status_badge(theme, "info", "Preview -- the self-updater isn't wired up yet")
        widgets.muted_text(theme, "This section reflects future in-app updates against GitHub Releases.")

        imgui.spacing()
        channels = ["Stable", "Beta"]
        idx = channels.index(settings.updates_channel) if settings.updates_channel in channels else 0
        imgui.set_next_item_width(160)
        changed, idx = imgui.combo("Channel", idx, channels)
        if changed:
            settings.updates_channel = channels[idx]

        _, settings.check_for_updates_on_launch = widgets.labeled_toggle(
            theme, "Check for updates on launch", settings.check_for_updates_on_launch, settings.reduce_motion
        )

        imgui.spacing()
        widgets.muted_text(theme, settings.last_checked_display)
        imgui.begin_disabled()
        imgui.button(f"{fa.ICON_FA_SYNC}  Check Now")
        imgui.end_disabled()
        if imgui.is_item_hovered():
            imgui.set_tooltip("Not implemented yet -- no updater backend exists.")


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    imgui.text(f"{fa.ICON_FA_COG}  Settings")
    imgui.spacing()

    _render_appearance(ctx)
    imgui.spacing()
    window_select.render_section(ctx)
    imgui.spacing()
    _render_updates(ctx)
