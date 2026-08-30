"""panels/settings.py -- Settings panel: theme, reduce motion, Target Window
(panels/window_select.py's card), and Updates. Theme/reduce-motion are fully
live via theme.apply_theme, called every frame from shell.py.

Updates: `_render_updates` only reads `ctx.state.settings.update_*` (written
by `updater.update_manager.sync_to()` from main.py) and issues commands via
`updater.update_manager`, never touching its internals directly.
`render_auto_update_prompt` is the check-on-launch popup, called
unconditionally every frame from shell.py regardless of active panel.
"""

from __future__ import annotations

import colorsys
import time

from imgui_bundle import hello_imgui as hi
from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import settings_store
import theme as theme_module
import updater
import widgets
from app_state import UpdateStatus
from panel_context import PanelContext
from panels import window_select
from version import VERSION

_AUTO_UPDATE_POPUP_ID = "Update Available"
_UPDATE_FLOW_POPUP_ID = "Installing Update"

# Statuses covered by render_update_flow_popup -- the tail of the update
# flow after AVAILABLE has been accepted, from either the auto-prompt or the
# Settings card's own button.
_UPDATE_FLOW_STATUSES = (UpdateStatus.DOWNLOADING, UpdateStatus.READY, UpdateStatus.INSTALLING)

_THEME_CARD = 84.0
_THEME_SWATCH_ROW_H = 26.0
_THEME_SWATCH_GAP = 3.0

# Fast preview sweep for the Color Cycle card's swatches (offset from each
# other), independent of the user's actual Color A/B, so the card visibly
# reads as animated in the picker.
_RAINBOW_PERIOD_SEC = 4.0


def _rainbow_rgba(phase_offset: float) -> tuple:
    hue = ((time.monotonic() / _RAINBOW_PERIOD_SEC) + phase_offset) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.75, 0.95)
    return (r, g, b, 1.0)


def _theme_swatches(name: str, t: theme_module.Theme) -> list:
    if name == "color_cycle":
        return [_rainbow_rgba(i * 0.25) for i in range(4)]
    return [t.bg_base, t.accent, t.accent_active, t.text_primary]


def _theme_picker(theme: theme_module.Theme, current: str) -> str:
    """Horizontal row of theme cards, each showing a strip of that theme's
    own palette swatches (background, accent, accent_active, text) with the
    display name below. Color Cycle's card uses a live animated rainbow
    instead of fixed swatches, signaling that it's dynamic."""
    new_name = current
    draw_list = imgui.get_window_draw_list()
    u32 = lambda rgba: imgui.color_convert_float4_to_u32(imgui.ImVec4(*rgba))  # noqa: E731
    # push_text_wrap_pos takes a window-local x, not screen-space -- every
    # wrap-pos call below must subtract this since the rest of the function
    # works in screen space.
    window_x = imgui.get_window_pos().x

    names = theme_module.theme_names()
    # Wrap to a new row rather than overflowing the card's width; computed
    # once from the width available here.
    avail_w = imgui.get_content_region_avail().x
    spacing = imgui.get_style().item_spacing.x
    cards_per_row = max(1, int((avail_w + spacing) // (_THEME_CARD + spacing)))

    for i, name in enumerate(names):
        if i > 0:
            if i % cards_per_row != 0:
                imgui.same_line()
        t = theme_module.get_theme(name)
        selected = name == current
        imgui.push_id(f"theme-{name}")

        pos = imgui.get_cursor_screen_pos()
        p_max = imgui.ImVec2(pos.x + _THEME_CARD, pos.y + _THEME_CARD)
        card_bg = theme.bg_input_active if selected else theme.bg_input
        draw_list.add_rect_filled(pos, p_max, u32(card_bg), rounding=8.0)

        # Swatch strip near the top of the card.
        swatches = _theme_swatches(name, t)
        n = len(swatches)
        strip_x0 = pos.x + 8.0
        strip_x1 = p_max.x - 8.0
        strip_w = strip_x1 - strip_x0
        sw_w = (strip_w - _THEME_SWATCH_GAP * (n - 1)) / n
        sy0 = pos.y + 8.0
        sy1 = sy0 + _THEME_SWATCH_ROW_H
        for j, col in enumerate(swatches):
            sx0 = strip_x0 + j * (sw_w + _THEME_SWATCH_GAP)
            sx1 = sx0 + sw_w
            draw_list.add_rect_filled(imgui.ImVec2(sx0, sy0), imgui.ImVec2(sx1, sy1), u32(col), rounding=3.0)

        # Display name, wrapped/clipped to the card width, below the swatches.
        imgui.set_cursor_screen_pos(imgui.ImVec2(pos.x + 6.0, sy1 + 6.0))
        imgui.push_text_wrap_pos(p_max.x - 6.0 - window_x)
        imgui.text_colored(theme.text_primary if selected else theme.text_secondary, t.display_name)
        imgui.pop_text_wrap_pos()

        imgui.set_cursor_screen_pos(pos)
        clicked = imgui.invisible_button("##themecard", imgui.ImVec2(_THEME_CARD, _THEME_CARD))
        border_col = theme.accent if selected else theme.border
        draw_list.add_rect(pos, p_max, u32(border_col), rounding=8.0, thickness=2.0 if selected else 1.0)

        imgui.pop_id()
        if clicked:
            new_name = name

    return new_name


def _render_appearance(ctx: PanelContext) -> None:
    theme = ctx.theme
    settings = ctx.state.settings
    with widgets.card(theme, "settings-appearance", size=(0, 0)):
        widgets.section_title("Appearance")

        old_theme_name = settings.theme_name
        settings.theme_name = _theme_picker(theme, settings.theme_name)
        if settings.theme_name != old_theme_name:
            settings_store.save(ctx.state)

        if settings.theme_name == "color_cycle":
            imgui.spacing()
            widgets.muted_text(
                theme,
                "Slowly, continuously drifts the accent color back and forth between two colors you pick "
                "-- like a slow RGB keyboard breathing effect, not a rainbow cycle.",
            )
            imgui.spacing()
            # committed fires once, on drag-release -- see hex_color_picker's
            # docstring. Saving on every `changed` frame would mean a disk
            # write per frame of drag.
            _, settings.cycle_color_a, committed_a = widgets.hex_color_picker(
                theme, "cycle-color-a", "Color A", settings.cycle_color_a
            )
            _, settings.cycle_color_b, committed_b = widgets.hex_color_picker(
                theme, "cycle-color-b", "Color B", settings.cycle_color_b
            )
            if committed_a or committed_b:
                settings_store.save(ctx.state)
            imgui.spacing()
            imgui.set_next_item_width(260)
            # Bounds are both "slow" -- even the fast end (15s per cycle)
            # reads as ambient drift, never a flash/strobe.
            _, settings.cycle_period_sec = imgui.slider_float(
                "Cycle speed##cycle", settings.cycle_period_sec, 15.0, 45.0, "%.0f sec per full cycle"
            )
            if imgui.is_item_deactivated_after_edit():
                settings_store.save(ctx.state)
            if settings.reduce_motion:
                widgets.muted_text(theme, "Reduce motion is on -- the color is frozen and will not drift.")

        imgui.spacing()
        changed, settings.reduce_motion = widgets.labeled_toggle(
            theme,
            "Reduce motion",
            settings.reduce_motion,
            settings.reduce_motion,
            tooltip="Disables toggle-switch animation and other future motion effects.",
        )
        if changed:
            settings_store.save(ctx.state)


def _render_window_behavior(ctx: PanelContext) -> None:
    theme = ctx.theme
    settings = ctx.state.settings
    with widgets.card(theme, "settings-window-behavior", size=(0, 0)):
        widgets.section_title("Window behavior")
        changed, settings.close_minimizes_to_tray = widgets.labeled_toggle(
            theme,
            "Closing the window minimizes to tray",
            settings.close_minimizes_to_tray,
            settings.reduce_motion,
            tooltip="Off: the X button exits the app outright instead of hiding to the tray icon.",
        )
        if changed:
            settings_store.save(ctx.state)


# Maps UpdateStatus -> (badge level, badge label).
def _status_badge_args(status: UpdateStatus, settings) -> tuple[str, str]:
    if status == UpdateStatus.CHECKING:
        return "info", "Checking for updates..."
    if status == UpdateStatus.UP_TO_DATE:
        return "ok", f"Up to date -- v{VERSION}"
    if status == UpdateStatus.AVAILABLE:
        return "info", f"v{settings.update_latest_version} available"
    if status == UpdateStatus.DOWNLOADING:
        return "info", f"Downloading... {settings.update_download_pct}%"
    if status == UpdateStatus.READY:
        return "ok", "Ready to install"
    if status == UpdateStatus.INSTALLING:
        return "info", "Installing -- the app will close shortly..."
    if status == UpdateStatus.ERROR:
        return "error", settings.update_error_message or "Update check failed"
    return "neutral", f"v{VERSION}"


# Maps UpdateStatus -> (button label, enabled).
def _button_state(status: UpdateStatus) -> tuple[str, bool]:
    if status in (UpdateStatus.IDLE, UpdateStatus.UP_TO_DATE, UpdateStatus.ERROR):
        return "Check Now", True
    if status == UpdateStatus.AVAILABLE:
        return "Update", True
    if status == UpdateStatus.READY:
        return "Install", True
    return "...", False  # CHECKING / DOWNLOADING / INSTALLING


def _on_update_button_clicked(status: UpdateStatus) -> None:
    if status in (UpdateStatus.IDLE, UpdateStatus.UP_TO_DATE, UpdateStatus.ERROR):
        updater.update_manager.start_check(VERSION)
    elif status == UpdateStatus.AVAILABLE:
        updater.update_manager.start_download()
    elif status == UpdateStatus.READY:
        # Only quit if the installer handoff succeeded. Same app_shall_exit
        # path as titlebar.py's close, so before_exit teardown still runs.
        if updater.update_manager.install_and_quit():
            hi.get_runner_params().app_shall_exit = True


def _render_updates(ctx: PanelContext) -> None:
    theme = ctx.theme
    settings = ctx.state.settings
    with widgets.card(theme, "settings-updates", size=(0, 0)):
        widgets.section_title("Updates")

        level, label = _status_badge_args(settings.update_status, settings)
        widgets.status_badge(theme, level, label)
        if not updater.repo_configured():
            widgets.muted_text(
                theme,
                "No GitHub repository is published for this project yet -- checks will fail until one exists.",
            )

        changed, settings.check_for_updates_on_launch = widgets.labeled_toggle(
            theme, "Check for updates on launch", settings.check_for_updates_on_launch, settings.reduce_motion
        )
        if changed:
            settings_store.save(ctx.state)

        imgui.spacing()
        widgets.muted_text(theme, settings.last_checked_display)

        btn_label, btn_enabled = _button_state(settings.update_status)
        if not btn_enabled:
            imgui.begin_disabled()
        clicked = imgui.button(f"{fa.ICON_FA_SYNC}  {btn_label}")
        if not btn_enabled:
            imgui.end_disabled()
        if clicked:
            _on_update_button_clicked(settings.update_status)

        imgui.spacing()
        if updater.repo_configured():
            widgets.hyperlink(theme, "View releases on GitHub", updater.releases_url())
        else:
            widgets.muted_text(theme, "View releases on GitHub (unavailable -- no repository published yet)")


def render_auto_update_prompt(ctx: PanelContext) -> None:
    """Check-on-launch prompt: "vX is available. Update now?" with
    Update Now / Later -- Later skips it for the session without disabling
    the setting. Call once per frame regardless of active panel.
    """
    theme = ctx.theme
    settings = ctx.state.settings

    if settings.auto_update_prompt_pending and not imgui.is_popup_open(_AUTO_UPDATE_POPUP_ID):
        imgui.open_popup(_AUTO_UPDATE_POPUP_ID)

    imgui.set_next_window_size(imgui.ImVec2(380, 0), imgui.Cond_.appearing)
    flags = imgui.WindowFlags_.always_auto_resize | imgui.WindowFlags_.no_resize
    opened, _ = imgui.begin_popup_modal(_AUTO_UPDATE_POPUP_ID, None, flags)
    if opened:
        imgui.text_wrapped(f"Shattered Gaming Overlay v{settings.update_latest_version} is available.")
        widgets.muted_text(theme, "Choosing Later skips this for the rest of the session.")
        imgui.spacing()
        if imgui.button("Update Now", imgui.ImVec2(140, 0)):
            updater.update_manager.dismiss_auto_prompt()
            updater.update_manager.start_download()
            imgui.close_current_popup()
        imgui.same_line()
        if imgui.button("Later", imgui.ImVec2(100, 0)):
            updater.update_manager.dismiss_auto_prompt()
            imgui.close_current_popup()
        imgui.end_popup()


def render_update_flow_popup(ctx: PanelContext) -> None:
    """Global modal covering the rest of the update flow once a download has
    started -- DOWNLOADING -> READY -> INSTALLING -- regardless of active
    panel, so switching away from Settings mid-update doesn't strand the
    user. Driven off `settings.update_status` (_UPDATE_FLOW_STATUSES), so it
    covers both the auto-prompt and the Settings card's own button.
    """
    theme = ctx.theme
    settings = ctx.state.settings
    active = settings.update_status in _UPDATE_FLOW_STATUSES

    if active and not imgui.is_popup_open(_UPDATE_FLOW_POPUP_ID):
        imgui.open_popup(_UPDATE_FLOW_POPUP_ID)

    imgui.set_next_window_size(imgui.ImVec2(380, 0), imgui.Cond_.appearing)
    flags = imgui.WindowFlags_.always_auto_resize | imgui.WindowFlags_.no_resize
    opened, _ = imgui.begin_popup_modal(_UPDATE_FLOW_POPUP_ID, None, flags)
    if opened:
        if not active:
            # Status moved past this range (e.g. straight to ERROR) while the
            # popup was still open -- close rather than render stale content.
            imgui.close_current_popup()
        elif settings.update_status == UpdateStatus.DOWNLOADING:
            imgui.text(f"Downloading v{settings.update_latest_version}...")
            imgui.spacing()
            pct = settings.update_download_pct / 100.0
            imgui.progress_bar(pct, imgui.ImVec2(340, 0), f"{settings.update_download_pct}%")
        elif settings.update_status == UpdateStatus.READY:
            imgui.text_wrapped(f"Shattered Gaming Overlay v{settings.update_latest_version} is ready to install.")
            widgets.muted_text(theme, "The app will close to finish installing.")
            imgui.spacing()
            if imgui.button("Install", imgui.ImVec2(140, 0)):
                # Reuses _on_update_button_clicked's install_and_quit()
                # handoff rather than reimplementing it.
                _on_update_button_clicked(UpdateStatus.READY)
        elif settings.update_status == UpdateStatus.INSTALLING:
            imgui.text("Installing -- the app will close shortly...")
        imgui.end_popup()


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    imgui.text(f"{fa.ICON_FA_COG}  Settings")
    imgui.spacing()

    _render_appearance(ctx)
    imgui.spacing()
    window_select.render_section(ctx)
    imgui.spacing()
    _render_window_behavior(ctx)
    imgui.spacing()
    _render_updates(ctx)
