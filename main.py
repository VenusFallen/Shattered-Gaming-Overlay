"""main.py -- entry point for Shattered Gaming Overlay's Companion window.

Launches ONLY the Companion window (the normal settings/config UI:
Dashboard, Remapper, Macros, Profiles, Window Select, Overlay toggles,
Settings) -- an ordinary desktop window, resizable, taskbar-visible, not
always-on-top, not click-through. It is built on Dear ImGui via
`imgui_bundle`'s "Hello ImGui" (`immapp`/`hello_imgui`), which is the
project-wide UI toolkit (see .claude/agents/ui-agent.md).

Borderless, not chromeless: `app_window_params.borderless = True` below
removes the OS-drawn title bar (and its OS min/max/close buttons + drag
region) for a modern "companion app" look (Discord/Playnite-style), at 3/4
the previous default size (960x600 vs. 1280x800). shell.py replaces the lost
chrome with a themed custom title bar (see titlebar.py for the full
rationale, including which of Hello ImGui's own `borderless_movable` /
`_resizable` / `_closable` fields -- confirmed by introspecting
`hi.AppWindowParams` -- are used vs. hand-rolled).

Renderer backend note: the project spec calls for DX11. The `imgui-bundle`
wheel actually installed from PyPI (confirmed by introspecting
`hello_imgui.RendererBackendType`/`PlatformBackendType` and then trying to
run with `direct_x11` -- see the build's own error message) is compiled
with only `HELLOIMGUI_HAS_OPENGL3` + GLFW3; DirectX11/DirectX12 are opt-in
CMake flags on the C++ side that the published cross-platform pip wheel
does not enable, regardless of OS. Requesting `direct_x11` here raises
`IM_ASSERT("DirectX11 backend is not available!")` at startup. This window
therefore runs on `RendererBackendType.open_gl3` instead, which IS compiled
in. This has no bearing on the project's anti-cheat safety properties --
the Companion window is an ordinary desktop window that never touches a
game process either way -- and it does not block the HUD overlay (see
hud_overlay.py), which per ui-agent.md needs a hand-rolled DirectComposition
swap chain via ctypes regardless of what Hello ImGui's backend selector
supports. See the task report for the full explanation and the options if
true DX11 parity is wanted later (building imgui_bundle from source with
`-DHELLOIMGUI_HAS_DIRECTX11=ON`).

This module also starts/stops the HUD overlay's own background thread
(hud_overlay.py) alongside the Companion window's lifecycle -- but the HUD
overlay itself is a separate, click-through, DirectComposition-backed,
non-injecting window described in .claude/agents/ui-agent.md, entirely
distinct from the Companion window built below. Nothing about the HUD
overlay is rendered as part of this window's own ImGui frame.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from imgui_bundle import hello_imgui as hi
from imgui_bundle import imgui
from imgui_bundle import immapp

import shell
import theme as theme_module
from app_state import new_app_state
from hud_overlay import hud_overlay
from key_capture import capture_service
from version import WINDOW_TITLE

if not sys.platform.startswith("win"):
    raise SystemExit("Shattered Gaming Overlay's Companion window is Windows-only.")

_ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.ico"


def _set_window_icon() -> None:
    # Hello ImGui's RunnerParams/AppWindowParams have no icon field at all
    # (confirmed by introspection, same as the DX11 backend check above) --
    # set it the plain Win32 way, reusing titlebar.py's FindWindowW-by-title
    # lookup pattern since Hello ImGui doesn't hand back its own hwnd either.
    if not _ICON_PATH.exists():
        return
    user32 = ctypes.windll.user32
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
    WM_SETICON, ICON_BIG, ICON_SMALL = 0x0080, 1, 0
    hwnd = user32.FindWindowW(None, WINDOW_TITLE)
    if not hwnd:
        return
    hicon = user32.LoadImageW(None, str(_ICON_PATH), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if hicon:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon)
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon)


def _post_init() -> None:
    # Keyboard navigation (arrow keys / Tab to move focus, Enter/Space to
    # activate) -- a real, not-cosmetic accessibility feature to turn on by
    # default for a companion app whose whole reason to exist is accessibility.
    io = imgui.get_io()
    io.config_flags |= imgui.ConfigFlags_.nav_enable_keyboard
    _set_window_icon()
    # Start the HUD overlay's own background thread + window alongside the
    # Companion window. It starts idle (crosshair disabled by default, see
    # app_state.CrosshairState) and stays running for the whole app
    # lifetime -- per-frame update_crosshair() calls in _show_gui() below
    # are what actually turn it on/off live.
    hud_overlay.start()


def _before_exit() -> None:
    # Tear down the bind-capture hook (if it was ever started) rather than
    # leaving it installed until process teardown.
    capture_service.shutdown()
    # Stop the HUD overlay's thread/window/DX11+DComp resources cleanly so
    # nothing outlives the Companion window's own process.
    hud_overlay.stop()


def main() -> None:
    app_state = new_app_state()

    runner_params = hi.RunnerParams()

    # --- window: borderless (custom-chrome, see titlebar.py), never
    # always-on-top, never click-through -- still a completely ordinary,
    # resizable, taskbar-visible top-level window otherwise. ---
    runner_params.app_window_params.window_title = WINDOW_TITLE
    runner_params.app_window_params.window_geometry.size = (960, 600)
    runner_params.app_window_params.restore_previous_geometry = True
    runner_params.app_window_params.resizable = True
    runner_params.app_window_params.borderless = True
    # Dragging is hand-rolled in titlebar.py (a themed strip, geometrically
    # disjoint from its own min/max/close buttons -- see that module's
    # docstring for why that's more robust than relying on Hello ImGui's own
    # generic top-of-window drag zone here). Resizing is NOT hand-rolled --
    # Hello ImGui's own edge/corner resize zone doesn't overlap the title
    # bar strip at all, so it's used as-is. Closing gets its own themed
    # button too, instead of Hello ImGui's generic one.
    runner_params.app_window_params.borderless_movable = False
    runner_params.app_window_params.borderless_resizable = True
    runner_params.app_window_params.borderless_closable = False
    runner_params.app_window_params.top_most = False  # never always-on-top
    runner_params.app_window_params.hidden = False

    # --- rendering: OpenGL3, not DX11 -- see module docstring for why ---
    runner_params.renderer_backend_type = hi.RendererBackendType.open_gl3

    # --- ImGui window params: one full-viewport canvas, no docking, no
    # HelloImGui-drawn menu/status chrome -- shell.py draws its own sidebar
    # nav instead of relying on HelloImGui's default app menu. ---
    iwp = runner_params.imgui_window_params
    iwp.default_imgui_window_type = hi.DefaultImGuiWindowType.provide_full_screen_window
    iwp.show_menu_bar = False
    iwp.show_status_bar = False
    # Opaque clear color behind the canvas (this window is never
    # transparent/click-through -- that's only the future HUD overlay).
    # Re-set every frame from the active theme (below) rather than fixed
    # here, now that multiple themes ship -- otherwise switching away from
    # the default Dark theme would leave a mismatched-color sliver behind
    # the canvas on any frame where ImGui doesn't cover every pixel.
    iwp.background_color = theme_module.DARK.bg_base

    # --- icon font: imgui_bundle ships FontAwesome 4's ttf in its bundled
    # assets (fontawesome-webfont.ttf); FontAwesome 6 is NOT bundled, so
    # FA4 is used deliberately here rather than requesting an icon set whose
    # font file isn't actually on disk. See panels/*.py + shell.py for the
    # icons_fontawesome_4 glyphs in use. ---
    runner_params.callbacks.default_icon_font = hi.DefaultIconFont.font_awesome4

    def _show_gui() -> None:
        # Keep the canvas's clear color in lockstep with whatever theme is
        # active right now (see the `background_color` comment above).
        iwp.background_color = theme_module.get_theme(app_state.settings.theme_name).bg_base
        shell.render_frame(app_state)
        # Hand off the current crosshair state to the HUD overlay's render
        # thread once per Companion-window frame -- see hud_overlay.py's
        # module docstring for why this is a lock-guarded snapshot rather
        # than sharing app_state.overlay.crosshair across the thread
        # boundary directly. Cheap enough (a few float/str copies) to do
        # unconditionally every frame rather than only on change.
        hud_overlay.update_crosshair(app_state.overlay.crosshair)

    runner_params.callbacks.post_init = _post_init
    runner_params.callbacks.before_exit = _before_exit
    runner_params.callbacks.show_gui = _show_gui

    immapp.run(runner_params)


if __name__ == "__main__":
    main()
