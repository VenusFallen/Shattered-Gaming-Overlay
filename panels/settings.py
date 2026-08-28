"""panels/settings.py -- Settings panel: theme, UI scale, reduce motion,
Target Window (folded in from the former standalone Window Select tab -- see
panels/window_select.py), and the Updates card. Theme/scale/reduce-motion are
fully live (see theme.apply_theme, called every frame from shell.py using
this state).

Updates is wired to a real backend now -- see updater.py (the self-updater
against GitHub Releases). `_render_updates` below only ever
reads `ctx.state.settings.update_*` (written once per frame by
`updater.update_manager.sync_to()`, called from main.py's `_show_gui`) and
issues commands via `updater.update_manager` -- it never touches
`updater.update_manager`'s internals directly. `render_auto_update_prompt`
is the automatic check-on-launch popup; shell.py calls it unconditionally
every frame (same placement as titlebar.render(ctx)) so it can show
regardless of which panel is active.

Note: no GitHub repo/release exists yet for this project (see updater.py's
module docstring) -- the Check/Update/Install flow below is real and
correct, but has never been exercised against an actual release. See
updater.py's own notes on exactly what could and couldn't be verified
without a live release.

About (program description/version/credit) lives in its own top-level nav
entry now -- see panels/about.py -- not here.
"""

from __future__ import annotations

import colorsys
import time

from imgui_bundle import hello_imgui as hi
from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import theme as theme_module
import updater
import widgets
from app_state import UpdateStatus
from panel_context import PanelContext
from panels import window_select
from version import VERSION

_AUTO_UPDATE_POPUP_ID = "Update Available"

_THEME_CARD = 84.0
_THEME_SWATCH_ROW_H = 26.0
_THEME_SWATCH_GAP = 3.0

# Rainbow preview speed for the Color Cycle card specifically -- a fast,
# continuous hue sweep across all four swatches (offset from each other) so
# that one card visually reads as "this theme is animated" in the picker,
# independent of whatever the user's actual Color A/B are configured to.
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
    """Horizontal row of theme cards -- each shows a strip of that theme's
    own palette swatches (background, accent, accent_active, text) with the
    display name below, same "see it before you pick it" idea as the
    crosshair style picker in panels/overlay.py. The Color Cycle card is the
    one exception: its swatches are a live animated rainbow rather than
    fixed colors, signaling at a glance that this theme is dynamic."""
    new_name = current
    draw_list = imgui.get_window_draw_list()
    u32 = lambda rgba: imgui.color_convert_float4_to_u32(imgui.ImVec4(*rgba))  # noqa: E731
    # push_text_wrap_pos takes a window-LOCAL x, not a screen-space one --
    # everything else in this function works in screen space (from
    # get_cursor_screen_pos()), so every wrap-pos call below has to subtract
    # this to convert. Passing the raw screen-space x here was the original
    # bug: it wrapped at some point way off to the right of the actual
    # window, i.e. never, which is why names overflowed their cards.
    window_x = imgui.get_window_pos().x

    names = theme_module.theme_names()
    # Wrap to a new row rather than overflowing the card's width -- there
    # are enough themes now (7) that they don't all fit on one line at a
    # size big enough to hold 4 swatches + a name. Computed once, before the
    # loop, from the width available right here (not reduced by anything
    # this loop itself draws).
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

        settings.theme_name = _theme_picker(theme, settings.theme_name)

        if settings.theme_name == "color_cycle":
            imgui.spacing()
            widgets.muted_text(
                theme,
                "Slowly, continuously drifts the accent color back and forth between two colors you pick "
                "-- like a slow RGB keyboard breathing effect, not a rainbow cycle.",
            )
            imgui.spacing()
            _, settings.cycle_color_a = widgets.hex_color_picker(theme, "cycle-color-a", "Color A", settings.cycle_color_a)
            _, settings.cycle_color_b = widgets.hex_color_picker(theme, "cycle-color-b", "Color B", settings.cycle_color_b)
            imgui.spacing()
            imgui.set_next_item_width(260)
            # Bounds are deliberately both "slow" -- even the fast end of
            # this range (15s per full back-and-forth cycle) reads as an
            # ambient drift, never a flash/strobe. See app_state.SettingsState
            # .cycle_period_sec and theme.color_cycle_phase.
            _, settings.cycle_period_sec = imgui.slider_float(
                "Cycle speed##cycle", settings.cycle_period_sec, 15.0, 45.0, "%.0f sec per full cycle"
            )
            if settings.reduce_motion:
                widgets.muted_text(theme, "Reduce motion is on -- the color is frozen and will not drift.")

        imgui.spacing()
        _, settings.reduce_motion = widgets.labeled_toggle(
            theme,
            "Reduce motion",
            settings.reduce_motion,
            settings.reduce_motion,
            tooltip="Disables toggle-switch animation and other future motion effects.",
        )


# Maps UpdateStatus -> (badge level, badge label). Mirrors R9Tools'
# `_appStatusLbl` text-per-state mapping (see its panels/settings.py).
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


# Maps UpdateStatus -> (button label, enabled). Mirrors R9Tools'
# `_appBtnClicked`'s idle/up_to_date/error -> available -> ready state
# machine (see its panels/settings.py).
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
        # Only actually quit if the installer handoff succeeded -- mirrors
        # titlebar.py's own `app_shall_exit = True` close path so the
        # normal before_exit teardown still runs (see updater.py's
        # install_and_quit docstring for why this must happen from here,
        # not from inside updater.py itself, which has no UI dependency).
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

        _, settings.check_for_updates_on_launch = widgets.labeled_toggle(
            theme, "Check for updates on launch", settings.check_for_updates_on_launch, settings.reduce_motion
        )

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
    """The automatic check-on-launch prompt: "Shattered Gaming Overlay vX is
    available. Update now?" with Update Now / Later, matching R9Tools'
    README-documented behavior ("Choosing 'Later' skips it for that session
    without turning the setting off"). Call once per frame regardless of
    which panel is active -- see shell.py's call site, placed the same way
    titlebar.render(ctx) is.
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


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    imgui.text(f"{fa.ICON_FA_COG}  Settings")
    imgui.spacing()

    _render_appearance(ctx)
    imgui.spacing()
    window_select.render_section(ctx)
    imgui.spacing()
    _render_updates(ctx)
