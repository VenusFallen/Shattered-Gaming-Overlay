"""tests/test_stats_percentile_lows.py -- unit coverage for stats_poller's
1%/0.1% frame-time-low calc (_percentile_low_fps) and the larger history
buffer feeding it (_FpsTracker.get_percentile_lows/get_frame_time_history).

Pure data logic only -- never spins up a real PresentMon subprocess or
touches the filesystem; _FpsTracker's internal deques are populated directly
so these tests don't depend on _read_loop's stdout-parsing thread at all.
See test_profiles_auto_switch.py/test_profiles_share.py for this project's
existing pytest conventions.
"""

from __future__ import annotations

import pytest

import stats_poller


# ---------------------------------------------------------------------------
# _percentile_low_fps -- pure function, no tracker/locking involved
# ---------------------------------------------------------------------------


def test_percentile_low_empty_returns_none():
    assert stats_poller._percentile_low_fps([], 0.01) is None


def test_percentile_low_all_zero_returns_none():
    # Shouldn't happen in practice (ms<=0 samples are filtered before they
    # ever reach a deque -- see _read_loop), but the pure function itself
    # must not divide by zero if it's ever handed degenerate input.
    assert stats_poller._percentile_low_fps([0.0, 0.0, 0.0], 0.01) is None


def test_percentile_low_single_spike_in_steady_samples():
    # 99 steady 10ms (100fps) frames + one 100ms (10fps) spike. With only
    # 100 samples, both the 1% and 0.1% buckets round up to just the single
    # worst sample (max(1, round(n*fraction))) -- both lows should equal
    # the spike's fps-equivalent, not the steady 100fps baseline.
    samples = [10.0] * 99 + [100.0]
    assert stats_poller._percentile_low_fps(samples, 0.01) == pytest.approx(10.0)
    assert stats_poller._percentile_low_fps(samples, 0.001) == pytest.approx(10.0)


def test_percentile_low_averages_the_tail_not_just_the_worst():
    # 970 steady 10ms frames + 30 slow 50ms frames (3% of 1000 samples).
    # 1% low (count=10) should average 10 of the 50ms frames -> 20fps, not
    # get dragged toward the 970 steady frames.
    samples = [10.0] * 970 + [50.0] * 30
    assert stats_poller._percentile_low_fps(samples, 0.01) == pytest.approx(20.0)


def test_percentile_low_0_1_pct_uses_the_slowest_tail():
    # 3990 steady 10ms frames + 10 very slow 200ms frames (0.25% of 4000).
    # 0.1% low (count=4) should average 4 of the 200ms frames -> 5fps.
    samples = [10.0] * 3990 + [200.0] * 10
    assert stats_poller._percentile_low_fps(samples, 0.001) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# _FpsTracker.get_percentile_lows() / get_frame_time_history() -- the
# tracker's read API, exercised by writing directly into its internal
# deques rather than driving a real PresentMon subprocess.
# ---------------------------------------------------------------------------


@pytest.fixture
def tracker(monkeypatch) -> stats_poller._FpsTracker:
    t = stats_poller._FpsTracker()
    # Fixed fake clock -- avoids any dependency on real process/machine
    # uptime for the staleness gate (mirrors get_fps()'s own check).
    monkeypatch.setattr(stats_poller.time, "monotonic", lambda: 1000.0)
    return t


def _fill(tracker: stats_poller._FpsTracker, ms_values) -> None:
    for v in ms_values:
        tracker._history.append(v)
        tracker._samples.append(v)
    tracker._last_sample_ts = stats_poller.time.monotonic()


def test_percentile_lows_none_below_min_samples(tracker):
    _fill(tracker, [10.0] * (stats_poller._FPS_PERCENTILE_MIN_SAMPLES - 1))
    assert tracker.get_percentile_lows() is None


def test_percentile_lows_populated_once_min_samples_reached(tracker):
    _fill(tracker, [10.0] * stats_poller._FPS_PERCENTILE_MIN_SAMPLES)
    lows = tracker.get_percentile_lows()
    assert lows is not None
    low_1pct, low_0_1pct = lows
    assert low_1pct == pytest.approx(100.0)
    assert low_0_1pct == pytest.approx(100.0)


def test_percentile_lows_none_when_stale(tracker, monkeypatch):
    _fill(tracker, [10.0] * stats_poller._FPS_PERCENTILE_MIN_SAMPLES)
    # Advance the fake clock well past _FPS_STALE_SEC since the last sample.
    monkeypatch.setattr(
        stats_poller.time, "monotonic",
        lambda: 1000.0 + stats_poller._FPS_STALE_SEC + 1.0)
    assert tracker.get_percentile_lows() is None


def test_frame_time_history_returns_most_recent_n(tracker):
    values = [float(i) for i in range(200)]
    _fill(tracker, values)
    assert tracker.get_frame_time_history(n=50) == tuple(values[-50:])


def test_frame_time_history_empty_when_stale(tracker, monkeypatch):
    _fill(tracker, [10.0] * 50)
    monkeypatch.setattr(
        stats_poller.time, "monotonic",
        lambda: 1000.0 + stats_poller._FPS_STALE_SEC + 1.0)
    assert tracker.get_frame_time_history() == ()


def test_frame_time_history_empty_when_no_samples(tracker):
    assert tracker.get_frame_time_history() == ()
