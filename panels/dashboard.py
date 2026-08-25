"""panels/dashboard.py -- landing screen: a bento-grid, at-a-glance overview
of the live AppState. This is the first screen a user sees (app_state.py
defaults `active_panel` to "dashboard"), so it's read-only by design and
pulls every number directly off the same state the other panels edit --
Profiles, Remapper, Macros, Window Select, Overlay -- rather than keeping
any parallel/duplicate state of its own.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from panel_context import PanelContext

_GRID_GAP = 12.0
# Tall enough for a wrapped 2-line sub-line (e.g. a long target exe name) --
# a single-line-tight height plus wrapped text spawned a spurious vertical
# scrollbar on this card, caught only by an actual screenshot (see report).
_STAT_CARD_HEIGHT = 128.0


def _stat_card(ctx: PanelContext, str_id: str, icon: str, label: str, value: str, sub: str, width: float) -> None:
    theme = ctx.theme
    with widgets.card(theme, str_id, size=(width, _STAT_CARD_HEIGHT)):
        imgui.text_colored(theme.text_secondary, f"{icon}  {label.upper()}")
        imgui.spacing()
        # Wrapped, not plain text_colored -- these are narrow fixed-width
        # grid cells (unlike other panels' full-width cards), and a longer
        # target/process name would otherwise overflow past the card border.
        imgui.push_style_color(imgui.Col_.text, theme.text_primary)
        imgui.text_wrapped(value)
        imgui.pop_style_color()
        imgui.spacing()
        imgui.push_style_color(imgui.Col_.text, theme.text_secondary)
        imgui.text_wrapped(sub)
        imgui.pop_style_color()


def _overlay_row(ctx: PanelContext, icon: str, label: str, enabled: bool) -> None:
    theme = ctx.theme
    level = "ok" if enabled else "neutral"
    status = "On" if enabled else "Off"
    widgets.status_badge(theme, level, f"{icon}  {label}: {status}")


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state

    imgui.text(f"{fa.ICON_FA_TACHOMETER_ALT}  Dashboard")
    widgets.muted_text(
        theme,
        "At a glance. Everything below reflects live state from its own panel -- "
        "nothing here is edited directly.",
    )
    imgui.spacing()
    imgui.spacing()

    # --- hero row: active profile ---
    active_profile = next((p for p in state.profiles.profiles if p.id == state.profiles.active_id), None)
    profile_name = active_profile.name if active_profile is not None else "None"
    profile_count = len(state.profiles.profiles)

    with widgets.card(theme, "dash-profile", size=(0, 0)):
        imgui.text_colored(theme.text_secondary, f"{fa.ICON_FA_FOLDER_OPEN}  ACTIVE PROFILE")
        imgui.spacing()
        imgui.push_style_color(imgui.Col_.text, theme.text_primary)
        imgui.text(profile_name)
        imgui.pop_style_color()
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(12, 0))
        imgui.same_line()
        if active_profile is not None and active_profile.protected:
            widgets.status_badge(theme, "info", f"{fa.ICON_FA_LOCK}  Protected")
            imgui.same_line()
            imgui.dummy(imgui.ImVec2(12, 0))
            imgui.same_line()
        widgets.status_badge(theme, "ok", "Loaded")
        imgui.spacing()
        widgets.muted_text(
            theme, f"{profile_count} profile{'s' if profile_count != 1 else ''} saved -- manage them from Profiles."
        )

    imgui.spacing()

    # --- bento row: remaps / macros / window target ---
    remap_total = len(state.remapper.entries)
    remap_enabled = sum(1 for e in state.remapper.entries if e.enabled)
    remap_value = f"{remap_enabled} / {remap_total}" if remap_total else "0"
    remap_sub = "enabled" if remap_total else "none configured yet"

    macro_total = len(state.macros.macros)
    macro_enabled = sum(1 for m in state.macros.macros if m.enabled)
    macro_value = f"{macro_enabled} / {macro_total}" if macro_total else "0"
    macro_sub = "enabled" if macro_total else "none configured yet"

    target = state.window_select.selected
    target_value = target.exe_name if target is not None else "Global"
    if target is not None:
        target_sub = f'"{target.window_title}"'
    else:
        target_sub = "Unrestricted -- no process targeted"

    avail = imgui.get_content_region_avail().x
    col_w = (avail - _GRID_GAP * 2) / 3.0

    _stat_card(ctx, "dash-remaps", fa.ICON_FA_KEYBOARD, "Remaps", remap_value, remap_sub, col_w)
    imgui.same_line(0, _GRID_GAP)
    _stat_card(ctx, "dash-macros", fa.ICON_FA_LIST_OL, "Macros", macro_value, macro_sub, col_w)
    imgui.same_line(0, _GRID_GAP)
    _stat_card(ctx, "dash-target", fa.ICON_FA_DESKTOP, "Window Target", target_value, target_sub, col_w)

    imgui.spacing()

    # --- overlay summary row ---
    o = state.overlay
    with widgets.card(theme, "dash-overlay", size=(0, 0)):
        imgui.text_colored(theme.text_secondary, f"{fa.ICON_FA_CROSSHAIRS}  OVERLAY")
        imgui.spacing()
        _overlay_row(ctx, fa.ICON_FA_TACHOMETER_ALT, "Stats HUD", o.stats_hud.enabled)
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(24, 0))
        imgui.same_line()
        _overlay_row(ctx, fa.ICON_FA_CROSSHAIRS, "Crosshair", o.crosshair.enabled)
        imgui.same_line()
        imgui.dummy(imgui.ImVec2(24, 0))
        imgui.same_line()
        _overlay_row(ctx, fa.ICON_FA_INFO_CIRCLE, "Status Indicators", o.status_indicators.enabled)
        imgui.spacing()
        widgets.muted_text(theme, "Configure what's shown, and how, from the Overlay panel.")
