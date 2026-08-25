"""panels/about.py -- About panel: what the program is, version, and credit.

Split out of settings.py into its own top-level nav entry (was previously a
card inside Settings).
"""

from __future__ import annotations

from imgui_bundle import icons_fontawesome_4 as fa
from imgui_bundle import imgui

import widgets
from panel_context import PanelContext
from version import VERSION

_GITHUB_URL = "https://github.com/VenusFallen"


def render(ctx: PanelContext) -> None:
    theme = ctx.theme
    imgui.text(f"{fa.ICON_FA_INFO_CIRCLE}  About")
    imgui.spacing()

    with widgets.card(theme, "about-main", size=(0, 0)):
        widgets.section_title("About")
        imgui.text_wrapped(
            "Shattered Gaming Overlay is a free, open-source accessibility companion for Windows "
            "gaming: remap keys and buttons, build macros, and manage per-game profiles, with a "
            "toggleable in-game HUD -- a stats display and an accessibility crosshair -- layered on "
            "top of whatever you're playing."
        )
        imgui.spacing()
        widgets.muted_text(theme, f"Version {VERSION}")
        widgets.muted_text(theme, "Made by VenusFallen")
        imgui.spacing()
        widgets.hyperlink(theme, f"{fa.ICON_FA_EXTERNAL_LINK_ALT}  github.com/VenusFallen", _GITHUB_URL)
