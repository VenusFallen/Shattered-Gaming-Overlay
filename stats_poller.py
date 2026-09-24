"""Background hardware-stats + FPS polling, feeding the Stats HUD (CPU/GPU usage & temp, VRAM, RAM, FPS,
1%/0.1% frame-time lows, recent-frame-time history for the live graph). LibreHardwareMonitor is opened with
Ring0 disabled (no kernel driver); PresentMon reads frame timing passively via ETW -- no game-process writes
or injection either way. FPS tracking follows real OS foreground focus, NOT gated by the window-select
process filter (that only gates Remapper/Macro matching/injection).
"""

from __future__ import annotations

import heapq
import logging
import signal
import statistics
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

import window_select

_log = logging.getLogger("shattered_overlay.stats")

_lhm_available = False
_Computer = None
_bootstrap_error: Optional[str] = None

# ---------------------------------------------------------------------------
# PresentMon / FPS tracking
# ---------------------------------------------------------------------------
_FPS_ROLLING_WINDOW = 30    # frames averaged for the smoothed FPS value
# PresentMon's piped stdout is block-buffered, so samples arrive in lumpy bursts (worst observed gap ~6s
# on a steady 60fps game); 10s gives margin above that without masking a genuine crash/exit for long.
_FPS_STALE_SEC = 10.0       # no fresh sample in this long -> report no FPS
_FPS_MIN_SAMPLES = 10       # below this, a single slow sample could dominate the rolling median -- hold at "no data"
_PID_DEBOUNCE_SEC = 0.75    # foreground pid must be stable this long before retargeting

# 30 samples is enough for a responsive median but far too few for a meaningful 1%/0.1% low, so a second,
# much larger buffer (_history) is kept just for percentile-low calc + the HUD graph. 4000 puts 4 real
# samples in the bottom 0.1% bucket (40 in the bottom 1%) while still being a live rolling window.
_FPS_HISTORY_WINDOW = 4000
_FPS_PERCENTILE_MIN_SAMPLES = 300  # below this, a 0.1%-low bucket would round to a single sample
_FPS_GRAPH_POINTS = 90  # glance-sized recent window for the HUD's live graph, not the full percentile history

_pm_missing_warned = False       # log "binary not found" only once per session
_pm_launch_failed_warned = False  # log "failed to launch" only once per session


def _presentmon_path() -> Path:
    """Resolve presentmon/PresentMon.exe, same dev-mode vs. frozen (sys._MEIPASS) pattern `_bootstrap()` uses for lib/."""
    try:
        if getattr(sys, "frozen", False):
            persistent = Path(sys.executable).parent / "presentmon"
            bundled = Path(sys._MEIPASS) / "presentmon"  # type: ignore[attr-defined]
            pm_dir = persistent if (persistent / "PresentMon.exe").exists() else bundled
        else:
            pm_dir = Path(__file__).parent / "presentmon"
    except Exception:
        pm_dir = Path(__file__).parent / "presentmon"
    return pm_dir / "PresentMon.exe"


def _warn_presentmon_missing_once(path: Path) -> None:
    global _pm_missing_warned
    if _pm_missing_warned:
        return
    _pm_missing_warned = True
    try:
        _log.warning("PresentMon.exe not found at %s -- FPS tracking disabled for this session", path)
    except Exception:
        pass


def _warn_presentmon_launch_failed_once(exc: Exception) -> None:
    global _pm_launch_failed_warned
    if _pm_launch_failed_warned:
        return
    _pm_launch_failed_warned = True
    try:
        _log.warning("Failed to launch PresentMon.exe", exc_info=exc)
    except Exception:
        pass


def _percentile_low_fps(ms_samples, fraction: float) -> Optional[float]:
    """1%/0.1%-low, CapFrameX/MSI Afterburner style: the average frame time of the slowest `fraction` share of samples (not a percentile boundary), converted to fps. Pure function, unit-testable without a real _FpsTracker."""
    n = len(ms_samples)
    if n == 0:
        return None
    count = max(1, round(n * fraction))
    slowest = heapq.nlargest(count, ms_samples)
    avg_ms = sum(slowest) / len(slowest)
    if avg_ms <= 0:
        return None
    return 1000.0 / avg_ms


class _FpsTracker:
    """Owns one PresentMon subprocess targeting a single PID at a time. Reads its live stdout CSV stream on
    a background thread, keeps a rolling window of msBetweenPresents samples, and exposes a smoothed FPS
    value via start(pid, exe)/stop()/get_fps(). The first sample after every start()/restart is discarded
    (a known ETW artifact, not a real frame interval), and get_fps() uses the MEDIAN of the window rather
    than the mean so occasional dropped-frame spikes don't drag it down. A second, much larger buffer
    (`_history`) is kept purely for 1%/0.1%-low calc and the HUD graph, leaving get_fps() just as responsive."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._samples: deque = deque(maxlen=_FPS_ROLLING_WINDOW)
        self._history: deque = deque(maxlen=_FPS_HISTORY_WINDOW)
        self._last_sample_ts = 0.0
        self._skip_next_sample = True
        self.last_error: Optional[str] = None

    def start(self, pid: int, exe_path: Path) -> None:
        self.stop()
        self.last_error = None
        try:
            self._proc = subprocess.Popen(
                [str(exe_path), "--process_id", str(pid), "--output_stdout",
                 # Self-heals if a previous child was hard-killed without
                 # tearing down its ETW trace session, which would otherwise
                 # make this launch fail with "trace session already running".
                 "--stop_existing_session"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                # NEW_PROCESS_GROUP is required for send_signal(CTRL_BREAK_EVENT)
                # in stop() below to target only this child.
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self._proc = None
            self.last_error = f"Failed to launch PresentMon.exe: {exc}"
            _warn_presentmon_launch_failed_once(exc)
            return
        with self._lock:
            self._samples.clear()
            self._history.clear()
            self._last_sample_ts = 0.0
            self._skip_next_sample = True
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="SGO-PresentMonReader")
        self._reader_thread.start()

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                # Graceful CTRL_BREAK_EVENT lets PresentMon's console handler stop its ETW session cleanly;
                # TerminateProcess skips that handler and can orphan the session until the next --stop_existing_session.
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        thread = self._reader_thread
        self._reader_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.3)
        if proc is not None:
            try:
                proc.wait(timeout=0.3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        with self._lock:
            self._samples.clear()
            self._history.clear()

    def get_fps(self) -> Optional[float]:
        with self._lock:
            if len(self._samples) < _FPS_MIN_SAMPLES:
                return None
            if time.monotonic() - self._last_sample_ts > _FPS_STALE_SEC:
                return None
            median_ms = statistics.median(self._samples)  # see class docstring
        if median_ms <= 0:
            return None
        return 1000.0 / median_ms

    def get_percentile_lows(self) -> Optional[Tuple[float, float]]:
        """(1%-low fps, 0.1%-low fps) over the large `_history` window, or None if not enough samples yet or the feed's gone stale."""
        with self._lock:
            if len(self._history) < _FPS_PERCENTILE_MIN_SAMPLES:
                return None
            if time.monotonic() - self._last_sample_ts > _FPS_STALE_SEC:
                return None
            samples = list(self._history)
        low_1pct = _percentile_low_fps(samples, 0.01)
        low_0_1pct = _percentile_low_fps(samples, 0.001)
        if low_1pct is None or low_0_1pct is None:
            return None
        return low_1pct, low_0_1pct

    def get_frame_time_history(self, n: int = _FPS_GRAPH_POINTS) -> Tuple[float, ...]:
        """Most recent up-to-`n` raw msBetweenPresents samples, oldest first, feeding the HUD's live graph; stale-gated so it doesn't show dead data after the game exits."""
        with self._lock:
            if not self._history:
                return ()
            if time.monotonic() - self._last_sample_ts > _FPS_STALE_SEC:
                return ()
            hist = list(self._history)
        return tuple(hist[-n:])

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        header_idx = None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if header_idx is None:
                    # Match case-insensitively -- shipped PresentMon builds
                    # emit "msBetweenPresents", not the "MsBetweenPresents"
                    # capitalization used in some docs/older builds.
                    cols = [c.strip().lower() for c in line.split(",")]
                    header_idx = cols.index("msbetweenpresents") if "msbetweenpresents" in cols else -1
                    continue
                if header_idx < 0:
                    continue
                parts = line.split(",")
                if header_idx >= len(parts):
                    continue
                try:
                    ms = float(parts[header_idx])
                except ValueError:
                    continue
                if ms <= 0:
                    continue
                with self._lock:
                    # First row of a fresh session measures session-start to first present, not a real frame interval -- discard it.
                    if self._skip_next_sample:
                        self._skip_next_sample = False
                        continue
                    self._samples.append(ms)
                    self._history.append(ms)
                    self._last_sample_ts = time.monotonic()
        except Exception:
            # Normal on terminate() (pipe closed mid-read) -- not worth logging.
            pass


# ---------------------------------------------------------------------------
# LibreHardwareMonitor bootstrap
# ---------------------------------------------------------------------------


def _lib_dir() -> Path:
    """Resolve lib/, same dev-mode vs. frozen pattern as `_presentmon_path()` -- persistent lib/ next to the exe wins if present, else the bundled sys._MEIPASS copy."""
    try:
        if getattr(sys, "frozen", False):
            persistent = Path(sys.executable).parent / "lib"
            bundled = Path(sys._MEIPASS) / "lib"  # type: ignore[attr-defined]
            return persistent if (persistent / "LibreHardwareMonitorLib.dll").exists() else bundled
    except Exception:
        pass
    return Path(__file__).parent / "lib"


def _bootstrap() -> None:
    """Load pythonnet + LibreHardwareMonitorLib.dll. Sets `_lhm_available` True only on full success, else `_bootstrap_error`. Never raises -- runs at import time."""
    global _lhm_available, _Computer, _bootstrap_error
    lib_dir = _lib_dir()

    if not (lib_dir / "LibreHardwareMonitorLib.dll").exists():
        _bootstrap_error = f"LibreHardwareMonitorLib.dll not found in {lib_dir}"
        return

    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    # Remove Zone.Identifier ADS -- Windows blocks internet-downloaded files until unblocked; no-op if absent.
    try:
        import ctypes
        for dll in lib_dir.glob("*.dll"):
            ctypes.windll.kernel32.DeleteFileW(str(dll) + ":Zone.Identifier")
    except Exception:
        pass

    # Detect which .NET runtime the LHM DLL targets: net472 builds contain b'.NETFramework'; newer
    # .NET Standard 2.0 builds don't and need pythonnet's "coreclr" runtime, not "netfx".
    runtime = "netfx"
    try:
        dll_bytes = (lib_dir / "LibreHardwareMonitorLib.dll").read_bytes()
        if b".NETFramework" not in dll_bytes:
            runtime = "coreclr"
    except Exception:
        pass

    try:
        # pythonnet 3.x requires selecting the runtime before `import clr`.
        try:
            import pythonnet as _pn
            try:
                _pn.load(runtime)
            except Exception:
                _pn.load("coreclr" if runtime == "netfx" else "netfx")
        except (ImportError, AttributeError):
            pass  # pythonnet 2.x -- no load() needed
    except ImportError:
        _bootstrap_error = "pythonnet is not installed (see requirements.txt)"
        return

    try:
        import clr  # noqa: F401  (pip install "pythonnet>=3.0.0")
        from System.Reflection import Assembly as _Asm

        # Load every support assembly by full path first, then the main lib -- clr.AddReference silently
        # fails to find LibreHardwareMonitorLib's dependencies unless each is already loaded this way.
        for dll in sorted(lib_dir.glob("*.dll")):
            if dll.stem == "LibreHardwareMonitorLib":
                continue
            try:
                _Asm.LoadFrom(str(dll))
            except Exception:
                pass
        _Asm.LoadFrom(str(lib_dir / "LibreHardwareMonitorLib.dll"))

        from LibreHardwareMonitor.Hardware import Computer

        _Computer = Computer
        _lhm_available = True
        _bootstrap_error = None
    except Exception as exc:
        _bootstrap_error = f"Failed to load LibreHardwareMonitorLib: {exc}"


_bootstrap()


def lhm_available() -> bool:
    return _lhm_available


# ---------------------------------------------------------------------------
# Public snapshot shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatsSnapshot:
    """Immutable per-tick result handed out by `StatsPoller.get_snapshot()`. All numeric fields are Optional,
    populated only when the corresponding sensor was found this tick. `available`/`error` describe the
    CPU/GPU/RAM (LHM) side only; `fps`/`fps_error` are independent, since PresentMon and LHM fail separately.
    `fps_1pct_low`/`fps_0_1pct_low`/`fps_frame_time_history` warm up later than `fps` since they need more
    samples; `fps_frame_time_history` is raw msBetweenPresents, not fps."""

    available: bool = False
    error: Optional[str] = None

    cpu_pct: Optional[float] = None
    cpu_temp: Optional[float] = None

    gpu_pct: Optional[float] = None
    gpu_temp: Optional[float] = None
    gpu_vram_used_gb: Optional[float] = None
    gpu_vram_total_gb: Optional[float] = None

    ram_used_gb: Optional[float] = None
    ram_total_gb: Optional[float] = None

    fps: Optional[float] = None
    fps_error: Optional[str] = None
    fps_1pct_low: Optional[float] = None
    fps_0_1pct_low: Optional[float] = None
    fps_frame_time_history: tuple = ()


_UNAVAILABLE_SNAPSHOT_ERROR = "LibreHardwareMonitor is unavailable"


class StatsPoller:
    """Background daemon thread that polls hardware stats + FPS every `poll_interval_sec`, publishing a
    fresh `StatsSnapshot` each tick. Safe to construct even when `lhm_available()` is False -- `start()`
    then just publishes a permanent `available=False` snapshot without spinning up a thread."""

    def __init__(self, poll_interval_sec: float = 1.0, track_fps: bool = True) -> None:
        self._poll_interval_sec = max(0.2, float(poll_interval_sec))
        self._track_fps = bool(track_fps)

        self._lock = threading.Lock()
        self._snapshot = StatsSnapshot()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # FPS/PresentMon state -- owned exclusively by the poll thread, so no extra locking needed here.
        self._fps_tracker: Optional[_FpsTracker] = None
        self._fps_target_pid: Optional[int] = None
        self._fps_pending_pid: Optional[int] = None
        self._fps_pending_since: float = 0.0
        self._fps_missing_error: Optional[str] = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        if not _lhm_available:
            with self._lock:
                self._snapshot = StatsSnapshot(
                    available=False,
                    error=_bootstrap_error or _UNAVAILABLE_SNAPSHOT_ERROR,
                )
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, name="SGO-StatsPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        if self._fps_tracker is not None:
            self._fps_tracker.stop()
            self._fps_tracker = None
            self._fps_target_pid = None
            self._fps_pending_pid = None

    def set_track_fps(self, enabled: bool) -> None:
        """Runtime on/off switch for FPS tracking -- for when a future
        settings toggle (e.g. StatsHudState.show_fps) needs to flip this
        without tearing down/recreating the whole poller."""
        self._track_fps = bool(enabled)

    def get_snapshot(self) -> StatsSnapshot:
        """Thread-safe, cheap -- never blocks on I/O. Safe to call every
        frame from a render loop."""
        with self._lock:
            return self._snapshot

    # -- poll thread ---------------------------------------------------------

    def _poll_loop(self) -> None:
        try:
            comp = _Computer()
            # Ring0 would extract/install LHM's bundled WinRing0 kernel driver -- stays disabled, trading
            # away a few raw-MSR/PCI sensors to keep the no-kernel-driver rule. Do not flip this to True.
            comp.IsRing0Enabled = False
            comp.IsCpuEnabled = True
            comp.IsGpuEnabled = True
            comp.IsMemoryEnabled = True
            comp.Open()
        except Exception as exc:
            with self._lock:
                self._snapshot = StatsSnapshot(
                    available=False, error=f"LibreHardwareMonitor failed to open: {exc}")
            self._running = False
            return

        try:
            while self._running:
                data: dict = {}
                try:
                    for hw in comp.Hardware:
                        hw.Update()
                        self._harvest(hw, data)
                    if "ram_used_gb" in data and "ram_available_gb" in data:
                        data["ram_total_gb"] = data["ram_used_gb"] + data.pop("ram_available_gb")
                    else:
                        data.pop("ram_available_gb", None)
                except Exception:
                    pass

                self._update_fps(data)

                snapshot = StatsSnapshot(
                    available=True,
                    error=None,
                    cpu_pct=data.get("cpu_pct"),
                    cpu_temp=data.get("cpu_temp"),
                    gpu_pct=data.get("gpu_pct"),
                    gpu_temp=data.get("gpu_temp"),
                    gpu_vram_used_gb=data.get("gpu_vram_used_gb"),
                    gpu_vram_total_gb=data.get("gpu_vram_total_gb"),
                    ram_used_gb=data.get("ram_used_gb"),
                    ram_total_gb=data.get("ram_total_gb"),
                    fps=data.get("fps"),
                    fps_error=data.get("fps_error"),
                    fps_1pct_low=data.get("fps_1pct_low"),
                    fps_0_1pct_low=data.get("fps_0_1pct_low"),
                    fps_frame_time_history=data.get("fps_frame_time_history", ()),
                )
                with self._lock:
                    self._snapshot = snapshot

                time.sleep(self._poll_interval_sec)
        finally:
            if self._fps_tracker is not None:
                self._fps_tracker.stop()
                self._fps_tracker = None
            try:
                comp.Close()
            except Exception:
                pass

    def _update_fps(self, data: dict) -> None:
        """Track FPS of whichever window currently has real OS focus; deliberately ignores the window-select target filter. Owned entirely by the poll thread; no locking needed here."""
        if not self._track_fps:
            if self._fps_tracker is not None:
                self._fps_tracker.stop()
                self._fps_tracker = None
                self._fps_target_pid = None
                self._fps_pending_pid = None
            return

        cur_pid = window_select.foreground_pid() or None
        if cur_pid is not None and cur_pid != self._fps_target_pid:
            if cur_pid == self._fps_pending_pid:
                if time.monotonic() - self._fps_pending_since >= _PID_DEBOUNCE_SEC:
                    self._retarget_fps(cur_pid)
                    self._fps_pending_pid = None
            else:
                self._fps_pending_pid = cur_pid
                self._fps_pending_since = time.monotonic()
        elif cur_pid == self._fps_target_pid:
            self._fps_pending_pid = None

        fps_error = None
        if self._fps_tracker is not None:
            fps_val = self._fps_tracker.get_fps()
            if fps_val is not None:
                data["fps"] = fps_val
            lows = self._fps_tracker.get_percentile_lows()
            if lows is not None:
                data["fps_1pct_low"], data["fps_0_1pct_low"] = lows
            data["fps_frame_time_history"] = self._fps_tracker.get_frame_time_history()
            fps_error = self._fps_tracker.last_error
        elif self._fps_missing_error is not None:
            fps_error = self._fps_missing_error
        data["fps_error"] = fps_error

    def _retarget_fps(self, pid: int) -> None:
        exe_path = _presentmon_path()
        if not exe_path.exists():
            _warn_presentmon_missing_once(exe_path)
            self._fps_missing_error = f"PresentMon.exe not found at {exe_path}"
            # Remember this pid as "targeted" anyway so we don't re-check the missing binary every poll tick.
            self._fps_target_pid = pid
            return
        self._fps_missing_error = None
        if self._fps_tracker is None:
            self._fps_tracker = _FpsTracker()
        self._fps_tracker.start(pid, exe_path)
        self._fps_target_pid = pid

    # -- sensor harvesting ---------------------------------------------------

    @staticmethod
    def _harvest(hw, data: dict) -> None:
        hw_type = str(hw.HardwareType)

        if "Cpu" in hw_type:
            loads, temps = [], []
            for s in hw.Sensors:
                v = s.Value
                if v is None:
                    continue
                st = str(s.SensorType)
                if "Load" in st:
                    loads.append((s.Name, float(v)))
                elif "Temperature" in st and float(v) > 0.0:
                    # Skip 0.0 -- LHM uses it as an unreadable-sensor sentinel on some AMD Ryzen CPUs.
                    temps.append((s.Name, float(v)))
            # Prefer "Total" load; fall back to first sensor.
            cpu_load = next(
                (v for n, v in loads if "Total" in n),
                loads[0][1] if loads else None)
            # Priority: Package (Intel) -> Tctl/Tdie (AMD) -> Average -> first.
            # Explicit None checks -- `or` would treat a valid 0.x value as falsy.
            temp_preds = [
                lambda n: "Package" in n,
                lambda n: "Tctl" in n or "Tdie" in n,
                lambda n: "Average" in n,
            ]
            cpu_temp = None
            for pred in temp_preds:
                match = next((v for n, v in temps if pred(n)), None)
                if match is not None:
                    cpu_temp = match
                    break
            if cpu_temp is None and temps:
                cpu_temp = temps[0][1]
            if cpu_load is not None and "cpu_pct" not in data:
                data["cpu_pct"] = cpu_load
            if cpu_temp is not None and "cpu_temp" not in data:
                data["cpu_temp"] = cpu_temp

        elif "Gpu" in hw_type:
            loads, temps = [], []
            vram_used_mb, vram_total_mb = [], []
            vram_used_gb, vram_total_gb = [], []
            for s in hw.Sensors:
                v = s.Value
                if v is None:
                    continue
                st = str(s.SensorType)
                name = s.Name
                if "Load" in st:
                    loads.append((name, float(v)))
                elif "Temperature" in st:
                    # Some AMD GPUs expose no Temperature sensor at all -- gpu_temp just stays None below.
                    temps.append((name, float(v)))
                elif "SmallData" in st:            # MB (VRAM)
                    if "Memory" in name and "Used" in name:
                        vram_used_mb.append(float(v))
                    elif "Memory" in name and "Total" in name:
                        vram_total_mb.append(float(v))
                elif "Data" in st:                  # GB (VRAM)
                    if "Memory" in name and "Used" in name:
                        vram_used_gb.append(float(v))
                    elif "Memory" in name and "Total" in name:
                        vram_total_gb.append(float(v))

            gpu_load = next((v for n, v in loads if "Core" in n),
                             loads[0][1] if loads else None)
            gpu_temp = next((v for n, v in temps if "Core" in n),
                             temps[0][1] if temps else None)
            if gpu_load is not None and "gpu_pct" not in data:
                data["gpu_pct"] = gpu_load
            if gpu_temp is not None and "gpu_temp" not in data:
                data["gpu_temp"] = gpu_temp

            # Prefer GB sensors (Data type); fall back to MB/1024.
            vram_u = (vram_used_gb[0] if vram_used_gb
                      else vram_used_mb[0] / 1024 if vram_used_mb else None)
            vram_t = (vram_total_gb[0] if vram_total_gb
                      else vram_total_mb[0] / 1024 if vram_total_mb else None)
            if vram_u is not None and "gpu_vram_used_gb" not in data:
                data["gpu_vram_used_gb"] = vram_u
            if vram_t is not None and "gpu_vram_total_gb" not in data:
                data["gpu_vram_total_gb"] = vram_t

        elif "Memory" in hw_type:
            if "Virtual" in str(hw.Name):
                return  # skip virtual memory (RAM + pagefile); physical only
            for s in hw.Sensors:
                v = s.Value
                if v is None:
                    continue
                st = str(s.SensorType)
                name = s.Name
                if "Data" in st:
                    if "Used" in name and "ram_used_gb" not in data:
                        data["ram_used_gb"] = float(v)
                    elif ("Available" in name or "Free" in name) \
                            and "ram_available_gb" not in data:
                        data["ram_available_gb"] = float(v)
