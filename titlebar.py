"""titlebar.py -- themed custom title bar for the borderless Companion window.

main.py runs the Companion window with `app_window_params.borderless = True`
(see that module's docstring for the introspected `AppWindowParams` fields
this relies on). Removing OS chrome removes the OS-drawn title bar along
with it -- its drag region and its minimize/maximize/close buttons -- so
this module rebuilds a slim, themed replacement strip that shell.py renders
at the very top of every frame, above the sidebar+content row.

Why this is hand-rolled instead of using Hello ImGui's own borderless
support: `AppWindowParams.borderless_movable` / `_resizable` / `_closable`
exist (confirmed in the bundled `hello_imgui.pyi`) and do provide a generic
drag zone / resize corner / close button for free -- but they're unstyled,
have no minimize/maximize affordance at all, and the docstring only
guarantees a drag zone "at the top of the window", not that it composites
correctly *underneath* real ImGui buttons drawn in that same strip. Rather
than gamble on that interaction, this bar avoids the ambiguity entirely:

  - `borderless_movable`  is left False -- dragging is implemented directly
    below via the classic Win32 "release capture, then send the titlebar a
    WM_NCLBUTTONDOWN/HTCAPTION click" trick (`_start_native_drag`), fired
    only from an `imgui.invisible_button` region that is geometrically
    disjoint from the minimize/maximize/close buttons -- so there is no
    hover/hit-test race to reason about, by construction, not by assumption.
  - `borderless_resizable` is left True -- edge/corner resizing is exactly
    what Hello ImGui's own resize-corner zone is for, and it doesn't
    overlap this top strip at all, so there's nothing to hand-roll there.
  - `borderless_closable`  is left False -- this bar draws its own themed
    close button instead of Hello ImGui's generic one.

imgui_bundle's Python binding has no "minimize/maximize/close the window"
call of its own (only the RunnerParams-level `app_shall_exit` flag, used
below for Close). Minimize and maximize/restore are therefore real Win32
`ShowWindow` calls via `ctypes`, matching the pattern input_hooks.py already
uses elsewhere in this project for other user-mode Win32 APIs.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from imgui_bundle import hello_imgui as hi
from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

from panel_context import PanelContext
from tray_icon import tray_icon
from version import WINDOW_TITLE

user32 = ctypes.windll.user32
user32.GetActiveWindow.restype = wintypes.HWND
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.IsZoomed.argtypes = (wintypes.HWND,)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ReleaseCapture.restype = wintypes.BOOL
user32.SendMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

_SW_HIDE = 0
_SW_RESTORE = 9
_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2

BAR_HEIGHT_UNSCALED = 38.0


def _hwnd() -> int:
    """The Companion window's own HWND. `GetActiveWindow` is scoped to this
    process's own message queue -- there is exactly one top-level window in
    this process, so this is unambiguous whenever the window has focus.
    Falls back to a title lookup for the rare frame where it briefly
    doesn't (e.g. right after a programmatic ShowWindow call)."""
    hwnd = user32.GetActiveWindow()
    if not hwnd:
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    return hwnd


def _is_maximized() -> bool:
    hwnd = _hwnd()
    return bool(hwnd) and bool(user32.IsZoomed(hwnd))


def _minimize() -> None:
    hwnd = _hwnd()
    if hwnd:
        user32.ShowWindow(hwnd, _SW_MINIMIZE)


def _toggle_maximize() -> None:
    hwnd = _hwnd()
    if not hwnd:
        return
    user32.ShowWindow(hwnd, _SW_RESTORE if user32.IsZoomed(hwnd) else _SW_MAXIMIZE)


def _close(settings) -> None:
    # Hide-to-tray, not exit: the tray icon (tray_icon.py) is what owns a
    # real, full application exit now (its Quit menu item sets this same
    # app_shall_exit flag). Falls back to actually exiting only if the tray
    # icon never came up (e.g. Shell_NotifyIcon failed) -- otherwise closing
    # the X button would strand the user with no way to get the window back.
    # `settings.close_minimizes_to_tray` (panels/settings.py's Window
    # behavior card) lets the user opt out of hide-to-tray entirely -- when
    # off, skip straight to the real-exit path below even if the tray icon
    # is running.
    if settings.close_minimizes_to_tray and tray_icon.is_running():
        hwnd = _hwnd()
        if hwnd:
            # Minimize before hiding, not a bare SW_HIDE: Windows only fires
            # WM_SIZE/SIZE_MINIMIZED (what GLFW's window proc uses to set its
            # own iconified flag, which is what makes Hello ImGui skip the
            # render loop -- see window_select.py's focus-tracking fix, which
            # exists precisely because that skip stops _show_gui from firing)
            # on a real minimize transition, not a plain hide. A raw SW_HIDE
            # alone would leave the render loop running at full/idle rate
            # while completely invisible. SW_RESTORE from the tray's IsIconic
            # check already handles un-minimizing and un-hiding in one call.
            user32.ShowWindow(hwnd, _SW_MINIMIZE)
            user32.ShowWindow(hwnd, _SW_HIDE)
        return
    # Goes through Hello ImGui's own exit flag rather than posting WM_CLOSE
    # directly, so the normal shutdown path (main.py's before_exit callback,
    # which tears down the bind-capture hook) still runs.
    hi.get_runner_params().app_shall_exit = True


def _start_native_drag() -> None:
    hwnd = _hwnd()
    if not hwnd:
        return
    user32.ReleaseCapture()
    user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)


def _bar_button(theme, str_id: str, icon: str, hover_color, size: float) -> bool:
    """One title-bar glyph button: transparent until hovered, then a themed
    fill (danger-red for Close, a neutral hover tint for the others -- the
    standard Windows/Discord-style convention), plus the icon itself, so
    hover state is never carried by color alone."""
    imgui.push_id(str_id)
    imgui.push_style_color(imgui.Col_.button, (0.0, 0.0, 0.0, 0.0))
    imgui.push_style_color(imgui.Col_.button_hovered, hover_color)
    imgui.push_style_color(imgui.Col_.button_active, hover_color)
    imgui.push_style_var(imgui.StyleVar_.frame_rounding, 0.0)
    clicked = imgui.button(icon, imgui.ImVec2(size, size))
    imgui.pop_style_var()
    imgui.pop_style_color(3)
    imgui.pop_id()
    return clicked


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    bar_h = BAR_HEIGHT_UNSCALED
    btn_w = bar_h * 1.15

    imgui.push_style_color(imgui.Col_.child_bg, theme.bg_sidebar)
    # NoScrollbar/NoScrollWithMouse: this strip's content (icon/name + three
    # square buttons) is sized to fit bar_h exactly, but without suppressing
    # the scrollbar explicitly, a 1-2px rounding-driven overflow was enough
    # to spawn a real vertical scrollbar over the button column (caught only
    # by an actual screenshot, not by reasoning about the layout math).
    no_scroll = imgui.WindowFlags_.no_scrollbar | imgui.WindowFlags_.no_scroll_with_mouse
    imgui.begin_child("titlebar", imgui.ImVec2(0, bar_h), imgui.ChildFlags_.none, no_scroll)

    bar_w = imgui.get_content_region_avail().x
    right_w = btn_w * 3
    drag_w = max(0.0, bar_w - right_w)
    cursor_start = imgui.get_cursor_screen_pos()

    # --- drag region (left/middle): invisible hit-target + decorative
    # icon/name drawn on top via the draw list (non-interactive, so it can
    # never steal the drag region's hover/click). ---
    imgui.invisible_button("##titlebar-drag", imgui.ImVec2(drag_w, bar_h))
    # Deliberately no double-click-to-maximize here: the very first mouse-down
    # of ANY click already fires `_start_native_drag`, which hands the click
    # off to a blocking Win32 modal move-loop before a second click could
    # ever be distinguished from it -- confirmed by actually trying it (a
    # synthetic double-click landed as two independent drags, never a
    # double-click), not assumed. Doing this properly would mean permanently
    # reporting HTCAPTION for this region from a real WM_NCHITTEST subclass
    # instead of a per-press SendMessage, which is a bigger, riskier change
    # (a live WNDPROC hook via ctypes) than this bar's scope calls for.
    if imgui.is_item_activated():
        _start_native_drag()

    draw_list = imgui.get_window_draw_list()
    label = f"{fa.ICON_FA_GAMEPAD}  Shattered Gaming Overlay"
    text_size = imgui.calc_text_size(label)
    text_y = cursor_start.y + (bar_h - text_size.y) * 0.5
    draw_list.add_text(
        imgui.ImVec2(cursor_start.x + 14.0, text_y),
        imgui.get_color_u32(theme.text_primary),
        label,
    )

    # --- window controls (right): minimize / maximize-restore / close ---
    imgui.same_line(0, 0)
    neutral_hover = theme.nav_hover_bg if theme.nav_hover_bg[3] > 0.0 else (1.0, 1.0, 1.0, 0.08)
    if _bar_button(theme, "titlebar-min", fa.ICON_FA_WINDOW_MINIMIZE, neutral_hover, btn_w):
        _minimize()
    imgui.same_line(0, 0)
    max_icon = fa.ICON_FA_WINDOW_RESTORE if _is_maximized() else fa.ICON_FA_WINDOW_MAXIMIZE
    if _bar_button(theme, "titlebar-max", max_icon, neutral_hover, btn_w):
        _toggle_maximize()
    imgui.same_line(0, 0)
    close_hover = (theme.danger[0], theme.danger[1], theme.danger[2], 0.85)
    if _bar_button(theme, "titlebar-close", fa.ICON_FA_TIMES, close_hover, btn_w):
        _close(ctx.state.settings)

    imgui.end_child()
    imgui.pop_style_color()
