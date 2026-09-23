"""Coverage for profiles.py's Macro JSON round-trip, specifically MacroStep.move_x/move_y (Move Mouse To/By) and MacroDef.trigger_modifiers (combo triggers like Shift+R)."""

from __future__ import annotations

from app_state import MacroDef, MacroStep, MacroStepKind
from key_capture import KeyBind
from profiles import _macro_from_json, _macro_to_json, _step_from_json, _step_to_json

SHIFT = KeyBind(vk_code=0x10, name="Shift")
CTRL = KeyBind(vk_code=0x11, name="Ctrl")
R_KEY = KeyBind(vk_code=0x52, name="R")


def test_step_round_trips_move_coordinates():
    step = MacroStep(id="s1", kind=MacroStepKind.MOUSE_MOVE_TO, move_x=812, move_y=-40)

    restored = _step_from_json(_step_to_json(step))

    assert restored.move_x == 812
    assert restored.move_y == -40


def test_step_from_json_defaults_missing_move_coordinates_to_zero():
    restored = _step_from_json({"id": "s1", "kind": "Key Tap"})

    assert restored.move_x == 0
    assert restored.move_y == 0


def test_step_round_trips_move_speed_pct():
    step = MacroStep(id="s1", kind=MacroStepKind.MOUSE_MOVE_TO, move_speed_pct=80)

    restored = _step_from_json(_step_to_json(step))

    assert restored.move_speed_pct == 80


def test_step_from_json_defaults_missing_move_speed_pct_to_50():
    restored = _step_from_json({"id": "s1", "kind": "Move Mouse To"})

    assert restored.move_speed_pct == 50


def test_macro_round_trips_trigger_modifiers():
    macro = MacroDef(id="m1", name="M", trigger=R_KEY, trigger_modifiers=[SHIFT, CTRL])

    restored = _macro_from_json(_macro_to_json(macro))

    assert restored.trigger == R_KEY
    assert restored.trigger_modifiers == [SHIFT, CTRL]


def test_macro_from_json_defaults_missing_trigger_modifiers_to_empty_list():
    restored = _macro_from_json({"id": "m1", "name": "M"})

    assert restored.trigger_modifiers == []


def test_macro_from_json_skips_non_dict_modifier_entries():
    restored = _macro_from_json({"id": "m1", "name": "M", "trigger_modifiers": ["garbage", 123, {"vk_code": 0x10, "name": "Shift"}]})

    assert len(restored.trigger_modifiers) == 1
    assert restored.trigger_modifiers[0].name == "Shift"
