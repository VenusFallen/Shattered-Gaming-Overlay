"""Coverage for panels/macros.py's _combined_trigger_label -- the display text for the single
combined trigger+modifiers bind button (e.g. "L Shift + R")."""

from __future__ import annotations

from app_state import MacroDef
from key_capture import KeyBind, UNBOUND
from panels.macros import _combined_trigger_label

SHIFT = KeyBind(vk_code=0x10, name="L Shift")
CTRL = KeyBind(vk_code=0x11, name="Ctrl")
R_KEY = KeyBind(vk_code=0x52, name="R")


def test_label_is_unbound_with_no_trigger():
    macro = MacroDef(id="m1", name="M", trigger=UNBOUND)
    assert _combined_trigger_label(macro) == "Unbound"


def test_label_is_just_the_key_name_with_no_modifiers():
    macro = MacroDef(id="m1", name="M", trigger=SHIFT)
    assert _combined_trigger_label(macro) == "L Shift"


def test_label_combines_one_modifier_and_the_trigger():
    macro = MacroDef(id="m1", name="M", trigger=R_KEY, trigger_modifiers=[SHIFT])
    assert _combined_trigger_label(macro) == "L Shift + R"


def test_label_combines_multiple_modifiers_and_the_trigger():
    macro = MacroDef(id="m1", name="M", trigger=R_KEY, trigger_modifiers=[SHIFT, CTRL])
    assert _combined_trigger_label(macro) == "L Shift + Ctrl + R"
