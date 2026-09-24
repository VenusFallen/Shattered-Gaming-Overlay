"""titlebar.py -- themed custom title bar for the borderless Companion window.
main.py runs with `app_window_params.borderless = True`, dropping the OS
title bar; this module rebuilds a themed replacement, with drag/resize/
minimize/maximize/close hand-rolled via raw Win32 calls instead of Hello
ImGui's own borderless zones (see `render_resize_grip()` for why).
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
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_longlong)
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)

# comctl32's SetWindowSubclass, not a raw GWLP_WNDPROC swap -- lets us intercept one message (WM_NCCALCSIZE) on
# a window GLFW owns and created, without touching or needing to save/restore GLFW's own window procedure;
# everything we don't handle chains straight through via DefSubclassProc.
comctl32 = ctypes.windll.comctl32
_UINT_PTR = ctypes.c_size_t
_DWORD_PTR = ctypes.c_size_t
_LRESULT = ctypes.c_ssize_t
_SUBCLASSPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, _UINT_PTR, _DWORD_PTR)
comctl32.SetWindowSubclass.restype = wintypes.BOOL
comctl32.SetWindowSubclass.argtypes = (wintypes.HWND, _SUBCLASSPROC, _UINT_PTR, _DWORD_PTR)
comctl32.RemoveWindowSubclass.restype = wintypes.BOOL
comctl32.RemoveWindowSubclass.argtypes = (wintypes.HWND, _SUBCLASSPROC, _UINT_PTR)
comctl32.DefSubclassProc.restype = _LRESULT
comctl32.DefSubclassProc.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_SW_HIDE = 0
_SW_RESTORE = 9
_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2
_HTBOTTOMRIGHT = 17
_GWL_STYLE = -16
_WS_THICKFRAME = 0x00040000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_WM_NCCALCSIZE = 0x0083
_WM_NCDESTROY = 0x0082
_NCCALCSIZE_SUBCLASS_ID = 1

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


def ensure_resizable_frame_style() -> None:
    """Call once at startup. GLFW creates this borderless window without WS_THICKFRAME (borderless_resizable=False
    tells it not to add its own corner-resize handling), but our own WM_NCLBUTTONDOWN/HTBOTTOMRIGHT resize in
    _start_native_resize() needs that style bit for DefWindowProc to actually enter a native sizing loop, not
    just accept and immediately no-op the message. SetWindowPos with SWP_FRAMECHANGED afterward is required too --
    changing GWL_STYLE alone doesn't make Windows recalculate non-client hit-testing to honor the new style."""
    hwnd = _hwnd()
    if not hwnd:
        return
    style = user32.GetWindowLongPtrW(hwnd, _GWL_STYLE)
    if style & _WS_THICKFRAME:
        return
    user32.SetWindowLongPtrW(hwnd, _GWL_STYLE, style | _WS_THICKFRAME)
    flags = _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, flags)


def _nccalcsize_subclass_proc(hwnd, msg, wparam, lparam, uid_subclass, ref_data):
    # WM_NCCALCSIZE's lParam points to a RECT either way -- a bare RECT* when wParam is FALSE, or
    # NCCALCSIZE_PARAMS* when TRUE, whose first field (rgrc[0]) is that same RECT at the same offset.
    # Claiming the top 1px as non-client (instead of the zero-margin rect GLFW's own borderless handling
    # would return) is what stops Windows 10/11's DWM from painting its thickframe accent line across the
    # top of the client area -- a known quirk for WS_THICKFRAME windows with no declared non-client margin.
    if msg == _WM_NCCALCSIZE and lparam:
        rect = ctypes.cast(lparam, ctypes.POINTER(_RECT)).contents
        rect.top += 1
        return 0
    if msg == _WM_NCDESTROY:
        comctl32.RemoveWindowSubclass(hwnd, _nccalcsize_subclass_proc_ref, uid_subclass)
    return comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)


# Kept alive for the process's lifetime -- ctypes callback objects that get garbage collected before Windows
# is done calling them crash or corrupt memory, and this window (and its subclass) lives until the app exits.
_nccalcsize_subclass_proc_ref = _SUBCLASSPROC(_nccalcsize_subclass_proc)


def install_nccalcsize_fix() -> None:
    """Call once at startup, alongside ensure_resizable_frame_style() -- see _nccalcsize_subclass_proc's own
    comment for what this actually fixes and why."""
    hwnd = _hwnd()
    if not hwnd:
        return
    comctl32.SetWindowSubclass(hwnd, _nccalcsize_subclass_proc_ref, _NCCALCSIZE_SUBCLASS_ID, 0)


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


def _resync_mouse_up_after_native_loop() -> None:
    # SendMessageW below blocks until the OS's own modal drag/resize loop exits on mouse-up -- that release
    # never flows back through ImGui's normal input path (GLFW's callback never sees it, since Windows handled
    # the whole gesture natively), so ImGui's own io.mouse_down[0] is left stuck true. The next real click then
    # looks like "already held" to ImGui, so its down-transition never fires -- explains the every-other-attempt
    # pattern reported live. By the time SendMessageW returns, the button is definitely up, so tell ImGui so.
    imgui.get_io().add_mouse_button_event(0, False)


def _start_native_drag() -> None:
    hwnd = _hwnd()
    if not hwnd:
        return
    user32.ReleaseCapture()
    user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, 0)
    _resync_mouse_up_after_native_loop()


def _start_native_resize() -> None:
    hwnd = _hwnd()
    if not hwnd:
        return
    user32.ReleaseCapture()
    user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTBOTTOMRIGHT, 0)
    _resync_mouse_up_after_native_loop()


def _bar_button(theme, str_id: str, icon: str, hover_color, size: float) -> bool:
    # Transparent until hovered, then a themed fill (danger-red for Close) plus the icon -- never color alone.
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
    # No double-click-to-maximize: mouse-down already fires a blocking native move-loop, so a second click never arrives.
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
    # Bottom-right resize handle, hand-rolled via WM_NCLBUTTONDOWN/HT* (same as the title bar drag) instead of
    # Hello ImGui's built-in borderless_resizable corner, which tracks drag state manually at the ImGui level and
    # has a reproducible bug: the window can keep following the cursor after mouse-up, needing a second click to
    # stop. Call once per frame regardless of active panel; skipped while maximized.
    if _is_maximized():
        return

    theme = ctx.theme
    viewport = imgui.get_main_viewport()
    x = viewport.pos.x + viewport.size.x - _RESIZE_GRIP_SIZE
    y = viewport.pos.y + viewport.size.y - _RESIZE_GRIP_SIZE

    # Raw mouse position/click state, not an invisible_button's hover/activation -- this corner sits right where
    # the main window's own (always-present) scrollbar track lives, and that scrollbar wins ImGui's normal
    # item-hover resolution over anything placed here, silently swallowing the click instead of resizing.
    mouse = imgui.get_io().mouse_pos
    hovered = x <= mouse.x <= x + _RESIZE_GRIP_SIZE and y <= mouse.y <= y + _RESIZE_GRIP_SIZE
    if hovered:
        imgui.set_mouse_cursor(imgui.MouseCursor_.resize_nwse)
        if imgui.is_mouse_clicked(0):
            _start_native_resize()

    # Three diagonal lines, the usual corner-resize-grip convention -- always visible, not hover-only like Hello ImGui's own.
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
