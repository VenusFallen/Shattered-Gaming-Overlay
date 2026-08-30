"""shell.py -- Companion window nav shell: sidebar + content dispatch.

Single `show_gui` root (main.py wires `render_frame` to
`RunnerParams.callbacks.show_gui`); Hello ImGui already wraps it in one
full-viewport window, so sidebar and active panel share one canvas.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import theme as theme_module
import titlebar
from app_state import AppState
from hud_overlay import hud_overlay
from key_capture import capture_service
from panel_context import PanelContext
from panels import about, dashboard, macros, overlay, profiles, remapper, settings

_SIDEBAR_WIDTH = 220.0

# (panel key, icon, label) -- nav order. Dashboard is the landing screen
# (see AppState.active_panel). Window Select lives inside Settings as a
# card now, not its own tab (panels/window_select.py::render_section).
_NAV_ITEMS = (
    ("dashboard", fa.ICON_FA_TACHOMETER_ALT, "Dashboard"),
    ("overlay", fa.ICON_FA_CROSSHAIRS, "Overlay"),
    ("macros", fa.ICON_FA_LIST_OL, "Macros"),
    ("remapper", fa.ICON_FA_KEYBOARD, "Remapper"),
    ("profiles", fa.ICON_FA_FOLDER_OPEN, "Profiles"),
    ("settings", fa.ICON_FA_COG, "Settings"),
    ("about", fa.ICON_FA_INFO_CIRCLE, "About"),
)

_PANEL_RENDERERS = {
    "dashboard": dashboard.render,
    "overlay": overlay.render,
    "macros": macros.render,
    "remapper": remapper.render,
    "profiles": profiles.render,
    "settings": settings.render,
    "about": about.render,
}


def _render_sidebar(ctx: PanelContext) -> None:
    theme = ctx.theme
    imgui.push_style_color(imgui.Col_.child_bg, theme.bg_sidebar)
    imgui.begin_child("sidebar", imgui.ImVec2(_SIDEBAR_WIDTH, 0), imgui.ChildFlags_.always_use_window_padding)

    imgui.text_colored(theme.text_primary, "Shattered Gaming")
    imgui.text_colored(theme.accent_text, "Overlay")
    imgui.spacing()
    imgui.text_colored(theme.text_secondary, "Companion")
    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    # imgui.selectable's size doesn't honor "-1 = fill width" in this
    # imgui_bundle build -- pass an explicit width or rows collapse to ~7px.
    row_width = imgui.get_content_region_avail().x

    for key, icon, label in _NAV_ITEMS:
        selected = ctx.state.active_panel == key
        marker = fa.ICON_FA_CHEVRON_RIGHT if selected else " "
        row_label = f"{marker}  {icon}  {label}##nav-{key}"
        imgui.push_style_color(imgui.Col_.text, theme.accent_text if selected else theme.text_primary)
        clicked, _ = imgui.selectable(row_label, selected, size=imgui.ImVec2(row_width, 34))
        imgui.pop_style_color()
        if clicked:
            ctx.state.active_panel = key

    imgui.end_child()
    imgui.pop_style_color()


def render_frame(state: AppState) -> None:
    """Called once per frame by Hello ImGui (see main.py)."""
    # Named app_settings, not settings -- the settings module is imported
    # under that name below (settings.render_auto_update_prompt) and would
    # be shadowed.
    app_settings = state.settings
    if app_settings.theme_name == "color_cycle":
        # Clock lives here, not in theme.py -- apply_theme/resolve_color_cycle_theme
        # stay time-agnostic. Reduce Motion freezes the effect by just not
        # advancing cycle_elapsed_sec.
        if not app_settings.reduce_motion:
            app_settings.cycle_elapsed_sec += imgui.get_io().delta_time
        theme = theme_module.resolve_color_cycle_theme(
            app_settings.cycle_color_a,
            app_settings.cycle_color_b,
            app_settings.cycle_period_sec,
            app_settings.cycle_elapsed_sec,
        )
    else:
        theme = theme_module.get_theme(app_settings.theme_name)
    theme_module.apply_theme(theme)
    # This is the one call site that resolves Color Cycle to a real Theme
    # each frame, so it's also the one that hands it to the HUD render thread.
    hud_overlay.update_theme(theme)

    ctx = PanelContext(state=state, theme=theme, capture=capture_service)

    titlebar.render(ctx)
    # Drawn unconditionally every frame, independent of active panel, so the
    # update prompt/flow can surface without navigating to Settings.
    settings.render_auto_update_prompt(ctx)
    settings.render_update_flow_popup(ctx)

    _render_sidebar(ctx)
    imgui.same_line()

    imgui.begin_child("content", imgui.ImVec2(0, 0), imgui.ChildFlags_.always_use_window_padding)
    render_panel = _PANEL_RENDERERS.get(state.active_panel, dashboard.render)
    render_panel(ctx)
    imgui.end_child()
