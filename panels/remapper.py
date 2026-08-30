"""panels/remapper.py -- Remapper panel: source key/button -> destination
key/button pairs. Purely UI state (app_state.RemapEntry list); no matching
or SendInput happens here -- that's the root-level remapper.py, which reads
this state each frame via update_snapshot().
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from app_state import RemapEntry
from key_capture import KeyBind
from panel_context import PanelContext


def _handle_capture(ctx: PanelContext, entry: RemapEntry, field_name: str) -> None:
    """Draw the bind button for `entry.<field_name>` and manage capture
    start/poll/cancel for it."""
    state = ctx.state.remapper
    is_target = state.capturing_entry_id == entry.id and state.capturing_field == field_name
    current: KeyBind = getattr(entry, field_name)

    if is_target:
        result = ctx.capture.poll_result()
        if result is not None:
            setattr(entry, field_name, result)
            state.capturing_entry_id = None
            state.capturing_field = None
        elif imgui.is_key_pressed(imgui.Key.escape):
            ctx.capture.cancel_capture()
            state.capturing_entry_id = None
            state.capturing_field = None

    clicked = widgets.bind_button(ctx.theme, f"{entry.id}-{field_name}", current.name, is_target)
    if clicked and not is_target:
        # Starting a new capture always wins -- only one can be active.
        ctx.capture.begin_capture()
        state.capturing_entry_id = entry.id
        state.capturing_field = field_name


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.remapper

    imgui.text(f"{fa.ICON_FA_KEYBOARD}  Remapper")
    widgets.muted_text(theme, "Remap any key or mouse button to another. A remap also arms macros/toggles bound to its destination.")
    imgui.spacing()

    if imgui.button(f"{fa.ICON_FA_PLUS}  Add Remap"):
        state.add_entry()

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    if not state.entries:
        widgets.muted_text(theme, "No remaps yet. Click \"Add Remap\" to create one.")
        return

    remove_id = None
    for entry in state.entries:
        with widgets.card(theme, f"remap-{entry.id}", size=(0, 0)):
            imgui.push_id(entry.id)

            changed, entry.enabled = widgets.labeled_toggle(
                theme, "Enabled", entry.enabled, ctx.state.settings.reduce_motion
            )

            imgui.same_line()
            imgui.dummy(imgui.ImVec2(20, 0))
            imgui.same_line()

            _handle_capture(ctx, entry, "source")
            imgui.same_line()
            imgui.text(fa.ICON_FA_ARROW_RIGHT)
            imgui.same_line()
            _handle_capture(ctx, entry, "destination")

            imgui.same_line()
            imgui.dummy(imgui.ImVec2(20, 0))
            imgui.same_line()
            if imgui.button(f"{fa.ICON_FA_TRASH}##remove"):
                remove_id = entry.id

            if not entry.enabled:
                widgets.status_badge(theme, "neutral", "Disabled")

            imgui.pop_id()
        imgui.spacing()

    if remove_id is not None:
        state.remove_entry(remove_id)
