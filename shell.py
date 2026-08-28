"""shell.py -- Companion window nav shell: sidebar + content dispatch.

This is the single `show_gui` callback root: main.py points Hello ImGui's
`RunnerParams.callbacks.show_gui` at `render_frame()`. Hello ImGui already
wraps the callback in one full-viewport ImGui window (see
ImGuiWindowParams.default_imgui_window_type = provide_full_screen_window in
main.py), so everything below draws directly onto that single canvas --
sidebar on the left, the active panel's content on the right.
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

# (panel key, icon, display label) -- order is the nav order. Dashboard is
# first: it's the landing screen (see app_state.AppState.active_panel).
# Window Select is no longer a standalone tab -- it's folded into Settings
# as a card (see panels/window_select.py::render_section, called from
# panels/settings.py).
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

    # NOTE: imgui.selectable's `size` does NOT support the classic Dear ImGui
    # "negative x = fill available width" convention in this imgui_bundle
    # build -- a literal ImVec2(-1, h) is taken at face value and produces a
    # ~7px-wide selectable (confirmed by an isolated repro: item_rect width
    # was 7.0 regardless of label length, vs. ~212 once an explicit positive
    # width is passed). That tiny width clipped away all 6 rows' icon+label
    # text and their hit-test area down to a sliver, which is what looked
    # like "one malformed row + an empty sidebar" on screen. Fix: compute the
    # actual available width once and pass it explicitly.
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
    # NOTE: deliberately not named `settings` -- panels/settings.py is
    # already imported into this module's namespace under that name (see
    # the `settings.render_auto_update_prompt(ctx)` call below), and a local
    # `settings` here would silently shadow that module reference.
    app_settings = state.settings
    if app_settings.theme_name == "color_cycle":
        # Advance the animation clock here, not inside theme.py --
        # apply_theme()/resolve_color_cycle_theme() stay non-time-aware, this
        # is the one call site that owns "what time is it". Reduce Motion
        # freezes the effect by simply not advancing cycle_elapsed_sec, so
        # the resolved color holds at whatever it was on last unfrozen.
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
    # Hand the resolved theme (including any live Color Cycle instant) off
    # to the HUD overlay's render thread -- see hud_overlay.py's
    # update_theme() docstring for why this is the one call site that
    # reaches it (this function is the only place that actually resolves
    # Color Cycle to a real Theme each frame).
    hud_overlay.update_theme(theme)

    ctx = PanelContext(state=state, theme=theme, capture=capture_service)

    titlebar.render(ctx)
    # Automatic check-on-launch prompt (see updater.py / panels/settings.py's
    # render_auto_update_prompt docstring) -- drawn unconditionally every
    # frame, same as titlebar.render(ctx) just above, so it can show
    # regardless of which panel is currently active.
    settings.render_auto_update_prompt(ctx)

    _render_sidebar(ctx)
    imgui.same_line()

    imgui.begin_child("content", imgui.ImVec2(0, 0), imgui.ChildFlags_.always_use_window_padding)
    render_panel = _PANEL_RENDERERS.get(state.active_panel, dashboard.render)
    render_panel(ctx)
    imgui.end_child()
