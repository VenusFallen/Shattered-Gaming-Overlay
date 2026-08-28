"""main.py -- entry point for Shattered Gaming Overlay's Companion window.

Launches ONLY the Companion window (the normal settings/config UI:
Dashboard, Remapper, Macros, Profiles, Window Select, Overlay toggles,
Settings) -- an ordinary desktop window, resizable, taskbar-visible, not
always-on-top, not click-through. It is built on Dear ImGui via
`imgui_bundle`'s "Hello ImGui" (`immapp`/`hello_imgui`), which is the
project-wide UI toolkit.

Borderless, not chromeless: `app_window_params.borderless = True` below
removes the OS-drawn title bar (and its OS min/max/close buttons + drag
region) for a modern "companion app" look (Discord/Playnite-style), at 3/4
the previous default size (960x600 vs. 1280x800). shell.py replaces the lost
chrome with a themed custom title bar (see titlebar.py for the full
rationale, including which of Hello ImGui's own `borderless_movable` /
`_resizable` / `_closable` fields -- confirmed by introspecting
`hi.AppWindowParams` -- are used vs. hand-rolled).

Renderer backend note: DirectX11 was the original target for this window.
The `imgui-bundle` wheel actually installed from PyPI (confirmed by
introspecting `hello_imgui.RendererBackendType`/`PlatformBackendType` and
then trying to run with `direct_x11` -- see the resulting error message
below) is compiled with only `HELLOIMGUI_HAS_OPENGL3` + GLFW3;
DirectX11/DirectX12 are opt-in CMake flags on the C++ side that the
published cross-platform pip wheel does not enable, regardless of OS.
Requesting `direct_x11` here raises
`IM_ASSERT("DirectX11 backend is not available!")` at startup. This window
therefore runs on `RendererBackendType.open_gl3` instead, which IS compiled
in. This has no bearing on the project's anti-cheat safety properties --
the Companion window is an ordinary desktop window that never touches a
game process either way -- and it does not block the HUD overlay (see
hud_overlay.py), which needs a hand-rolled DirectComposition swap chain via
ctypes regardless of what Hello ImGui's backend selector supports. The
option if true DX11 parity is wanted later is building imgui_bundle from
source with `-DHELLOIMGUI_HAS_DIRECTX11=ON`.

This module also starts/stops the HUD overlay's own background thread
(hud_overlay.py) alongside the Companion window's lifecycle -- but the HUD
overlay itself is a separate, click-through, DirectComposition-backed,
non-injecting window, entirely distinct from the Companion window built
below. Nothing about the HUD
overlay is rendered as part of this window's own ImGui frame.

Also starts/stops the system tray icon (tray_icon.py). titlebar.py's Close
button hides the Companion window to tray instead of exiting; the tray
icon's own Quit menu item is what performs a real, full application exit
now (see tray_icon.py's module docstring). This is unrelated to, and never
touches, the HUD overlay -- hiding the Companion window to tray has no
effect on whatever the HUD overlay is currently showing.
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

# Background hardware-stats/FPS poller feeding the HUD overlay's Stats box
# (see stats_poller.py's module docstring -- start()/stop() are no-ops that
# publish a permanent available=False snapshot if LibreHardwareMonitor isn't
# available, so this is always safe to construct/start unconditionally).
# track_fps starts True; _show_gui() below flips it off at runtime whenever
# the Stats HUD's own "FPS" toggle is off, so PresentMon doesn't run for
# nothing while that metric isn't even shown.
stats_poller = StatsPoller(poll_interval_sec=1.0, track_fps=True)

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


def _post_init(app_state: AppState) -> None:
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
    # Stats poller: CPU/GPU/RAM/VRAM/FPS telemetry feeding the HUD overlay's
    # Stats box (see stats_poller.py). Runs for the whole app lifetime, same
    # as hud_overlay -- it's cheap to poll even while the Stats HUD toggle is
    # off (no HUD element gates whether this collects data, only whether the
    # HUD draws it; keeps the box responsive the instant the toggle flips on
    # rather than waiting a poll interval).
    stats_poller.start()
    # Independent OS-foreground-focus polling: its own dedicated background
    # thread, entirely decoupled from Hello ImGui's `show_gui` callback (which
    # does not fire while the Companion window is minimized). Must be running
    # before remapper_engine.start() below, since remapper.py's hook thread
    # reads window_select.cached_foreground_pid() live on every physical
    # event -- see window_select.py's "Independent focus polling" and
    # remapper.py's "Window-filter gating" docstring sections.
    window_select.start_focus_tracking()
    # System tray icon (tray_icon.py): its own hidden message window +
    # background thread, started here alongside the other lifetime-of-the-app
    # background pieces above. titlebar.py's Close button checks
    # tray_icon.tray_icon.is_running() to decide whether to hide-to-tray or
    # fall back to a real exit, so this must be up before the user can click
    # Close -- safe either way since _post_init finishes before the first
    # frame renders.
    tray_icon_module.tray_icon.start()
    # Remapper/Macro engine: hooks stay installed for the whole app lifetime
    # (it's the match/inject step that's gated on focus, not hook
    # installation). macro_engine.py
    # subscribes to remapper_engine's post-remap effective-event stream
    # instead of installing its own hook (see remapper.py's module
    # docstring) -- wire that subscription before either starts.
    remapper_engine.add_effective_listener(macro_engine.handle_effective_event)
    remapper_engine.start()
    macro_engine.start()
    # Automatic check-on-launch (see updater.py / panels/settings.py's
    # render_auto_update_prompt) -- fire-and-forget: start_check() spawns
    # its own short-lived daemon thread and returns immediately, so this
    # never blocks post_init/the first frame on the network call, same as
    # hud_overlay.start()/remapper_engine.start() above only ever kick off
    # background work rather than doing it inline. The result surfaces via
    # updater.update_manager.sync_to(), called every frame below, once the
    # background check actually completes.
    if app_state.settings.check_for_updates_on_launch:
        updater.update_manager.start_check(VERSION, is_automatic=True)


def _before_exit(app_state: AppState) -> None:
    # Belt-and-suspenders: panels/settings.py already writes settings.json
    # on every committed change (theme pick, toggle flip, slider/color-
    # picker release), but this catches anything that somehow didn't --
    # never skip a real save just because one should have already happened.
    settings_store.save(app_state)
    # Tear down the bind-capture hook (if it was ever started) rather than
    # leaving it installed until process teardown.
    capture_service.shutdown()
    # Same for the macro-recording hook (macro_recorder.py) -- separate
    # HookManager instance from capture_service's, see that module's
    # docstring for why.
    macro_recorder.shutdown()
    # Stop the HUD overlay's thread/window/DX11+DComp resources cleanly so
    # nothing outlives the Companion window's own process.
    hud_overlay.stop()
    # Stop the stats poller's background thread (LibreHardwareMonitor +
    # PresentMon) cleanly, same lifecycle scope as hud_overlay above.
    stats_poller.stop()
    # Stop the remap hook and let any in-flight macro playback threads wind
    # down cleanly (macro_engine.stop() signals + joins them).
    macro_engine.stop()
    remapper_engine.stop()
    # Stop the independent focus-polling thread started in _post_init above.
    window_select.stop_focus_tracking()
    # Remove the tray icon and stop its message-pump thread cleanly, whether
    # exit was triggered by the tray's own Quit item or any other path.
    tray_icon_module.tray_icon.stop()


def main() -> None:
    app_state = new_app_state()
    # Load settings.json if present (see settings_store.py's module
    # docstring) -- app-wide preferences, deliberately separate from and
    # loaded before profiles.json below: which profile is active must never
    # change what theme/close-behavior/etc. the user configured.
    settings_store.load(app_state)
    # Load profiles.json if present (see profiles.py's module docstring) --
    # populates app_state.profiles.profiles and, if a saved active profile
    # exists, applies it (with the persist_* safety pattern) before the
    # first frame renders. If no profiles.json exists yet, app_state keeps
    # the single empty, protected Default profile new_app_state() built.
    profiles_engine.load_all(app_state)

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
        # Keep window-focus tracking live regardless of which panel is
        # active -- panels/window_select.py's own refresh_if_stale() call
        # only runs while the Settings tab is open (it's nested inside that
        # panel's render), which would otherwise leave
        # `selected_has_focus` stale for remapper_engine/macro_engine's
        # window-filter gate while the user is on e.g. the Macros tab.
        window_select.refresh_if_stale(app_state.window_select)
        # Same lock-guarded-snapshot pattern as remapper_engine/macro_engine
        # below, opposite direction: copies the updater's background-thread
        # result INTO app_state.settings before this frame renders, so
        # panels/settings.py and the auto-update prompt both see it live.
        updater.update_manager.sync_to(app_state.settings)
        shell.render_frame(app_state)
        # Hand off the current crosshair state to the HUD overlay's render
        # thread once per Companion-window frame -- see hud_overlay.py's
        # module docstring for why this is a lock-guarded snapshot rather
        # than sharing app_state.overlay.crosshair across the thread
        # boundary directly. Cheap enough (a few float/str copies) to do
        # unconditionally every frame rather than only on change.
        hud_overlay.update_crosshair(app_state.overlay.crosshair)
        # Same idea for the Stats box: keep FPS/PresentMon tracking off
        # unless the Stats HUD is actually enabled AND showing FPS (see
        # stats_poller.py's set_track_fps()), then hand the latest combined
        # snapshot to the HUD overlay every frame.
        stats_poller.set_track_fps(app_state.overlay.stats_hud.enabled and app_state.overlay.stats_hud.show_fps)
        hud_overlay.update_stats(app_state.overlay.stats_hud, stats_poller.get_snapshot())
        # Module status badges: live counts of currently-*enabled* entries
        # (not total configured) -- see app_state.StatusIndicatorsState's
        # docstring for why "enabled" is the right count for a status
        # indicator.
        remap_enabled_count = sum(1 for e in app_state.remapper.entries if e.enabled)
        macro_enabled_count = sum(1 for m in app_state.macros.macros if m.enabled)
        hud_overlay.update_indicators(app_state.overlay.status_indicators, remap_enabled_count, macro_enabled_count)
        # Same lock-guarded-snapshot pattern for the Remapper/Macro engine's
        # own background threads -- see remapper.py/macro_engine.py's
        # update_snapshot() docstrings for why they must never read
        # app_state.remapper/app_state.macros/app_state.window_select
        # directly from a hook/playback thread.
        remapper_engine.update_snapshot(app_state.remapper, app_state.window_select)
        macro_engine.update_snapshot(app_state.macros)

    runner_params.callbacks.post_init = lambda: _post_init(app_state)
    runner_params.callbacks.before_exit = lambda: _before_exit(app_state)
    runner_params.callbacks.show_gui = _show_gui

    immapp.run(runner_params)


if __name__ == "__main__":
    main()
