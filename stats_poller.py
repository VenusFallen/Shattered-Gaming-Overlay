"""stats_poller.py -- background hardware-stats + FPS polling, feeding the
future Stats HUD (CPU/GPU usage & temp, VRAM, RAM, FPS).

Ported from R9Tools' stats_poller.py (this project's predecessor, at
D:\\Projects\\Python\\Testing\\R9Tools\\stats_poller.py), which already solved
this exact problem. The LibreHardwareMonitor bootstrap quirks, the AMD Ryzen
0.0-temp sentinel filter, the PresentMon subprocess/CTRL_BREAK_EVENT teardown
dance, and the sensor-harvesting priority rules below are all carried over
from there -- adapted, not re-derived. See "What's different from R9Tools"
below for the actual deltas.

Hard-rule compliance
-----------------------------------------------------------
- LibreHardwareMonitorLib (bundled unmodified in lib/, MPL 2.0 --
  lib/LICENSE-LibreHardwareMonitor.txt) is opened with
  `Computer.IsRing0Enabled = False`. This is the setting that keeps this
  module from ever installing LHM's bundled WinRing0 kernel driver -- LHM
  *can* install a kernel driver for raw MSR/PCI sensor access, but only if
  Ring0 is left enabled. Leaving it False trades away a handful of sensors
  (some CPU temps on some boards) for staying inside the project's absolute
  "no kernel-mode driver" rule. Do not flip this to True.
- PresentMon (bundled unmodified in presentmon/, MIT --
  presentmon/LICENSE-PresentMon.txt) reads frame-present timing passively via
  Windows' own ETW tracing (`--process_id <pid>`). It performs no DLL
  injection and no writes into the target game process -- it's a read-only
  external observer, same as the input hooks are.
- No target-process memory is ever read or written by this module, LHM, or
  PresentMon. This module never touches input (no SendInput, no hooks) at
  all -- it is pure sensor/telemetry polling.

What's different from R9Tools
------------------------------
- Snapshot shape: R9Tools' StatsPoller().latest was a bare dict with a
  bespoke key set (cpu_usage, gpu_usage, gpu_vram_used, ...) fed to a
  registered callback. Here it's a frozen `StatsSnapshot` dataclass
  (`cpu_pct`, `gpu_pct`, `gpu_vram_used_gb`, ... -- exact field names on the
  class below), handed out via `get_snapshot()`, matching this project's
  lock-guarded-snapshot pull pattern (see hud_overlay.py's
  `update_crosshair`/render-thread split, macro_engine.py's
  `update_snapshot`).
- No `settings` dict dependency. R9Tools drove `update_rate_hz` and
  `show_fps` off a shared settings dict that doesn't exist on this side of
  the port (this module is explicitly poller-only -- a HUD-rendering
  follow-up will design how a caller wires real settings in).
  `StatsPoller.__init__` takes plain constructor args instead
  (`poll_interval_sec`, `track_fps`), with `set_track_fps()` for runtime
  toggling once a settings toggle exists to drive it.
- Foreground-window lookup for FPS targeting reuses
  `window_select.foreground_pid()` instead of R9Tools' own local
  win32gui/win32process calls -- this project already has a correct, tested
  implementation of that, no need for a second one. Like R9Tools, FPS
  tracking always follows the real OS foreground window and is deliberately
  NOT gated by the window-select process filter (that filter
  only gates Remapper/Macro engine matching/injection, never this read-only
  stats/FPS observer -- same reasoning as the overlay-visibility
  carve-out for the HUD).
- This module is not wired into main.py/hud_overlay.py/app_state.py yet --
  intentionally. That will happen in a follow-up HUD-rendering pass, designed
  together with how the renderer actually wants to consume `get_snapshot()`.

Threading
---------
`StatsPoller.start()`/`.stop()` run/tear down a single background daemon
thread (`_poll_loop`), the same start/stop shape as `HookManager`/
`hud_overlay`'s render thread. Each poll tick builds a brand-new
`StatsSnapshot` and publishes it under `self._lock`; callers pull the latest
one via `get_snapshot()` (thread-safe, cheap, never blocks on I/O). Snapshots
are never mutated in place -- each tick replaces the whole object, so a
caller holding a reference to one `StatsSnapshot` never sees it change out
from under it.

Graceful degradation
---------------------
If `pythonnet` isn't installed, or the bundled LibreHardwareMonitorLib.dll
can't be found/loaded, `lhm_available()` returns False and `start()` is a
no-op that immediately publishes a snapshot with `available=False` and
`error` set to a human-readable reason -- this module never raises out of
`start()`/`get_snapshot()` over a missing dependency. Same idea for
PresentMon: if presentmon/PresentMon.exe is missing or fails to launch,
`fps` stays None and `fps_error` explains why, without affecting the
CPU/GPU/RAM side of the snapshot at all -- these are two independent
subsystems and either can fail without disabling the other.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import window_select

_log = logging.getLogger("shattered_overlay.stats")

_lhm_available = False
_Computer = None
_bootstrap_error: Optional[str] = None

# ---------------------------------------------------------------------------
# PresentMon / FPS tracking
# ---------------------------------------------------------------------------
_FPS_ROLLING_WINDOW = 30    # frames averaged for the smoothed FPS value
_FPS_STALE_SEC = 2.0        # no fresh sample in this long -> report no FPS
_PID_DEBOUNCE_SEC = 0.75    # foreground pid must be stable this long before retargeting

_pm_missing_warned = False       # log "binary not found" only once per session
_pm_launch_failed_warned = False  # log "failed to launch" only once per session


def _presentmon_path() -> Path:
    """Resolve presentmon/PresentMon.exe using the same dev-mode vs. frozen
    (sys._MEIPASS) resolution pattern `_bootstrap()` uses for lib/, and the
    same `getattr(sys, "frozen", False)` check updater.py already uses
    elsewhere in this project."""
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


class _FpsTracker:
    """Owns one PresentMon subprocess targeting a single PID at a time.

    Reads its live stdout CSV stream on a background thread, keeps a rolling
    window of msBetweenPresents samples, and exposes a smoothed FPS value.
    Fully self-contained: caller just calls start(pid, exe)/stop()/get_fps().
    Ported near-verbatim from R9Tools' `_FpsTracker`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._samples: deque = deque(maxlen=_FPS_ROLLING_WINDOW)
        self._last_sample_ts = 0.0
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
            self._last_sample_ts = 0.0
        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name="SGO-PresentMonReader")
        self._reader_thread.start()

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                # Prefer a graceful CTRL_BREAK_EVENT over terminate()/kill():
                # PresentMon's console control handler stops its ETW trace
                # session cleanly on break, whereas TerminateProcess skips
                # that handler and can leave the session orphaned (breaking
                # the next launch until --stop_existing_session cleans it up).
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

    def get_fps(self) -> Optional[float]:
        with self._lock:
            if not self._samples:
                return None
            if time.monotonic() - self._last_sample_ts > _FPS_STALE_SEC:
                return None
            avg_ms = sum(self._samples) / len(self._samples)
        if avg_ms <= 0:
            return None
        return 1000.0 / avg_ms

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
                    self._samples.append(ms)
                    self._last_sample_ts = time.monotonic()
        except Exception:
            # Normal on terminate() (pipe closed mid-read) -- not worth logging.
            pass


# ---------------------------------------------------------------------------
# LibreHardwareMonitor bootstrap
# ---------------------------------------------------------------------------


def _lib_dir() -> Path:
    """Resolve lib/ using the same dev-mode vs. frozen resolution pattern as
    `_presentmon_path()` -- persistent lib/ next to the exe wins if present
    (allows dropping in newer DLLs without rebuilding), else the bundled
    sys._MEIPASS copy."""
    try:
        if getattr(sys, "frozen", False):
            persistent = Path(sys.executable).parent / "lib"
            bundled = Path(sys._MEIPASS) / "lib"  # type: ignore[attr-defined]
            return persistent if (persistent / "LibreHardwareMonitorLib.dll").exists() else bundled
    except Exception:
        pass
    return Path(__file__).parent / "lib"


def _bootstrap() -> None:
    """Load pythonnet + LibreHardwareMonitorLib.dll. Sets `_lhm_available`
    True only on full success; leaves `_bootstrap_error` set to a
    human-readable reason on any failure. Never raises -- this runs at
    import time."""
    global _lhm_available, _Computer, _bootstrap_error
    lib_dir = _lib_dir()

    if not (lib_dir / "LibreHardwareMonitorLib.dll").exists():
        _bootstrap_error = f"LibreHardwareMonitorLib.dll not found in {lib_dir}"
        return

    if str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

    # Remove Zone.Identifier ADS on all DLLs -- Windows blocks files
    # downloaded from the internet until unblocked. No-op if the stream
    # doesn't exist.
    try:
        import ctypes
        for dll in lib_dir.glob("*.dll"):
            ctypes.windll.kernel32.DeleteFileW(str(dll) + ":Zone.Identifier")
    except Exception:
        pass

    # Detect which .NET runtime the LHM DLL targets by scanning its binary.
    # net472 builds contain b'.NETFramework'; .NET 8/9/10 builds (LHM 0.9.x
    # targets .NET Standard 2.0) do not -- those need pythonnet's "coreclr"
    # runtime, never "netfx", or the load silently fails downstream.
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

        # Load every support assembly by full file path, then the main lib
        # last -- clr.AddReference silently fails to find LibreHardwareMonitorLib's
        # dependencies unless each one has already been loaded this way first.
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
    """Immutable per-tick result handed out by `StatsPoller.get_snapshot()`.

    All numeric fields are Optional -- a field is only populated when the
    corresponding sensor was actually found this tick (e.g. `gpu_temp` stays
    None on AMD GPUs that expose no Temperature sensor at all; `cpu_temp`
    skips a literal 0.0 reading, which LHM uses as an unreadable-sensor
    sentinel on some AMD Ryzen CPUs).

    `available`/`error` describe the CPU/GPU/RAM (LibreHardwareMonitor) side
    only. `fps`/`fps_error` are independent -- FPS (PresentMon) can be
    unavailable while CPU/GPU/RAM data is flowing fine, and vice versa.
    """

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


_UNAVAILABLE_SNAPSHOT_ERROR = "LibreHardwareMonitor is unavailable"


class StatsPoller:
    """Background daemon thread that polls hardware stats + FPS every
    `poll_interval_sec`, publishing a fresh `StatsSnapshot` each tick.

        poller = StatsPoller(poll_interval_sec=1.0, track_fps=True)
        poller.start()
        ...
        snap = poller.get_snapshot()
        ...
        poller.stop()

    Safe to construct even when `lhm_available()` is False -- `start()`
    simply publishes a permanent `available=False` snapshot and never spins
    up a thread in that case.
    """

    def __init__(self, poll_interval_sec: float = 1.0, track_fps: bool = True) -> None:
        self._poll_interval_sec = max(0.2, float(poll_interval_sec))
        self._track_fps = bool(track_fps)

        self._lock = threading.Lock()
        self._snapshot = StatsSnapshot()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # FPS/PresentMon tracking state -- owned exclusively by the poll
        # thread (created/torn down inside _poll_loop, never touched from
        # another thread), so no extra locking needed beyond self._lock for
        # publishing the resulting snapshot.
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
            # Ring0 extracts/installs LHM's bundled WinRing0 kernel driver,
            # which Microsoft's Vulnerable Driver Blocklist quarantines --
            # disabling it trades away sensors needing raw MSR/PCI access
            # (e.g. some CPU temps) for never creating that driver service.
            # See module docstring's hard-rule-compliance section -- do not
            # flip this to True.
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
        """Track FPS of whichever window currently has real OS focus, via
        `window_select.foreground_pid()`. Deliberately ignores any window
        target filter -- see module docstring. Owned entirely by the poll
        thread; no locking needed here."""
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
            fps_error = self._fps_tracker.last_error
        elif self._fps_missing_error is not None:
            fps_error = self._fps_missing_error
        data["fps_error"] = fps_error

    def _retarget_fps(self, pid: int) -> None:
        exe_path = _presentmon_path()
        if not exe_path.exists():
            _warn_presentmon_missing_once(exe_path)
            self._fps_missing_error = f"PresentMon.exe not found at {exe_path}"
            # Don't retry every debounce window once we know the binary is
            # missing -- just remember this pid as "targeted" (a no-op
            # tracker state) so we don't spam the missing-file check every
            # poll tick.
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
                    # Skip 0.0 -- LHM uses it as a sentinel when the sensor
                    # can't be read (e.g. AMD Ryzen's Core (Tctl/Tdie) on
                    # some LHM versions).
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
                    # Some AMD GPUs expose no Temperature sensor at all --
                    # only D3D usage. That's not an error: this loop simply
                    # never appends anything and gpu_temp stays None below.
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
