"""Coverage for input_inject.py's mouse-move primitives (send_mouse_move_absolute/relative, get_cursor_pos,
_normalize_absolute). Never calls the real Win32 SendInput/GetCursorPos/GetSystemMetrics -- user32's methods are monkeypatched with Mocks that inspect what was passed.
"""

from __future__ import annotations

import ctypes
from unittest.mock import Mock

import pytest

import input_inject
from input_inject import (
    INJECTED_MARKER,
    INPUT,
    INPUT_MOUSE,
    MOUSEEVENTF_ABSOLUTE,
    MOUSEEVENTF_MOVE,
    MOUSEEVENTF_VIRTUALDESK,
    get_cursor_pos,
    send_mouse_move_absolute,
    send_mouse_move_by_step,
    send_mouse_move_relative,
)


@pytest.fixture(autouse=True)
def _stub_send_input(monkeypatch):
    mock = Mock(return_value=1)
    monkeypatch.setattr(input_inject.user32, "SendInput", mock)
    yield mock


def _sent_mouseinput(mock) -> "input_inject.MOUSEINPUT":
    """Pulls the MOUSEINPUT struct out of the one INPUT SendInput was called with -- passed as a real ctypes array, not a Mock, so indexing/.mi works exactly like the real API."""
    args = mock.call_args[0]
    input_array = args[1]
    assert isinstance(input_array[0], INPUT)
    return input_array[0].mi


def test_send_mouse_move_relative_uses_plain_move_flag_only(_stub_send_input):
    send_mouse_move_relative(15, -30)

    mi = _sent_mouseinput(_stub_send_input)
    assert mi.dx == 15
    assert mi.dy == -30
    assert mi.dwFlags == MOUSEEVENTF_MOVE  # no ABSOLUTE/VIRTUALDESK bits
    assert mi.dwExtraInfo == INJECTED_MARKER


def test_send_mouse_move_relative_tags_injected_marker(_stub_send_input):
    send_mouse_move_relative(1, 1)

    mi = _sent_mouseinput(_stub_send_input)
    assert mi.dwExtraInfo == INJECTED_MARKER


@pytest.fixture
def _stub_virtual_desktop(monkeypatch):
    """A 1920x1080 single-monitor virtual desktop starting at the origin --
    the common case."""

    def _metrics(index):
        return {
            input_inject.SM_XVIRTUALSCREEN: 0,
            input_inject.SM_YVIRTUALSCREEN: 0,
            input_inject.SM_CXVIRTUALSCREEN: 1920,
            input_inject.SM_CYVIRTUALSCREEN: 1080,
        }[index]

    monkeypatch.setattr(input_inject.user32, "GetSystemMetrics", Mock(side_effect=_metrics))
    yield


def test_send_mouse_move_absolute_uses_absolute_and_virtualdesk_flags(_stub_send_input, _stub_virtual_desktop):
    send_mouse_move_absolute(960, 540)  # dead center

    mi = _sent_mouseinput(_stub_send_input)
    assert mi.dwFlags == (MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)
    # Center of a 1920x1080 desktop normalizes to the middle of 0-65535.
    assert mi.dx == pytest.approx(32768, abs=2)
    assert mi.dy == pytest.approx(32768, abs=2)


def test_send_mouse_move_absolute_top_left_normalizes_to_zero(_stub_send_input, _stub_virtual_desktop):
    send_mouse_move_absolute(0, 0)

    mi = _sent_mouseinput(_stub_send_input)
    assert mi.dx == 0
    assert mi.dy == 0


def test_send_mouse_move_absolute_handles_negative_origin_secondary_monitor(_stub_send_input, monkeypatch):
    # A monitor placed to the left of the primary gives the virtual desktop
    # a negative origin -- the recorded point must still resolve correctly
    # relative to that origin, not to (0, 0).
    def _metrics(index):
        return {
            input_inject.SM_XVIRTUALSCREEN: -1920,
            input_inject.SM_YVIRTUALSCREEN: 0,
            input_inject.SM_CXVIRTUALSCREEN: 3840,  # two 1920-wide monitors combined
            input_inject.SM_CYVIRTUALSCREEN: 1080,
        }[index]

    monkeypatch.setattr(input_inject.user32, "GetSystemMetrics", Mock(side_effect=_metrics))

    send_mouse_move_absolute(-1920, 0)  # leftmost edge of the virtual desktop

    mi = _sent_mouseinput(_stub_send_input)
    assert mi.dx == 0
    assert mi.dy == 0


def test_send_mouse_move_absolute_tags_injected_marker(_stub_send_input, _stub_virtual_desktop):
    send_mouse_move_absolute(100, 100)

    mi = _sent_mouseinput(_stub_send_input)
    assert mi.dwExtraInfo == INJECTED_MARKER


def test_get_cursor_pos_returns_a_real_coordinate_pair():
    # GetCursorPos is read-only, so this calls the real Win32 API to confirm the byref()/POINT plumbing round-trips correctly.
    x, y = get_cursor_pos()
    assert isinstance(x, int)
    assert isinstance(y, int)


def test_get_cursor_pos_raises_on_failure(monkeypatch):
    monkeypatch.setattr(input_inject.user32, "GetCursorPos", Mock(return_value=0))  # FALSE
    monkeypatch.setattr(ctypes, "get_last_error", Mock(return_value=0))

    with pytest.raises(OSError):
        get_cursor_pos()


@pytest.fixture(autouse=True)
def _reset_timer_resolution_state():
    input_inject._timer_resolution_raised = False
    yield
    input_inject._timer_resolution_raised = False


def test_raise_timer_resolution_requests_1ms(monkeypatch):
    mock = Mock(return_value=0)  # TIMERR_NOERROR
    monkeypatch.setattr(input_inject.winmm, "timeBeginPeriod", mock)

    input_inject.raise_timer_resolution()

    mock.assert_called_once_with(1)
    assert input_inject._timer_resolution_raised is True


def test_restore_timer_resolution_calls_end_period_after_a_successful_raise(monkeypatch):
    monkeypatch.setattr(input_inject.winmm, "timeBeginPeriod", Mock(return_value=0))
    end_mock = Mock(return_value=0)
    monkeypatch.setattr(input_inject.winmm, "timeEndPeriod", end_mock)

    input_inject.raise_timer_resolution()
    input_inject.restore_timer_resolution()

    end_mock.assert_called_once_with(1)
    assert input_inject._timer_resolution_raised is False


def test_restore_timer_resolution_is_a_no_op_without_a_prior_raise(monkeypatch):
    # timeEndPeriod must be paired with a real prior timeBeginPeriod -- calling it unpaired is a documented
    # error, not a safe no-op, so restore must never call it when raise never succeeded.
    end_mock = Mock(return_value=0)
    monkeypatch.setattr(input_inject.winmm, "timeEndPeriod", end_mock)

    input_inject.restore_timer_resolution()

    end_mock.assert_not_called()


def test_restore_timer_resolution_is_a_no_op_when_raise_failed(monkeypatch):
    monkeypatch.setattr(input_inject.winmm, "timeBeginPeriod", Mock(return_value=97))  # TIMERR_NOCANDO
    end_mock = Mock(return_value=0)
    monkeypatch.setattr(input_inject.winmm, "timeEndPeriod", end_mock)

    input_inject.raise_timer_resolution()
    input_inject.restore_timer_resolution()

    end_mock.assert_not_called()
