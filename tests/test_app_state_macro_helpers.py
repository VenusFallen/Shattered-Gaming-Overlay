"""Coverage for MacroDef.has_move_by_step(), the check panels/macros.py uses to lock a macro's Mode combo to Once while it contains a Move Mouse By step."""

from __future__ import annotations

from app_state import MacroDef, MacroStepKind


def _macro() -> MacroDef:
    return MacroDef(id="m1", name="M")


def test_has_move_by_step_false_with_no_steps():
    assert _macro().has_move_by_step() is False


def test_has_move_by_step_false_with_unrelated_steps():
    macro = _macro()
    macro.add_step().kind = MacroStepKind.KEY_TAP
    macro.add_step().kind = MacroStepKind.MOUSE_MOVE_TO

    assert macro.has_move_by_step() is False


def test_has_move_by_step_true_when_present():
    macro = _macro()
    macro.add_step().kind = MacroStepKind.KEY_TAP
    macro.add_step().kind = MacroStepKind.MOUSE_MOVE_BY

    assert macro.has_move_by_step() is True
