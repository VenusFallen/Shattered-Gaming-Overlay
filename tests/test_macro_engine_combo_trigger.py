"""Coverage for MacroDef.trigger_modifiers (e.g. Shift+R): modifiers are evaluated at the instant the
trigger key goes down and remembered per-runtime as "armed", so a release-driven mode still respects
what was true at press time. input_inject.send_key is monkeypatched so nothing here calls real SendInput.
"""

from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

import input_inject
from app_state import MacroDef, MacroMode, MacroStepKind, MacrosState, SettingsState
from key_capture import KeyBind
from macro_engine import MacroEngine
from remapper import EffectiveInputEvent

SHIFT = KeyBind(vk_code=0x10, name="Shift")
CTRL = KeyBind(vk_code=0x11, name="Ctrl")
R_KEY = KeyBind(vk_code=0x52, name="R")


@pytest.fixture(autouse=True)
def _stub_injection(monkeypatch):
    monkeypatch.setattr(input_inject, "send_key", Mock())
    monkeypatch.setattr(input_inject, "send_mouse_button", Mock())
    yield


@pytest.fixture(autouse=True)
def _run_threads_synchronously(monkeypatch):
    """_on_trigger spawns real background threads for playback -- run them synchronously so a test can assert on side effects immediately."""

    class _SyncThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            self._target = target
            self._args = args

        def start(self):
            if self._target:
                self._target(*self._args)

        def is_alive(self):
            return False

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(threading, "Thread", _SyncThread)
    yield


def _press(engine: MacroEngine, vk: int) -> None:
    engine.handle_effective_event(EffectiveInputEvent(vk_code=vk, up=False))


def _release(engine: MacroEngine, vk: int) -> None:
    engine.handle_effective_event(EffectiveInputEvent(vk_code=vk, up=True))


def _once_macro_with_modifiers(modifiers) -> MacrosState:
    macro = MacroDef(id="m1", name="M", trigger=R_KEY, trigger_modifiers=modifiers, mode=MacroMode.ONCE, enabled=True)
    macro.add_step().kind = MacroStepKind.KEY_TAP
    macro.steps[0].key = R_KEY
    state = MacrosState()
    state.macros = [macro]
    return state


def _engine_with(state: MacrosState) -> MacroEngine:
    engine = MacroEngine()
    engine.start()
    engine.update_snapshot(state, SettingsState())
    return engine


# ---------------------------------------------------------------------------
# Basic combo matching
# ---------------------------------------------------------------------------


def test_trigger_fires_when_modifier_is_held_first():
    engine = _engine_with(_once_macro_with_modifiers([SHIFT]))

    _press(engine, SHIFT.vk_code)
    _press(engine, R_KEY.vk_code)
    _release(engine, R_KEY.vk_code)

    input_inject.send_key.assert_any_call(R_KEY.vk_code, key_up=False)


def test_trigger_does_not_fire_without_its_modifier():
    engine = _engine_with(_once_macro_with_modifiers([SHIFT]))

    _press(engine, R_KEY.vk_code)  # Shift never pressed
    _release(engine, R_KEY.vk_code)

    input_inject.send_key.assert_not_called()


def test_multiple_modifiers_all_required():
    engine = _engine_with(_once_macro_with_modifiers([SHIFT, CTRL]))

    _press(engine, SHIFT.vk_code)
    _press(engine, R_KEY.vk_code)  # only Shift held, not Ctrl
    _release(engine, R_KEY.vk_code)

    input_inject.send_key.assert_not_called()

    input_inject.send_key.reset_mock()
    _press(engine, CTRL.vk_code)
    _press(engine, R_KEY.vk_code)  # now both held
    _release(engine, R_KEY.vk_code)

    input_inject.send_key.assert_any_call(R_KEY.vk_code, key_up=False)


def test_extra_unrelated_held_keys_do_not_block_a_match():
    engine = _engine_with(_once_macro_with_modifiers([SHIFT]))

    _press(engine, CTRL.vk_code)  # unrelated key, also held
    _press(engine, SHIFT.vk_code)
    _press(engine, R_KEY.vk_code)
    _release(engine, R_KEY.vk_code)

    input_inject.send_key.assert_any_call(R_KEY.vk_code, key_up=False)


def test_no_modifiers_configured_behaves_exactly_like_a_plain_trigger():
    engine = _engine_with(_once_macro_with_modifiers([]))

    _press(engine, R_KEY.vk_code)
    _release(engine, R_KEY.vk_code)

    input_inject.send_key.assert_any_call(R_KEY.vk_code, key_up=False)


# ---------------------------------------------------------------------------
# Armed-at-press-time semantics
# ---------------------------------------------------------------------------


def test_releasing_modifier_before_trigger_release_still_fires_once_mode():
    # Once mode fires on release -- what matters is whether the modifier
    # was held at PRESS time, not whether it's still held at release time.
    engine = _engine_with(_once_macro_with_modifiers([SHIFT]))

    _press(engine, SHIFT.vk_code)
    _press(engine, R_KEY.vk_code)
    _release(engine, SHIFT.vk_code)  # modifier let go first
    _release(engine, R_KEY.vk_code)  # trigger released after

    input_inject.send_key.assert_any_call(R_KEY.vk_code, key_up=False)


def test_pressing_trigger_without_modifier_then_release_does_not_fire_even_if_modifier_pressed_between():
    engine = _engine_with(_once_macro_with_modifiers([SHIFT]))

    _press(engine, R_KEY.vk_code)  # not armed -- Shift wasn't down yet
    _press(engine, SHIFT.vk_code)  # too late, R is already down
    _release(engine, R_KEY.vk_code)

    input_inject.send_key.assert_not_called()


def test_second_press_of_trigger_re_evaluates_modifiers():
    engine = _engine_with(_once_macro_with_modifiers([SHIFT]))

    _press(engine, R_KEY.vk_code)  # unarmed press
    _release(engine, R_KEY.vk_code)  # doesn't fire
    _press(engine, SHIFT.vk_code)
    _press(engine, R_KEY.vk_code)  # armed press
    _release(engine, R_KEY.vk_code)  # fires

    input_inject.send_key.assert_any_call(R_KEY.vk_code, key_up=False)


# ---------------------------------------------------------------------------
# Disambiguating multiple macros on the same trigger key
# ---------------------------------------------------------------------------


def test_plain_and_combo_macro_on_same_key_each_fire_correctly():
    combo = MacroDef(id="combo", name="Combo", trigger=R_KEY, trigger_modifiers=[SHIFT], mode=MacroMode.ONCE, enabled=True)
    combo.add_step().kind = MacroStepKind.KEY_TAP
    combo.steps[0].key = KeyBind(vk_code=0xC0, name="Backtick")  # distinct injected key, so calls are distinguishable

    plain = MacroDef(id="plain", name="Plain", trigger=R_KEY, trigger_modifiers=[], mode=MacroMode.ONCE, enabled=True)
    plain.add_step().kind = MacroStepKind.KEY_TAP
    plain.steps[0].key = KeyBind(vk_code=0xC1, name="Tilde")

    state = MacrosState()
    state.macros = [combo, plain]
    engine = _engine_with(state)

    # Plain R (no Shift) -- combo doesn't match, falls through to plain.
    _press(engine, R_KEY.vk_code)
    _release(engine, R_KEY.vk_code)
    input_inject.send_key.assert_any_call(0xC1, key_up=False)
    assert 0xC0 not in [c.args[0] for c in input_inject.send_key.call_args_list]

    input_inject.send_key.reset_mock()

    # Shift+R -- combo matches and wins (listed first).
    _press(engine, SHIFT.vk_code)
    _press(engine, R_KEY.vk_code)
    _release(engine, R_KEY.vk_code)
    _release(engine, SHIFT.vk_code)
    input_inject.send_key.assert_any_call(0xC0, key_up=False)
    assert 0xC1 not in [c.args[0] for c in input_inject.send_key.call_args_list]
