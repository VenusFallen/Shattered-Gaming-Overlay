"""main.py -- entry point for the Companion window (Dear ImGui via
imgui_bundle's Hello ImGui).

Borderless with a hand-rolled titlebar (see titlebar.py) instead of relying
on Hello ImGui's own borderless drag/resize/close fields. Renders on OpenGL3,
not DX11 -- the published imgui_bundle wheel only compiles in
HELLOIMGUI_HAS_OPENGL3; DX11 would need building imgui_bundle from source.
Doesn't affect the HUD overlay, which drives its own DirectComposition swap
chain directly via ctypes regardless of this window's backend.

Also owns the lifecycle of the HUD overlay's background thread
(hud_overlay.py, a separate click-through window, not rendered as part of
this window's own frame) and the system tray icon (tray_icon.py) -- Close
hides to tray; the tray's own Quit is the real exit.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from imgui_bundle import hello_imgui as hi
from imgui_bundle import imgui
from imgui_bundle import immapp

import profiles as profiles_engine
import settings_store
import shell
import theme as theme_module
import tray_icon as tray_icon_module
import updater
import window_select
from app_state import AppState, new_app_state
from hud_overlay import hud_overlay
from key_capture import capture_service
from macro_engine import macro_engine
from macro_recorder import macro_recorder
from remapper import remapper_engine
from stats_poller import StatsPoller
from version import VERSION, WINDOW_TITLE

# Safe to construct/start unconditionally -- degrades to available=False if
# LibreHardwareMonitor isn't present. _show_gui() below flips track_fps off
# at runtime whenever the Stats HUD's FPS toggle is off.
stats_poller = StatsPoller(poll_interval_sec=1.0, track_fps=True)

if not sys.platform.startswith("win"):
    raise SystemExit("Shattered Gaming Overlay's Companion window is Windows-only.")

_ICON_PATH = Path(__file__).resolve().parent / "assets" / "icon.ico"


def _set_window_icon() -> None:
    # Hello ImGui has no icon field or hwnd accessor -- set it the plain
    # Win32 way via a FindWindowW-by-title lookup.
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


def _post_init(app_state: AppState) -> None:
    # Real accessibility feature, not cosmetic -- arrow keys/Tab to move
    # focus, Enter/Space to activate.
    io = imgui.get_io()
    io.config_flags |= imgui.ConfigFlags_.nav_enable_keyboard
    _set_window_icon()
    # HUD overlay: starts idle, runs for the app's whole lifetime;
    # update_crosshair() in _show_gui() below turns elements on/off live.
    hud_overlay.start()
    # Cheap enough to poll continuously even while the Stats HUD is off, so
    # the box is already warm the instant the toggle flips on.
    stats_poller.start()
    # Own thread, decoupled from `show_gui` (which Hello ImGui skips while
    # minimized). Must start before remapper_engine below -- its hook thread
    # reads cached_foreground_pid() live on every input event.
    window_select.start_focus_tracking()
    # Must be up before the user can click Close -- titlebar.py checks
    # tray_icon.is_running() to decide hide-to-tray vs. real exit.
    tray_icon_module.tray_icon.start()
    # Hooks stay installed for the app's lifetime; only match/inject is
    # focus-gated. macro_engine subscribes to the post-remap event stream
    # instead of its own hook -- wire it before either starts.
    remapper_engine.add_effective_listener(macro_engine.handle_effective_event)
    remapper_engine.start()
    macro_engine.start()
    # Fire-and-forget -- start_check() spawns its own thread and returns
    # immediately; the result surfaces via sync_to() each frame.
    if app_state.settings.check_for_updates_on_launch:
        updater.update_manager.start_check(VERSION, is_automatic=True)


def _before_exit(app_state: AppState) -> None:
    # Belt-and-suspenders -- panels/settings.py already saves on every
    # change; this catches anything that somehow didn't.
    settings_store.save(app_state)
    capture_service.shutdown()
    macro_recorder.shutdown()  # separate HookManager from capture_service's
    hud_overlay.stop()
    stats_poller.stop()
    macro_engine.stop()  # joins in-flight playback threads
    remapper_engine.stop()
    window_select.stop_focus_tracking()
    tray_icon_module.tray_icon.stop()


def main() -> None:
    app_state = new_app_state()
    # Settings before profiles -- which profile is active must never change
    # app-wide preferences like theme or close-behavior.
    settings_store.load(app_state)
    profiles_engine.load_all(app_state)

    runner_params = hi.RunnerParams()

    runner_params.app_window_params.window_title = WINDOW_TITLE
    runner_params.app_window_params.window_geometry.size = (960, 600)
    runner_params.app_window_params.restore_previous_geometry = True
    runner_params.app_window_params.resizable = True
    runner_params.app_window_params.borderless = True
    # Drag is hand-rolled in titlebar.py; resize uses Hello ImGui's own
    # corner zone as-is (doesn't overlap the titlebar strip); close gets a
    # themed button instead of Hello ImGui's generic one.
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
    # Re-set from the active theme every frame in _show_gui() below --
    # this default only matters for the first frame.
    iwp.background_color = theme_module.DARK.bg_base

    # FA6 isn't bundled with imgui_bundle -- FA4 is what's actually on disk.
    runner_params.callbacks.default_icon_font = hi.DefaultIconFont.font_awesome4

    def _show_gui() -> None:
        iwp.background_color = theme_module.get_theme(app_state.settings.theme_name).bg_base
        # panels/window_select.py's own refresh_if_stale() only runs while
        # Settings is open -- call it here too so selected_has_focus stays
        # live for the remapper/macro gate regardless of active panel.
        window_select.refresh_if_stale(app_state.window_select)
        updater.update_manager.sync_to(app_state.settings)
        shell.render_frame(app_state)
        # Lock-guarded snapshot handoff to the HUD's own render thread.
        hud_overlay.update_crosshair(app_state.overlay.crosshair)
        stats_poller.set_track_fps(app_state.overlay.stats_hud.enabled and app_state.overlay.stats_hud.show_fps)
        stats_snapshot = stats_poller.get_snapshot()
        hud_overlay.update_stats(app_state.overlay.stats_hud, stats_snapshot)
        app_state.stats_fps_error = stats_snapshot.fps_error
        remap_enabled_count = sum(1 for e in app_state.remapper.entries if e.enabled)
        macro_enabled_count = sum(1 for m in app_state.macros.macros if m.enabled)
        hud_overlay.update_indicators(app_state.overlay.status_indicators, remap_enabled_count, macro_enabled_count)
        remapper_engine.update_snapshot(app_state.remapper, app_state.window_select)
        macro_engine.update_snapshot(app_state.macros)

    runner_params.callbacks.post_init = lambda: _post_init(app_state)
    runner_params.callbacks.before_exit = lambda: _before_exit(app_state)
    runner_params.callbacks.show_gui = _show_gui

    immapp.run(runner_params)


if __name__ == "__main__":
    main()
