"""panels/soundboard.py -- Soundboard panel: import an audio file, bind it
to a hotkey, play it on trigger. Purely UI state; playback and device I/O
happen in soundboard_engine.py via update_snapshot()/handle_effective_event().
The engine singleton is called directly only for the Preview button.
"""

from __future__ import annotations

import os

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import audio_devices
import file_dialog
import soundboard_engine
import soundboard_store
import widgets
from panel_context import PanelContext

_LIST_WIDTH = 240.0


def _save(ctx: PanelContext) -> None:
    # Persist immediately on any edit, same convention as profiles.py -- not a periodic autosave.
    soundboard_store.save(ctx.state)


def _render_output_device_picker(ctx: PanelContext) -> None:
    # Searchable dropdown, not a plain combo -- a dozen+ WASAPI devices show up with VoiceMeeter/GoXLR installed.
    theme = ctx.theme
    state = ctx.state.soundboard
    devices = audio_devices.list_output_devices()

    preview = state.output_device_name or "(system default)"
    imgui.set_next_item_width(320)
    opened = imgui.begin_combo("##soundboard-output", preview)
    if imgui.is_item_activated():
        state.device_filter_text = ""  # reset so a stale search doesn't leak into a fresh open
    if not opened:
        return

    widgets.muted_text(theme, f"Currently: {state.output_device_name or 'system default'}")
    imgui.separator()
    imgui.set_next_item_width(-1)
    _, state.device_filter_text = imgui.input_text(f"{fa.ICON_FA_SEARCH} Filter##soundboard-output-filter", state.device_filter_text)
    imgui.separator()

    clicked_default, _ = imgui.selectable("(system default)", state.output_device_name == "")
    if clicked_default:
        state.output_device_name = ""
        _save(ctx)
        imgui.close_current_popup()

    filter_lower = state.device_filter_text.strip().lower()
    for device in devices:
        if filter_lower and filter_lower not in device.name.lower():
            continue
        is_selected = state.output_device_name == device.name
        clicked, _ = imgui.selectable(device.name, is_selected)
        if clicked:
            state.output_device_name = device.name
            _save(ctx)
            imgui.close_current_popup()

    imgui.end_combo()


def _handle_clip_hotkey_capture(ctx: PanelContext, clip) -> None:
    state = ctx.state.soundboard
    is_target = state.capturing_clip_id == clip.id

    if is_target:
        result = ctx.capture.poll_result()
        if result is not None:
            clip.hotkey = result
            state.capturing_clip_id = None
            _save(ctx)
        elif imgui.is_key_pressed(imgui.Key.escape):
            ctx.capture.cancel_capture()
            state.capturing_clip_id = None

    clicked = widgets.bind_button(ctx.theme, f"{clip.id}-hotkey", clip.hotkey.name, is_target)
    if clicked and not is_target:
        ctx.capture.begin_capture()
        state.capturing_clip_id = clip.id


def _render_clip_list(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.soundboard
    with widgets.card(theme, "soundboard-list", size=(_LIST_WIDTH, 0)):
        widgets.section_title("Soundboard")
        if imgui.button(f"{fa.ICON_FA_PLUS}  Add Sound", imgui.ImVec2(-1, 0)):
            path = file_dialog.show_open_dialog(title="Add Sound", filter_spec=file_dialog.AUDIO_FILTER, def_ext="")
            if path:
                clip = state.add_clip()
                clip.file_path = path
                clip.name = os.path.splitext(os.path.basename(path))[0] or "New Sound"
                _save(ctx)
        imgui.spacing()

        for clip in state.clips:
            selected = clip.id == state.selected_clip_id
            label = f"{clip.name}##{clip.id}"
            if soundboard_engine.clip_is_missing(clip.file_path):
                # Icon + color, never color alone -- same convention as widgets.status_badge.
                imgui.text_colored(theme.danger, fa.ICON_FA_EXCLAMATION_TRIANGLE)
                imgui.same_line()
            clicked, _ = imgui.selectable(label, selected)
            if clicked:
                state.selected_clip_id = clip.id

        if not state.clips:
            widgets.muted_text(theme, "No sounds yet.")


def _render_clip_editor(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.soundboard
    clip = state.find(state.selected_clip_id)

    with widgets.card(theme, "soundboard-editor", size=(0, 0)):
        if clip is None:
            widgets.muted_text(theme, "Select or add a sound on the left.")
            return

        imgui.push_id(clip.id)

        imgui.set_next_item_width(280)
        _, clip.name = imgui.input_text("Name", clip.name)
        if imgui.is_item_deactivated_after_edit():
            _save(ctx)
        imgui.same_line()
        imgui.set_cursor_pos_x(widgets.right_pinned_cursor_x())
        if imgui.button(f"{fa.ICON_FA_TRASH}##removeclip"):
            remove_id = clip.id
        else:
            remove_id = None
        if imgui.is_item_hovered():
            imgui.set_tooltip("Delete this sound")

        changed_enabled, clip.enabled = widgets.labeled_toggle(theme, "Enabled", clip.enabled, ctx.state.settings.reduce_motion)
        if changed_enabled:
            _save(ctx)

        imgui.text("Hotkey")
        imgui.same_line()
        _handle_clip_hotkey_capture(ctx, clip)

        imgui.same_line()
        if imgui.button(f"{fa.ICON_FA_PLAY}  Preview"):
            soundboard_engine.soundboard_engine.preview(clip)
        if imgui.is_item_hovered():
            imgui.set_tooltip("Play this sound now, without needing its hotkey.")

        imgui.set_next_item_width(200)
        _, clip.volume = imgui.slider_float("Volume", clip.volume, 0.0, 1.0, "%.2f")
        if imgui.is_item_deactivated_after_edit():
            _save(ctx)

        imgui.spacing()
        if soundboard_engine.clip_is_missing(clip.file_path):
            widgets.status_badge(theme, "error", f"{fa.ICON_FA_EXCLAMATION_TRIANGLE}  File not found")
            widgets.muted_text(theme, clip.file_path or "(no file set)")
        else:
            widgets.muted_text(theme, clip.file_path)

        imgui.pop_id()

    if remove_id is not None:
        state.remove_clip(remove_id)
        _save(ctx)


def render(ctx: PanelContext) -> None:
    imgui.text(f"{fa.ICON_FA_MUSIC}  Soundboard")
    widgets.muted_text(ctx.theme, "Play imported sounds on a hotkey.")
    imgui.spacing()

    widgets.muted_text(ctx.theme, "Output device:")
    imgui.same_line()
    _render_output_device_picker(ctx)
    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    imgui.begin_group()
    _render_clip_list(ctx)
    imgui.end_group()
    imgui.same_line()
    imgui.begin_group()
    _render_clip_editor(ctx)
    imgui.end_group()
