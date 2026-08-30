"""panels/profiles.py -- Profiles panel: named, per-game configs with a
protected "Default" profile. Save/Load/Delete/Create all go through the
root-level profiles.py (real JSON-on-disk persistence) -- this panel
only renders app_state.ProfileDef list + the resulting state.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import profiles as profiles_engine
import widgets
from panel_context import PanelContext


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
                if imgui.is_item_hovered():
                    imgui.set_tooltip("Overwrite this profile with the CURRENT live Remapper/Macros/Window Select state.")

            imgui.same_line()
            if profile.protected:
                imgui.text(f"{profile.name}  ({fa.ICON_FA_LOCK} protected)")
            else:
                imgui.text(profile.name)

            if not profile.protected:
                imgui.same_line()
                imgui.set_cursor_pos_x(imgui.get_window_width() - 40)
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

            imgui.pop_id()
        imgui.spacing()

    if remove_id is not None:
        profiles_engine.delete_profile(ctx.state, remove_id)
