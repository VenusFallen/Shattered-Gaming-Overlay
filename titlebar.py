"""titlebar.py -- themed custom title bar for the borderless Companion window.

main.py runs with `app_window_params.borderless = True`, which drops the OS
title bar (drag region + min/max/close). This module rebuilds a themed
replacement strip that shell.py renders at the top of every frame.

`borderless_movable`/`_resizable`/`_closable` are all left False: Hello
ImGui's generic drag/resize zones are unstyled, have no min/max affordance,
and (resize specifically) had a real bug -- see `render_resize_grip()`'s
docstring. Drag and resize are both hand-rolled instead via the Win32
"release capture, send WM_NCLBUTTONDOWN/HT*" trick, and close/min/max are
themed buttons instead of Hello ImGui's generic ones.

imgui_bundle has no minimize/maximize/close call of its own; those go
through raw Win32 `ShowWindow` via ctypes (same pattern as input_hooks.py).
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
_HTBOTTOMRIGHT = 17

BAR_HEIGHT_UNSCALED = 38.0
_RESIZE_GRIP_SIZE = 16.0


def _hwnd() -> int:
    """The Companion window's HWND. Falls back to a title lookup for the rare
    frame where GetActiveWindow briefly returns nothing (e.g. right after a
    programmatic ShowWindow call)."""
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
    # Hide-to-tray, not exit -- tray_icon.py owns real exit via its Quit item
    # (same app_shall_exit flag). Falls back to a real exit if the tray icon
    # never came up. `settings.close_minimizes_to_tray` opts out entirely.
    if settings.close_minimizes_to_tray and tray_icon.is_running():
        hwnd = _hwnd()
        if hwnd:
            # Minimize before hiding: a bare SW_HIDE never fires
            # WM_SIZE/SIZE_MINIMIZED, so GLFW never sets iconified and Hello
            # ImGui keeps running the render loop while fully invisible.
            user32.ShowWindow(hwnd, _SW_MINIMIZE)
            user32.ShowWindow(hwnd, _SW_HIDE)
        return
    # Via Hello ImGui's exit flag, not WM_CLOSE directly, so main.py's
    # before_exit shutdown path (tears down the bind-capture hook) still runs.
    hi.get_runner_params().app_shall_exit = True


def _start_native_drag() -> None:
    hwnd = _hwnd()
    if not hwnd:
        return
    user32.ReleaseCapture()
    user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)


def _start_native_resize() -> None:
    hwnd = _hwnd()
    if not hwnd:
        return
    user32.ReleaseCapture()
    user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTBOTTOMRIGHT, 0)


def _bar_button(theme, str_id: str, icon: str, hover_color, size: float) -> bool:
    """One title-bar glyph button: transparent until hovered, then a themed
    fill (danger-red for Close, neutral for the rest) plus the icon, so
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
    # Suppress scrollbar: rounding-driven 1-2px overflow was otherwise enough
    # to spawn a vertical scrollbar over the button column.
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
    # No double-click-to-maximize: the first mouse-down already fires
    # _start_native_drag into a blocking Win32 move-loop, so a second click
    # never arrives as a distinguishable double-click. Fixing that needs a
    # real WM_NCHITTEST subclass, out of scope here.
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


def render_resize_grip(ctx: PanelContext) -> None:
    """Bottom-right resize handle, hand-rolled the same native-Win32 way
    the title bar drag above is (`WM_NCLBUTTONDOWN`/`HT*`, handing the
    whole drag off to Windows' own move-loop) instead of using Hello
    ImGui's built-in `borderless_resizable` corner (left False in main.py).

    That built-in implementation tracks its own manual drag state at the
    ImGui level rather than handing off to the OS -- per its own docs, "a
    drag zone is displayed at the bottom-right... when the mouse is over
    it" -- and has a real, reproducible bug: clicking the corner could
    start the window continuously following the cursor even after the
    mouse button was released, needing a second click to stop it. Found
    live 2026-09-17. The native WM_NCLBUTTONDOWN approach can't have this
    class of bug -- once sent, Windows' own move-loop owns the drag
    entirely until the OS itself sees the button released, the same
    guarantee `_start_native_drag()` already relies on for the title bar.

    Drawn every frame regardless of active panel (call this once from
    shell.py's render_frame, same as titlebar.render()), positioned via the
    main viewport's absolute screen rect so it sits in the true corner
    regardless of which panel/child region is currently under it. Skipped
    while maximized -- nothing to resize."""
    if _is_maximized():
        return

    theme = ctx.theme
    viewport = imgui.get_main_viewport()
    x = viewport.pos.x + viewport.size.x - _RESIZE_GRIP_SIZE
    y = viewport.pos.y + viewport.size.y - _RESIZE_GRIP_SIZE

    imgui.set_cursor_screen_pos(imgui.ImVec2(x, y))
    imgui.push_id("resize-grip")
    imgui.invisible_button("##resize-grip", imgui.ImVec2(_RESIZE_GRIP_SIZE, _RESIZE_GRIP_SIZE))
    hovered = imgui.is_item_hovered()
    if hovered:
        imgui.set_mouse_cursor(imgui.MouseCursor_.resize_nwse)
    if imgui.is_item_activated():
        _start_native_resize()
    imgui.pop_id()

    # Minimal visual affordance -- three short diagonal lines, the same
    # corner-resize-grip convention most desktop apps use, drawn on top of
    # the invisible button so it stays discoverable without a hover-only
    # reveal (unlike Hello ImGui's own version, which only appears on hover).
    draw_list = imgui.get_window_draw_list()
    color = imgui.get_color_u32(theme.accent_text if hovered else theme.border_strong)
    pad = 3.0
    for i in range(3):
        offset = i * 4.0
        draw_list.add_line(
            imgui.ImVec2(x + _RESIZE_GRIP_SIZE - pad - offset, y + _RESIZE_GRIP_SIZE - pad),
            imgui.ImVec2(x + _RESIZE_GRIP_SIZE - pad, y + _RESIZE_GRIP_SIZE - pad - offset),
            color,
            1.5,
        )
