"""panels/window_select.py -- Target Window: pick a specific running process,
or leave global. Purely UI rendering over app_state.WindowSelectState; the
actual psutil/win32 process enumeration and OS-focus tracking live in the
root-level window_select.py (the engine-side module -- same split as
input_hooks.py/input_inject.py vs their panel files), wired in below via a
single refresh_if_stale() call.

Folded into the Settings panel as its own card (`render_section`) rather than
a standalone top-level tab -- see shell.py's nav order.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
import window_select
from panel_context import PanelContext


def render_section(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.window_select
    window_select.refresh_if_stale(state)

    with widgets.card(theme, "settings-window-select", size=(0, 0)):
        widgets.section_title("Target Window")
        widgets.muted_text(
            theme,
            "Target a specific running game, or leave global. When a process is targeted, "
            "Remapper and Macros go inert the instant it loses focus and resume the instant "
            "it regains it -- the Overlay stays visible regardless.",
        )
        imgui.spacing()

        if state.selected is None:
            widgets.status_badge(theme, "info", "Global -- unrestricted, no process targeted")
        else:
            proc = state.selected
            widgets.status_badge(
                theme,
                "ok" if state.selected_has_focus else "neutral",
                f"Targeting: {proc.exe_name} (PID {proc.pid})",
            )
            widgets.muted_text(theme, f'Window title: "{proc.window_title}"')
            focus_label = "has OS focus" if state.selected_has_focus else "does not have OS focus (inactive)"
            widgets.muted_text(theme, f"Currently {focus_label}.")

        imgui.spacing()
        imgui.separator_text("Choose a process")

        imgui.set_next_item_width(280)
        changed, state.filter_text = imgui.input_text(f"{fa.ICON_FA_SEARCH} Filter", state.filter_text)
        imgui.same_line()
        if imgui.button(f"{fa.ICON_FA_SYNC}  Refresh"):
            window_select.force_refresh(state)
        if imgui.is_item_hovered():
            imgui.set_tooltip("The list auto-refreshes every couple seconds anyway -- use this to force it now.")

        imgui.spacing()

        clicked_global, _ = imgui.selectable(f"{fa.ICON_FA_GLOBE}  Global (no target)", state.selected is None)
        if clicked_global:
            state.selected = None
            state.selected_has_focus = False

        imgui.spacing()

        filter_lower = state.filter_text.strip().lower()
        visible = [
            p
            for p in state.available
            if not filter_lower or filter_lower in p.exe_name.lower() or filter_lower in p.window_title.lower()
        ]

        with widgets.card(theme, "settings-window-select-list", size=(0, 220)):
            if not state.available:
                widgets.muted_text(
                    theme,
                    "No windows found yet -- this refreshes automatically every couple "
                    "seconds, or click Refresh above.",
                )
            elif not visible:
                widgets.muted_text(theme, "No running processes match that filter.")
            else:
                for proc in visible:
                    is_selected = state.selected is not None and state.selected.pid == proc.pid
                    label = f"{proc.exe_name}  --  {proc.window_title}##{proc.pid}"
                    clicked, _ = imgui.selectable(label, is_selected)
                    if clicked:
                        state.selected = proc
                        state.selected_has_focus = False
