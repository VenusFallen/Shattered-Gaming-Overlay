"""Macros panel: named trigger -> step-sequence macros. Purely UI state (app_state.MacroDef/MacroStep); playback isn't executed here."""

from __future__ import annotations

from typing import List

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import input_inject
import widgets
from app_state import MacroMode, MacroStepKind
from macro_recorder import RecordedStep, macro_recorder
from panel_context import PanelContext

_MODE_LABELS = [m.value for m in MacroMode]
_STEP_KIND_LABELS = [k.value for k in MacroStepKind]
_MOUSE_BUTTONS = ["Left", "Right", "Middle", "X1", "X2"]
_MAX_DELAY_MS = 2_000_000_000  # drag_int needs a real v_max; this is a ceiling, not a meaningful limit


def _render_list(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.macros
    with widgets.card(theme, "macro-list", size=(240, 0)):
        widgets.section_title("Macros")
        if imgui.button(f"{fa.ICON_FA_PLUS}  New", imgui.ImVec2(-1, 0)):
            state.add_macro()
        imgui.spacing()
        for macro in state.macros:
            selected = macro.id == state.selected_id
            label = f"{macro.name}##{macro.id}"
            if not macro.enabled:
                label = f"{macro.name} ({fa.ICON_FA_PAUSE_CIRCLE})##{macro.id}"
            clicked, _ = imgui.selectable(label, selected)
            if clicked:
                state.selected_id = macro.id
        if not state.macros:
            widgets.muted_text(theme, "No macros yet.")


def _combined_trigger_label(macro) -> str:
    if not macro.trigger.is_bound:
        return "Unbound"
    return " + ".join(m.name for m in macro.trigger_modifiers) + (" + " if macro.trigger_modifiers else "") + macro.trigger.name


def _handle_trigger_capture(ctx: PanelContext, macro) -> None:
    """Single button, one gesture: press just one key for a plain trigger, or hold one and press a second
    before releasing either for a combo (e.g. Shift + R) -- see key_capture.py's chord capture."""
    state = ctx.state.macros
    is_target = state.capturing_macro_id == macro.id

    if is_target:
        result = ctx.capture.poll_chord_result()
        if result is not None:
            modifiers, trigger = result
            macro.trigger = trigger
            macro.trigger_modifiers = list(modifiers)
            state.capturing_macro_id = None
        elif imgui.is_key_pressed(imgui.Key.escape):
            ctx.capture.cancel_chord_capture()
            state.capturing_macro_id = None

    clicked = widgets.bind_button(ctx.theme, f"{macro.id}-trigger", _combined_trigger_label(macro), is_target)
    if clicked and not is_target:
        ctx.capture.begin_chord_capture()
        state.capturing_macro_id = macro.id
    if imgui.is_item_hovered() and not is_target:
        imgui.set_tooltip("Press one key for a simple trigger, or hold one and press a second for a combo like Shift + R.")


def _handle_step_key_capture(ctx: PanelContext, step) -> None:
    state = ctx.state.macros
    is_target = state.capturing_step_id == step.id

    if is_target:
        result = ctx.capture.poll_result()
        if result is not None:
            step.key = result
            state.capturing_step_id = None
        elif imgui.is_key_pressed(imgui.Key.escape):
            ctx.capture.cancel_capture()
            state.capturing_step_id = None

    clicked = widgets.bind_button(ctx.theme, f"{step.id}-key", step.key.name, is_target)
    if clicked and not is_target:
        ctx.capture.begin_capture()
        state.capturing_step_id = step.id


def _handle_move_to_capture(ctx: PanelContext, step) -> None:
    """Captures the cursor's current position on the next keypress, not a click -- clicking a capture button would move the cursor first."""
    state = ctx.state.macros
    is_target = state.capturing_move_step_id == step.id

    if is_target:
        result = ctx.capture.poll_result()
        if result is not None:
            try:
                step.move_x, step.move_y = input_inject.get_cursor_pos()
            except OSError:
                pass
            state.capturing_move_step_id = None
        elif imgui.is_key_pressed(imgui.Key.escape):
            ctx.capture.cancel_capture()
            state.capturing_move_step_id = None

    clicked = widgets.bind_button(ctx.theme, f"{step.id}-move", f"({step.move_x}, {step.move_y})", is_target)
    if clicked and not is_target:
        ctx.capture.begin_capture()
        state.capturing_move_step_id = step.id
    if imgui.is_item_hovered() and not is_target:
        imgui.set_tooltip("Hover your cursor over the target, then press any key to capture that spot.")


def _apply_recorded_steps(macro, recorded: List[RecordedStep]) -> None:
    """Appends via macro.add_step() so ID generation stays centralized in app_state.py."""
    for r in recorded:
        step = macro.add_step()
        step.kind = r.kind
        step.key = r.key
        step.mouse_button = r.mouse_button
        step.scroll_delta = r.scroll_delta
        step.delay_ms = r.delay_ms


def _handle_recording(ctx: PanelContext, macro) -> None:
    """Record starts a macro_recorder session; Stop converts it to real steps. Escape cancels/discards instead."""
    theme = ctx.theme
    state = ctx.state.macros
    is_recording_this = state.recording_macro_id == macro.id
    other_recording = state.recording_macro_id is not None and not is_recording_this

    if is_recording_this:
        if imgui.is_key_pressed(imgui.Key.escape):
            macro_recorder.cancel()
            state.recording_macro_id = None
            return
        # Trigger on mouse-down (is_item_activated), not the release-triggered
        # "clicked" return -- stops recording before the click's own mouse-up
        # can get buffered as a trailing step (see macro_recorder.py).
        imgui.button(f"{fa.ICON_FA_STOP_CIRCLE}  Stop")
        if imgui.is_item_activated():
            recorded = macro_recorder.stop()
            _apply_recorded_steps(macro, recorded)
            state.recording_macro_id = None
        else:
            imgui.same_line()
            elapsed = macro_recorder.elapsed_seconds()
            # "info", not "error"/"warn" -- an active recording isn't a problem.
            widgets.status_badge(theme, "info", f"Recording... {elapsed:0.1f}s  (Esc to cancel)")
    else:
        if other_recording:
            imgui.begin_disabled()
        if imgui.button(f"{fa.ICON_FA_DOT_CIRCLE}  Record"):
            macro_recorder.start()
            state.recording_macro_id = macro.id
        if other_recording:
            imgui.end_disabled()
        if other_recording and imgui.is_item_hovered():
            imgui.set_tooltip("Another macro is currently recording -- stop it first.")


def _draw_reorder_arrow(pointing_up: bool) -> None:
    """Draws a small filled triangle over the last-submitted item, sized to its actual rect -- the FontAwesome
    chevron glyphs render at the loaded icon font's fixed size, which overflows a half-height button."""
    draw_list = imgui.get_window_draw_list()
    r_min = imgui.get_item_rect_min()
    r_max = imgui.get_item_rect_max()
    cx = (r_min.x + r_max.x) * 0.5
    cy = (r_min.y + r_max.y) * 0.5
    half_w, half_h = 3.5, 2.0
    color = imgui.get_color_u32(imgui.Col_.text)
    if pointing_up:
        p1, p2, p3 = imgui.ImVec2(cx - half_w, cy + half_h), imgui.ImVec2(cx + half_w, cy + half_h), imgui.ImVec2(cx, cy - half_h)
    else:
        p1, p2, p3 = imgui.ImVec2(cx - half_w, cy - half_h), imgui.ImVec2(cx + half_w, cy - half_h), imgui.ImVec2(cx, cy + half_h)
    draw_list.add_triangle_filled(p1, p2, p3, color)


def _move_step_up(macro, step_id: str) -> None:
    idx = next((i for i, s in enumerate(macro.steps) if s.id == step_id), None)
    if idx is not None and idx > 0:
        macro.steps[idx - 1], macro.steps[idx] = macro.steps[idx], macro.steps[idx - 1]


def _move_step_down(macro, step_id: str) -> None:
    idx = next((i for i, s in enumerate(macro.steps) if s.id == step_id), None)
    if idx is not None and idx < len(macro.steps) - 1:
        macro.steps[idx + 1], macro.steps[idx] = macro.steps[idx], macro.steps[idx + 1]


def _render_steps(ctx: PanelContext, macro) -> None:
    theme = ctx.theme
    widgets.section_title("Steps")
    if imgui.button(f"{fa.ICON_FA_PLUS}  Add Step"):
        macro.add_step()
    imgui.same_line()
    _handle_recording(ctx, macro)

    remove_id = None
    move_up_id = None
    move_down_id = None
    for i, step in enumerate(macro.steps):
        imgui.push_id(step.id)
        with widgets.card(theme, f"step-{step.id}", size=(0, 0)):
            # Reorder buttons live at the row's own start, ahead of everything else -- the row's variable-width
            # kind-specific content and the right-pinned delete button already crowd the rest of the row, so
            # this is the one spot that never has to compete for space with either. Stacked (not side by side)
            # and half-height each, so the pair costs one button's worth of horizontal space, not two.
            reorder_gap = 3.0
            reorder_btn_size = imgui.ImVec2(24.0, imgui.get_frame_height() * 0.5)
            # The stack is exactly one gap taller than a normal control (two half-height buttons + the gap
            # between them) -- starting it half a gap earlier centers it against the combo, rather than moving
            # the combo (and everything after it in the row) off the row's normal, shared baseline.
            row_x = imgui.get_cursor_pos_x()
            row_top_y = imgui.get_cursor_pos_y()
            imgui.set_cursor_pos_y(row_top_y - reorder_gap / 2.0)
            imgui.push_style_var(imgui.StyleVar_.item_spacing, imgui.ImVec2(imgui.get_style().item_spacing.x, reorder_gap))
            if i == 0:
                imgui.begin_disabled()
            if imgui.button("##moveup", reorder_btn_size):
                move_up_id = step.id
            _draw_reorder_arrow(pointing_up=True)
            if i == 0:
                imgui.end_disabled()
            if i == len(macro.steps) - 1:
                imgui.begin_disabled()
            if imgui.button("##movedown", reorder_btn_size):
                move_down_id = step.id
            _draw_reorder_arrow(pointing_up=False)
            if i == len(macro.steps) - 1:
                imgui.end_disabled()
            imgui.pop_style_var()

            # Positioned explicitly, not via same_line() + set_cursor_pos_y(): ImGui measures the "current line" for
            # everything chained after this from where same_line() put the cursor, ignoring a later Y override --
            # which left the bind/delay/delete widgets riding the arrow stack's lower line, not the combo's.
            imgui.set_cursor_pos(imgui.ImVec2(row_x + reorder_btn_size.x + imgui.get_style().item_spacing.x, row_top_y))

            kind_idx = list(MacroStepKind).index(step.kind)
            imgui.set_next_item_width(160)
            changed, kind_idx = imgui.combo("##kind", kind_idx, _STEP_KIND_LABELS)
            if changed:
                step.kind = list(MacroStepKind)[kind_idx]

            imgui.same_line()
            if step.kind in (MacroStepKind.KEY_DOWN, MacroStepKind.KEY_UP, MacroStepKind.KEY_TAP):
                _handle_step_key_capture(ctx, step)
            elif step.kind in (MacroStepKind.MOUSE_DOWN, MacroStepKind.MOUSE_UP, MacroStepKind.MOUSE_CLICK):
                btn_idx = _MOUSE_BUTTONS.index(step.mouse_button) if step.mouse_button in _MOUSE_BUTTONS else 0
                imgui.set_next_item_width(120)
                changed, btn_idx = imgui.combo("##mousebtn", btn_idx, _MOUSE_BUTTONS)
                if changed:
                    step.mouse_button = _MOUSE_BUTTONS[btn_idx]
            elif step.kind == MacroStepKind.SCROLL:
                imgui.set_next_item_width(120)
                changed, step.scroll_delta = imgui.drag_int("##scroll", step.scroll_delta, 10.0, -1200, 1200, "%d")
            elif step.kind == MacroStepKind.DELAY:
                # "ms" is a separate label, not baked into the format string, so it doesn't interfere with typing a replacement value.
                imgui.set_next_item_width(90)
                changed, step.delay_ms = imgui.drag_int("##delay", step.delay_ms, 1.0, 0, _MAX_DELAY_MS, "%d")
                imgui.same_line()
                widgets.muted_text(theme, "ms")
            elif step.kind == MacroStepKind.MOUSE_MOVE_TO:
                # Numeric fields double as a manual override -- capture gets you close, dragging gets you exact.
                imgui.set_next_item_width(70)
                _, step.move_x = imgui.drag_int("##movetox", step.move_x, 1.0, 0, 10_000, "x %d")
                imgui.same_line()
                imgui.set_next_item_width(70)
                _, step.move_y = imgui.drag_int("##movetoy", step.move_y, 1.0, 0, 10_000, "y %d")
                imgui.same_line()
                _handle_move_to_capture(ctx, step)

                imgui.set_next_item_width(160)
                _, step.move_speed_pct = imgui.slider_int("Speed##movetospeed", step.move_speed_pct, 0, 100, "%d%%")
                if imgui.is_item_hovered():
                    imgui.set_tooltip(
                        "How fast the cursor glides to the target -- it always moves in small steps timed at "
                        "the polling rate set in Settings, with a natural accelerate/decelerate curve, never "
                        "an instant jump."
                    )
            elif step.kind == MacroStepKind.MOUSE_MOVE_BY:
                # Split across narrow lines -- these cards have no horizontal scrollbar, a wide single line clipped the delete button off-screen.
                imgui.set_next_item_width(70)
                _, step.move_x = imgui.drag_int("##movebydx", step.move_x, 1.0, -2000, 2000, "dx %d")
                if imgui.is_item_hovered():
                    imgui.set_tooltip("Positive moves right, negative moves left.")
                imgui.same_line()
                imgui.set_next_item_width(70)
                # dy is "positive = up" -- opposite of SendInput's own raw convention, see input_inject.send_mouse_move_by_step.
                _, step.move_y = imgui.drag_int("##movebydy", step.move_y, 1.0, -2000, 2000, "dy %d")
                if imgui.is_item_hovered():
                    imgui.set_tooltip("Positive moves up, negative moves down.")

                # Independent of Humanize jitter above (distance) -- this only varies the route.
                imgui.set_next_item_width(160)
                _, step.path_wobble_pct = imgui.slider_int("Path wobble##movebywobble", step.path_wobble_pct, 0, 100, "%d%%")
                if imgui.is_item_hovered():
                    imgui.set_tooltip(
                        "How much the path to (dx, dy) wobbles side to side on the way there -- independent of "
                        "Humanize jitter, which only varies the total distance. 0 is a perfectly straight line."
                    )

            imgui.same_line()
            # Pinned to the card's right edge -- preceding widget width varies by step kind, so plain same_line() could clip this off-screen.
            imgui.set_cursor_pos_x(widgets.right_pinned_cursor_x())
            if imgui.button(f"{fa.ICON_FA_TRASH}##removestep"):
                remove_id = step.id
        imgui.pop_id()

    if move_up_id is not None:
        _move_step_up(macro, move_up_id)
    if move_down_id is not None:
        _move_step_down(macro, move_down_id)
    if remove_id is not None:
        macro.steps = [s for s in macro.steps if s.id != remove_id]


def _render_editor(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.macros
    macro = state.find(state.selected_id)

    with widgets.card(theme, "macro-editor", size=(0, 0)):
        if macro is None:
            widgets.muted_text(theme, "Select or create a macro on the left.")
            return

        imgui.set_next_item_width(280)
        changed, macro.name = imgui.input_text("Name", macro.name)
        imgui.same_line()
        imgui.set_cursor_pos_x(widgets.right_pinned_cursor_x())
        delete_clicked = imgui.button(f"{fa.ICON_FA_TRASH}##removemacro")
        if imgui.is_item_hovered():
            imgui.set_tooltip("Delete this macro")

        _, macro.enabled = widgets.labeled_toggle(theme, "Enabled", macro.enabled, ctx.state.settings.reduce_motion)

        imgui.text("Trigger")
        imgui.same_line()
        _handle_trigger_capture(ctx, macro)

        mode_idx = list(MacroMode).index(macro.mode)
        imgui.set_next_item_width(160)
        changed, mode_idx = imgui.combo("Mode", mode_idx, _MODE_LABELS)
        if changed:
            macro.mode = list(MacroMode)[mode_idx]
        if imgui.is_item_hovered():
            imgui.set_tooltip("Once: single playback per trigger.\nHold: repeats while trigger is held.\nToggle: trigger arms/disarms looped playback.")

        imgui.set_next_item_width(200)
        changed, macro.humanize_jitter_pct = imgui.slider_int(
            "Humanize jitter", macro.humanize_jitter_pct, 0, 50, "%d%%"
        )
        if imgui.is_item_hovered():
            imgui.set_tooltip("Adds random timing variance to playback so it doesn't look inhumanly regular.")

        imgui.spacing()
        _render_steps(ctx, macro)

    if delete_clicked:
        # remove_macro() clears recording_macro_id but not the recorder itself.
        if state.recording_macro_id == macro.id:
            macro_recorder.cancel()
        state.remove_macro(macro.id)


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    imgui.text(f"{fa.ICON_FA_LIST_OL}  Macros")
    widgets.muted_text(theme, "Record/playback sequences with Once, Hold, or Toggle triggers.")
    imgui.spacing()

    imgui.begin_group()
    _render_list(ctx)
    imgui.end_group()
    imgui.same_line()
    imgui.begin_group()
    _render_editor(ctx)
    imgui.end_group()
