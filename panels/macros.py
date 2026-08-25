"""panels/macros.py -- Macros panel: named trigger -> step-sequence macros
with Once/Hold/Toggle modes and a humanize-jitter knob. Purely UI state
(app_state.MacroDef/MacroStep); no recording/playback happens here -- that's
engine-agent's future macro_engine.py.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from app_state import MacroMode, MacroStepKind
from panel_context import PanelContext

_MODE_LABELS = [m.value for m in MacroMode]
_STEP_KIND_LABELS = [k.value for k in MacroStepKind]
_MOUSE_BUTTONS = ["Left", "Right", "Middle", "X1", "X2"]


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


def _handle_trigger_capture(ctx: PanelContext, macro) -> None:
    state = ctx.state.macros
    is_target = state.capturing_macro_id == macro.id

    if is_target:
        result = ctx.capture.poll_result()
        if result is not None:
            macro.trigger = result
            state.capturing_macro_id = None
        elif imgui.is_key_pressed(imgui.Key.escape):
            ctx.capture.cancel_capture()
            state.capturing_macro_id = None

    clicked = widgets.bind_button(ctx.theme, f"{macro.id}-trigger", macro.trigger.name, is_target)
    if clicked and not is_target:
        ctx.capture.begin_capture()
        state.capturing_macro_id = macro.id


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


def _render_steps(ctx: PanelContext, macro) -> None:
    theme = ctx.theme
    widgets.section_title("Steps")
    if imgui.button(f"{fa.ICON_FA_PLUS}  Add Step"):
        macro.add_step()

    remove_id = None
    for i, step in enumerate(macro.steps):
        imgui.push_id(step.id)
        with widgets.card(theme, f"step-{step.id}", size=(0, 0)):
            imgui.text(f"{i + 1}.")
            imgui.same_line()

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
                imgui.set_next_item_width(140)
                changed, step.delay_ms = imgui.drag_int("##delay", step.delay_ms, 1.0, 0, 5000, "%d ms")

            imgui.same_line()
            if imgui.button(f"{fa.ICON_FA_TRASH}##removestep"):
                remove_id = step.id
        imgui.pop_id()

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
