"""Coverage for panels/macros.py's _move_step_up/_move_step_down -- swaps a step with its neighbor,
no-ops at either end of the list."""

from __future__ import annotations

from app_state import MacroDef, MacroStep, MacroStepKind
from panels.macros import _move_step_up, _move_step_down


def _macro_with_steps(*ids: str) -> MacroDef:
    macro = MacroDef(id="m1", name="M")
    macro.steps = [MacroStep(id=i, kind=MacroStepKind.KEY_TAP) for i in ids]
    return macro


def test_move_up_swaps_with_the_previous_step():
    macro = _macro_with_steps("a", "b", "c")

    _move_step_up(macro, "b")

    assert [s.id for s in macro.steps] == ["b", "a", "c"]


def test_move_up_is_a_no_op_for_the_first_step():
    macro = _macro_with_steps("a", "b", "c")

    _move_step_up(macro, "a")

    assert [s.id for s in macro.steps] == ["a", "b", "c"]


def test_move_down_swaps_with_the_next_step():
    macro = _macro_with_steps("a", "b", "c")

    _move_step_down(macro, "b")

    assert [s.id for s in macro.steps] == ["a", "c", "b"]


def test_move_down_is_a_no_op_for_the_last_step():
    macro = _macro_with_steps("a", "b", "c")

    _move_step_down(macro, "c")

    assert [s.id for s in macro.steps] == ["a", "b", "c"]


def test_move_up_and_down_are_no_ops_for_an_unknown_step_id():
    macro = _macro_with_steps("a", "b", "c")

    _move_step_up(macro, "nonexistent")
    _move_step_down(macro, "nonexistent")

    assert [s.id for s in macro.steps] == ["a", "b", "c"]
