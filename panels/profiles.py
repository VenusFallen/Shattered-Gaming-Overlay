"""panels/profiles.py -- Profiles panel: named, per-game configs with a
protected "Default" profile. Save/Load/Delete/Create all go through the
root-level profiles.py (real JSON-on-disk persistence) -- this panel
only renders app_state.ProfileDef list + the resulting state.
"""

from __future__ import annotations

import time

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import file_dialog
import profiles as profiles_engine
import widgets
import window_select
from panel_context import PanelContext

# How long the "Saved!" indicator stays next to a profile's name after
# clicking Save -- a brief acknowledgment, not a lingering status.
_SAVE_FLASH_SEC = 1.5


def _render_target_executable_picker(ctx: PanelContext, profile) -> None:
    # Dropdown over the same WindowSelectState.available list Settings' Target Window section uses.
    # Refreshes on is_item_activated() (the exact open click), not every frame the popup stays open.
    theme = ctx.theme
    state = ctx.state.profiles

    preview = profile.target_executable or "(none -- auto-switch off)"
    imgui.set_next_item_width(220)
    opened = imgui.begin_combo("##targetexe", preview)
    if imgui.is_item_activated():
        window_select.force_refresh(ctx.state.window_select)
        # `auto_switch_filter_text` is shared, not per-profile -- clear it so a stale filter doesn't leak into the next picker.
        state.auto_switch_filter_text = ""
    if imgui.is_item_hovered():
        imgui.set_tooltip(
            "Auto-loads this profile when that process gains focus. Also requires the global toggle in Settings."
        )
    if not opened:
        return

    widgets.muted_text(theme, f"Currently: {profile.target_executable or 'none'}")
    imgui.separator()

    imgui.set_next_item_width(-1)
    _, state.auto_switch_filter_text = imgui.input_text(
        f"{fa.ICON_FA_SEARCH} Filter##targetexefilter", state.auto_switch_filter_text
    )
    imgui.separator()

    clicked_none, _ = imgui.selectable("(none -- auto-switch off)", profile.target_executable == "")
    if clicked_none:
        profile.target_executable = ""
        profiles_engine.sync_metadata(ctx.state)
        imgui.close_current_popup()

    filter_lower = state.auto_switch_filter_text.strip().lower()
    available = ctx.state.window_select.available
    if not available:
        widgets.muted_text(theme, "No running processes found yet.")
    else:
        seen = set()
        for proc in available:
            exe_lower = proc.exe_name.lower()
            if exe_lower in seen:
                continue  # one entry per exe name -- multiple windows of the same game collapse together
            seen.add(exe_lower)
            if filter_lower and filter_lower not in exe_lower and filter_lower not in proc.window_title.lower():
                continue
            is_selected = profile.target_executable.lower() == exe_lower
            clicked, _ = imgui.selectable(f"{proc.exe_name}  --  {proc.window_title}", is_selected)
            if clicked:
                profile.target_executable = proc.exe_name
                profiles_engine.sync_metadata(ctx.state)
                imgui.close_current_popup()

    imgui.end_combo()


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.profiles

    imgui.text(f"{fa.ICON_FA_FOLDER_OPEN}  Profiles")
    widgets.muted_text(
        theme,
        "Save/load named per-game configs -- Remapper, Macros, Window Select, and the Overlay "
        "(crosshair, Stats HUD, status indicators). Loading a profile always starts with "
        "Remapper/Macros/Window Select disabled, except whatever you mark below to survive the "
        "load -- the Overlay always comes along as saved, since it's just a visual aid.",
    )
    imgui.spacing()

    imgui.set_next_item_width(240)
    changed, state.new_profile_draft = imgui.input_text("##newprofile", state.new_profile_draft)
    imgui.same_line()
    can_create = bool(state.new_profile_draft.strip())
    if not can_create:
        imgui.begin_disabled()
    if imgui.button(f"{fa.ICON_FA_PLUS}  Create Profile"):
        # Snapshots the current live state into a new profile and activates
        # it -- a "Save As" flow.
        if profiles_engine.create_profile_from_current(ctx.state, state.new_profile_draft.strip()) is not None:
            state.new_profile_draft = ""
    if not can_create:
        imgui.end_disabled()
    if imgui.is_item_hovered():
        imgui.set_tooltip("Saves the CURRENT Remapper/Macros/Window Select state under a new profile name.")

    imgui.same_line()
    if imgui.button(f"{fa.ICON_FA_FILE_IMPORT}  Import Profile"):
        # Native Open dialog is blocking -- fine, only fires on this click.
        path = file_dialog.show_open_dialog()
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_text = f.read()
            except OSError:
                raw_text = None
            if raw_text is not None:
                profiles_engine.import_profile(ctx.state, raw_text)
                # Malformed/unreadable file -> returns None and fails silent-but-safe, same as this panel's other no-ops.
    if imgui.is_item_hovered():
        imgui.set_tooltip("Add a profile someone else exported to you as a new profile in your own list.")

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    remove_id = None
    for profile in state.profiles:
        with widgets.card(theme, f"profile-{profile.id}", size=(0, 0)):
            imgui.push_id(profile.id)

            is_active = profile.id == state.active_id
            if is_active:
                widgets.status_badge(theme, "ok", "Active")
            else:
                if imgui.button(f"{fa.ICON_FA_CHECK}  Load"):
                    profiles_engine.apply_profile(ctx.state, profile.id)

            if not profile.protected:
                imgui.same_line()
                if imgui.button(f"{fa.ICON_FA_SAVE}  Save"):
                    profiles_engine.save_profile(ctx.state, profile.id)
                    state.save_flash_id = profile.id
                    state.save_flash_until = time.monotonic() + _SAVE_FLASH_SEC
                if imgui.is_item_hovered():
                    imgui.set_tooltip("Overwrite this profile with the CURRENT live Remapper/Macros/Window Select state.")

            imgui.same_line()
            if imgui.button(f"{fa.ICON_FA_FILE_EXPORT}  Export"):
                default_name = profiles_engine.suggest_export_filename(ctx.state, profile.id)
                path = file_dialog.show_save_dialog(default_filename=default_name)
                if path:
                    profiles_engine.export_profile_to_file(ctx.state, profile.id, path)
            if imgui.is_item_hovered():
                imgui.set_tooltip("Save this profile to a standalone file you can share with someone else.")

            imgui.same_line()
            if profile.protected:
                imgui.text(f"{profile.name}  ({fa.ICON_FA_LOCK} protected)")
            else:
                imgui.text(profile.name)

            if state.save_flash_id == profile.id and time.monotonic() < state.save_flash_until:
                imgui.same_line()
                widgets.status_badge(theme, "ok", "Saved!")

            if not profile.protected:
                imgui.same_line()
                # right_pinned_cursor_x(), not a fixed offset -- a long name or the Saved! badge can run wider than usual.
                imgui.set_cursor_pos_x(widgets.right_pinned_cursor_x())
                if imgui.button(f"{fa.ICON_FA_TRASH}##removeprofile"):
                    remove_id = profile.id

            imgui.spacing()
            widgets.muted_text(theme, "Survive profile load:")
            imgui.same_line()
            changed_r, profile.persist_remapper = widgets.labeled_toggle(
                theme, "Remapper", profile.persist_remapper, ctx.state.settings.reduce_motion
            )
            imgui.same_line()
            imgui.dummy(imgui.ImVec2(12, 0))
            imgui.same_line()
            changed_m, profile.persist_macros = widgets.labeled_toggle(
                theme, "Macros", profile.persist_macros, ctx.state.settings.reduce_motion
            )
            imgui.same_line()
            imgui.dummy(imgui.ImVec2(12, 0))
            imgui.same_line()
            changed_w, profile.persist_window_select = widgets.labeled_toggle(
                theme, "Window Select", profile.persist_window_select, ctx.state.settings.reduce_motion
            )
            if changed_r or changed_m or changed_w:
                # Persist the flag change immediately (metadata only) --
                # otherwise it's lost if the app closes before the next
                # Save/Load/Create/Delete.
                profiles_engine.sync_metadata(ctx.state)

            imgui.spacing()
            widgets.muted_text(theme, "Auto-switch target:")
            imgui.same_line()
            _render_target_executable_picker(ctx, profile)

            imgui.pop_id()
        imgui.spacing()

    if remove_id is not None:
        profiles_engine.delete_profile(ctx.state, remove_id)
