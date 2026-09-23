"""panels/remapper.py -- Remapper panel: Standard Remapping (1:1 source ->
destination key/button pairs) and Auto Toggle/Hold (single-key entries that
change how that key's own presses land). Purely UI state; matching and
SendInput happen in the root-level remapper.py via update_snapshot().
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from app_state import AutoToggleHoldEntry, RemapEntry, RemapMode
from key_capture import KeyBind
from panel_context import PanelContext

_MODE_LABELS = [m.value for m in RemapMode]

_NAME_FIELD_WIDTH = 140.0
_SPACER = (20.0, 0.0)


def _spacer() -> None:
    imgui.same_line()
    imgui.dummy(imgui.ImVec2(*_SPACER))
    imgui.same_line()


def _handle_capture(ctx: PanelContext, entry: RemapEntry, field_name: str) -> None:
    # Draw the bind button for `entry.<field_name>` and manage capture start/poll/cancel for it.
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
        # Only one capture can be active across BOTH sections of this panel.
        ctx.capture.begin_capture()
        state.capturing_entry_id = entry.id
        state.capturing_field = field_name
        state.capturing_auto_id = None


def _handle_auto_capture(ctx: PanelContext, entry: AutoToggleHoldEntry) -> None:
    # Mirrors _handle_capture but tracks `capturing_auto_id` separately so it's never mistaken for a standard entry's capture.
    state = ctx.state.remapper
    is_target = state.capturing_auto_id == entry.id

    if is_target:
        result = ctx.capture.poll_result()
        if result is not None:
            entry.key = result
            state.capturing_auto_id = None
        elif imgui.is_key_pressed(imgui.Key.escape):
            ctx.capture.cancel_capture()
            state.capturing_auto_id = None

    clicked = widgets.bind_button(ctx.theme, f"{entry.id}-key", entry.key.name, is_target)
    if clicked and not is_target:
        ctx.capture.begin_capture()
        state.capturing_auto_id = entry.id
        state.capturing_entry_id = None
        state.capturing_field = None


def _render_standard_section(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.remapper

    widgets.section_title("Remapping")
    widgets.muted_text(theme, "Remap any key or mouse button to another. A remap also arms macros/toggles bound to its destination.")
    imgui.spacing()

    if imgui.button(f"{fa.ICON_FA_PLUS}  Add Remap"):
        state.add_entry()

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

            _spacer()
            imgui.set_next_item_width(_NAME_FIELD_WIDTH)
            _, entry.name = imgui.input_text("##name", entry.name)
            if imgui.is_item_hovered():
                imgui.set_tooltip("Optional label -- purely cosmetic.")

            _spacer()
            _handle_capture(ctx, entry, "source")
            imgui.same_line()
            imgui.text(fa.ICON_FA_ARROW_RIGHT)
            imgui.same_line()
            _handle_capture(ctx, entry, "destination")

            imgui.same_line()
            # Pinned to the card's right edge -- a mid-capture bind_button's label can otherwise push this off-card.
            imgui.set_cursor_pos_x(widgets.right_pinned_cursor_x())
            if imgui.button(f"{fa.ICON_FA_TRASH}##remove"):
                remove_id = entry.id

            if not entry.enabled:
                widgets.status_badge(theme, "neutral", "Disabled")

            imgui.pop_id()
        imgui.spacing()

    if remove_id is not None:
        state.remove_entry(remove_id)


def _render_auto_section(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.remapper

    widgets.section_title("Auto Toggle/Hold")
    widgets.muted_text(
        theme,
        "One key acting on itself. Toggle: first press latches it down, next press releases it.\n"
        "Hold: taps the key on both press and release, turning a toggle-only game action into hold-to-use.",
    )
    imgui.spacing()

    if imgui.button(f"{fa.ICON_FA_PLUS}  Add Auto Toggle/Hold"):
        state.add_auto_entry()

    imgui.spacing()

    if not state.auto_entries:
        widgets.muted_text(theme, "No Auto Toggle/Hold entries yet.")
        return

    remove_id = None
    for entry in state.auto_entries:
        with widgets.card(theme, f"auto-{entry.id}", size=(0, 0)):
            imgui.push_id(entry.id)

            changed, entry.enabled = widgets.labeled_toggle(
                theme, "Enabled", entry.enabled, ctx.state.settings.reduce_motion
            )

            _spacer()
            imgui.set_next_item_width(_NAME_FIELD_WIDTH)
            _, entry.name = imgui.input_text("##name", entry.name)
            if imgui.is_item_hovered():
                imgui.set_tooltip("Optional label -- purely cosmetic.")

            _spacer()
            _handle_auto_capture(ctx, entry)

            _spacer()
            mode_idx = list(RemapMode).index(entry.mode)
            imgui.set_next_item_width(110)
            changed, mode_idx = imgui.combo("##mode", mode_idx, _MODE_LABELS)
            if changed:
                entry.mode = list(RemapMode)[mode_idx]
            if imgui.is_item_hovered():
                imgui.set_tooltip(
                    "Toggle: first press latches the key down, next press releases it. Release does nothing.\n"
                    "Hold: press taps the key once, release taps it again -- each tap is a clean down/up."
                )

            imgui.same_line()
            # Same right-edge pin as the standard section's delete button above.
            imgui.set_cursor_pos_x(widgets.right_pinned_cursor_x())
            if imgui.button(f"{fa.ICON_FA_TRASH}##remove"):
                remove_id = entry.id

            if not entry.enabled:
                widgets.status_badge(theme, "neutral", "Disabled")

            imgui.pop_id()
        imgui.spacing()

    if remove_id is not None:
        state.remove_auto_entry(remove_id)


def render(ctx: PanelContext) -> None:
    imgui.text(f"{fa.ICON_FA_KEYBOARD}  Remapper")
    imgui.spacing()

    _render_standard_section(ctx)

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    _render_auto_section(ctx)
