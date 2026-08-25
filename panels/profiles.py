"""panels/profiles.py -- Profiles panel: named, per-game configs with a
protected "Default" profile. Purely UI state (app_state.ProfileDef list); no
save/load-to-disk happens here -- that's engine-agent's future profiles.py.
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from panel_context import PanelContext


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    state = ctx.state.profiles

    imgui.text(f"{fa.ICON_FA_FOLDER_OPEN}  Profiles")
    widgets.muted_text(
        theme,
        "Save/load named per-game configs. Loading a profile always starts with "
        "Remapper/Macros/Window Select disabled, except whatever you mark below "
        "to survive the load.",
    )
    imgui.spacing()

    imgui.set_next_item_width(240)
    changed, state.new_profile_draft = imgui.input_text("##newprofile", state.new_profile_draft)
    imgui.same_line()
    can_create = bool(state.new_profile_draft.strip())
    if not can_create:
        imgui.begin_disabled()
    if imgui.button(f"{fa.ICON_FA_PLUS}  Create Profile"):
        state.add_profile(state.new_profile_draft.strip())
        state.new_profile_draft = ""
    if not can_create:
        imgui.end_disabled()

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
                    state.active_id = profile.id

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
            _, profile.persist_remapper = widgets.labeled_toggle(theme, "Remapper", profile.persist_remapper)
            imgui.same_line()
            imgui.dummy(imgui.ImVec2(12, 0))
            imgui.same_line()
            _, profile.persist_macros = widgets.labeled_toggle(theme, "Macros", profile.persist_macros)
            imgui.same_line()
            imgui.dummy(imgui.ImVec2(12, 0))
            imgui.same_line()
            _, profile.persist_window_select = widgets.labeled_toggle(
                theme, "Window Select", profile.persist_window_select
            )

            imgui.pop_id()
        imgui.spacing()

    if remove_id is not None:
        state.remove_profile(remove_id)
