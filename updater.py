"""updater.py -- self-update for Shattered Gaming Overlay against GitHub
Releases: Check -> Download -> Install flow. Downloads the new release, then
re-runs the installer silently in the background and lets the app close
itself so the install can complete. Stdlib only (urllib, zipfile, json).

`repo_configured()` lets callers (panels/settings.py) tell "no repo
configured" apart from "configured, just no releases yet" -- only the
former should suppress the "View releases" link.

`UpdateManager` hands background-thread results back to the render loop via
a lock-guarded snapshot (`sync_to()`, called from main.py's `_show_gui`),
the same pattern remapper.py/macro_engine.py/hud_overlay.py use for their
own `update_snapshot()`.
"""

from __future__ import annotations

import ctypes
import io
import json
import logging
import os
import sys
import tempfile
import threading
import zipfile
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
import urllib.request

# Guarded import -- keeps this module importable without pulling in
# imgui_bundle; only needed here for a type hint.
try:  # pragma: no cover - only used for type hints
    from app_state import SettingsState, UpdateStatus
except Exception:  # pragma: no cover
    SettingsState = object  # type: ignore
    UpdateStatus = None  # type: ignore

_log = logging.getLogger("shattered_overlay.updater")

# Placeholder repo/asset configuration -- see module docstring.
_APP_REPO = "VenusFallen/Shattered-Gaming-Overlay"
_API = "https://api.github.com/repos/{repo}/releases/latest"

# TODO(release): confirm/adjust once the real installer + release pipeline
# produces its actual asset names.
_ASSET_ZIP_PREFIX = "ShatteredGamingOverlay_v"
_INSTALLER_EXE_NAME = "ShatteredGamingOverlay_Setup.exe"


def repo_configured() -> bool:
    """True once `_APP_REPO` has been pointed at a real GitHub repo. Lets
    panels/settings.py avoid presenting a "View releases on GitHub" link
    that would 404, without hardcoding the placeholder check in the UI."""
    return "/" in _APP_REPO and not _APP_REPO.startswith("REPLACE_ME")


def releases_url() -> str:
    return f"https://github.com/{_APP_REPO}/releases"


def _fetch_release(repo: str) -> dict:
    """Return the latest release JSON from GitHub. Raises on network error."""
    url = _API.format(repo=repo)
    req = urllib.request.Request(url, headers={"User-Agent": "ShatteredGamingOverlay-Updater"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _download_url(url: str, progress_cb=None) -> bytes:
    """Download a URL to memory. progress_cb(pct: int) is optional."""
    req = urllib.request.Request(url, headers={"User-Agent": "ShatteredGamingOverlay-Updater"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        buf = io.BytesIO()
        downloaded = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            downloaded += len(chunk)
            if progress_cb and total:
                progress_cb(int(downloaded * 100 / total))
    if progress_cb:
        progress_cb(100)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shattered Gaming Overlay self-update
# ---------------------------------------------------------------------------


def check_app_update(current_version: str) -> tuple[bool, str]:
    """Returns (update_available, latest_version_str). Raises on network
    error -- including an unconfigured repo, which callers should surface as
    a normal check-failed error."""
    data = _fetch_release(_APP_REPO)
    latest = data.get("tag_name", "").lstrip("v")
    cur = current_version.lstrip("v")
    return (bool(latest) and latest != cur), latest


def _find_zip_asset_url(data: dict) -> str:
    """Pick the release zip asset: match extension + expected name prefix
    first (avoids grabbing an unrelated asset), fall back to any .zip."""
    assets = data.get("assets", [])
    prefix = _ASSET_ZIP_PREFIX.lower()
    for a in assets:
        name = a.get("name", "")
        if name.lower().endswith(".zip") and name.lower().startswith(prefix):
            return a["browser_download_url"]
    for a in assets:
        if a.get("name", "").lower().endswith(".zip"):
            return a["browser_download_url"]
    raise RuntimeError(f"No {_ASSET_ZIP_PREFIX}*.zip asset found in the latest release")


def download_app(progress_cb=None) -> Path:
    """Download the latest release zip and extract the installer exe into a
    fresh temp directory. Only works in a frozen (PyInstaller) build --
    raises otherwise. Returns the extracted installer path; nothing is
    executed here, call launch_installer_and_quit() with it separately."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Self-update only works in the packaged exe")

    data = _fetch_release(_APP_REPO)
    asset_url = _find_zip_asset_url(data)

    payload = _download_url(asset_url, progress_cb)

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        installer_member = next(
            (n for n in zf.namelist() if Path(n).name.lower() == _INSTALLER_EXE_NAME.lower()),
            None,
        )
        if installer_member is None:
            raise RuntimeError(f"{_INSTALLER_EXE_NAME} not found inside the downloaded release zip")

        extract_dir = Path(tempfile.mkdtemp(prefix="sgo_update_"))
        extracted_path = extract_dir / _INSTALLER_EXE_NAME
        with zf.open(installer_member) as src, open(extracted_path, "wb") as dst:
            dst.write(src.read())

    return extracted_path


def _quote_ps_single(value: str) -> str:
    """Wrap `value` in single quotes for a PowerShell -Command string,
    doubling embedded single quotes per PowerShell's escape rule."""
    return "'" + str(value).replace("'", "''") + "'"


def _build_relaunch_command(pid: int, installer_path: Path, install_args: list[str]) -> str:
    """Build the PowerShell -Command string that waits for process `pid` to
    fully exit, then starts `installer_path` with `install_args`. Pure
    function (no subprocess spawning) so it's unit-testable directly."""
    arg_list = ", ".join(_quote_ps_single(a) for a in install_args)
    return (
        f"Wait-Process -Id {pid} -Timeout 10 -ErrorAction SilentlyContinue; "
        f"Start-Process -FilePath {_quote_ps_single(str(installer_path))} "
        f"-ArgumentList @({arg_list}) -WindowStyle Hidden"
    )


def _quote_cmdline_arg(value: str) -> str:
    """Quote `value` as a single Win32 command-line argument per the
    `CommandLineToArgvW` parsing rules (what `ShellExecuteExW`'s
    `lpParameters` is parsed with) -- a different layer than
    `_quote_ps_single`'s PowerShell quoting above.

    A run of N backslashes before a literal `"` becomes 2N+1 backslashes
    then `"`; a run at the very end of the argument becomes 2N backslashes.
    Same algorithm as `subprocess.list2cmdline`, reproduced here since that
    one is only reachable via subprocess's argv-list APIs, not a single
    `lpParameters` string.
    """
    if value and not any(c in value for c in ' \t\n\v"'):
        return value
    out = ['"']
    n_backslashes = 0
    for ch in value:
        if ch == "\\":
            n_backslashes += 1
        elif ch == '"':
            out.append("\\" * (n_backslashes * 2 + 1))
            out.append('"')
            n_backslashes = 0
        else:
            out.append("\\" * n_backslashes)
            out.append(ch)
            n_backslashes = 0
    out.append("\\" * (n_backslashes * 2))
    out.append('"')
    return "".join(out)


def _build_shell_execute_parameters(ps_command: str) -> str:
    """Build the `lpParameters` string `ShellExecuteExW` expects for
    launching `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy
    Bypass -WindowStyle Hidden -Command <ps_command>`.

    `lpParameters` is one raw command-line string parsed by
    `CommandLineToArgvW`, not an argv list -- each argument (including
    `ps_command`, which already carries its own inner PowerShell quoting) is
    quoted independently via `_quote_cmdline_arg` so this outer layer can't
    corrupt what `_build_relaunch_command` already produced.
    """
    args = [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-Command", ps_command,
    ]
    return " ".join(_quote_cmdline_arg(a) for a in args)


# ShellExecuteExW ("runas") bindings -- see launch_installer_and_quit()'s
# docstring for why elevation is triggered here rather than via a
# subprocess.Popen'd watcher. Raw ctypes, no pywin32, matching this
# project's convention elsewhere (tray_icon.py, titlebar.py, window_select.py).

_SW_HIDE = 0

if sys.platform == "win32":
    _shell32_ex = ctypes.WinDLL("shell32", use_last_error=True)

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            # Union with hMonitor in the real struct; this call never uses
            # the icon/monitor variant, so a plain HANDLE field is enough.
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    _shell32_ex.ShellExecuteExW.argtypes = (ctypes.POINTER(_SHELLEXECUTEINFOW),)
    _shell32_ex.ShellExecuteExW.restype = wintypes.BOOL


def _shell_execute_runas(lpFile: str, lpParameters: str) -> None:
    """Launch `lpFile` elevated (UAC "runas" verb) via `ShellExecuteExW`,
    called from this still-alive, foreground, interactive process. Raises
    OSError on failure."""
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = 0
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = lpFile
    info.lpParameters = lpParameters
    info.lpDirectory = None
    info.nShow = _SW_HIDE
    info.hInstApp = None
    info.lpIDList = None
    info.lpClass = None
    info.hkeyClass = None
    info.dwHotKey = 0
    info.hIcon = None
    info.hProcess = None

    ok = _shell32_ex.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(f"ShellExecuteExW(runas) failed: WinError {err}")


def _log_dir() -> Path:
    """%LOCALAPPDATA%\\Shattered Gaming Overlay\\logs."""
    base = os.getenv("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "Shattered Gaming Overlay" / "logs"


def launch_installer_and_quit(installer_path: Path) -> None:
    """Hand off to the extracted installer, then return so the caller can
    quit the app.

    Does NOT launch the installer directly. Elevates and starts a detached
    PowerShell watcher that runs `Wait-Process -Id <this pid>` (10s
    safety-net timeout) and only starts the installer once this process has
    actually, fully terminated -- not merely been asked to quit. Without
    this, the installer's file-copy step can race this process's own
    exe/dll file lock releasing; Inno Setup's own `AppMutex` check can abort
    within milliseconds of launch, faster than a Restart-Manager-mediated
    close-and-wait can complete. Guaranteeing real process death before the
    installer starts removes the race entirely.

    The watcher is launched via `ShellExecuteExW("runas", ...)` rather than
    `subprocess.Popen` because the installed app requires admin elevation
    (PresentMon's FPS tracking needs an elevated ETW session), and a
    non-elevated, detached PowerShell watcher trying to `Start-Process` an
    admin-manifested installer fails silently -- confirmed live: the UAC
    `consent.exe` process appears but the prompt never reaches the
    interactive desktop. `ShellExecuteExW`'s `runas` verb, called while this
    process is still alive and owns a foreground GUI window, is the same
    mechanism Explorer's "Run as administrator" uses and reliably surfaces
    the UAC dialog. Once approved, the resulting PowerShell process is
    already elevated, so its later `Start-Process` on the installer inherits
    that with no second prompt.

    The caller must still make the app quit immediately after this returns
    (e.g. `hi.get_runner_params().app_shall_exit = True`) -- that's what the
    watcher is waiting on.

    Install flags (Inno Setup silent-install switches):
      /VERYSILENT       - no wizard UI
      /SUPPRESSMSGBOXES - suppress informational message boxes
      /NORESTART        - never prompt for or force a reboot
      /LOG=<path>       - write an install log to
                          %LOCALAPPDATA%\\Shattered Gaming Overlay\\logs

    The watcher runs hidden and detached -- only the OS-owned UAC dialog is
    ever shown; the installer it launches inherits that same
    detachment/elevation.
    """
    if sys.platform != "win32":
        raise RuntimeError("Installer handoff is only supported on Windows")

    installer_path = Path(installer_path)
    if not installer_path.is_file():
        raise RuntimeError(f"Installer not found at {installer_path}")

    my_pid = os.getpid()

    log_path = None
    try:
        log_dir = _log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"update_install_{timestamp}.log"
    except Exception:
        # Best-effort only -- a log path we couldn't prepare must never
        # block the actual update install from proceeding.
        log_path = None

    install_args = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
    if log_path is not None:
        install_args.append(f"/LOG={log_path}")

    ps_command = _build_relaunch_command(my_pid, installer_path, install_args)
    lp_parameters = _build_shell_execute_parameters(ps_command)

    _shell_execute_runas("powershell.exe", lp_parameters)


# UpdateManager -- stateful, thread-safe orchestration. Mirrors
# remapper.py/hud_overlay.py's "singleton owns background work behind a
# lock, render thread only calls documented thread-safe methods" pattern.


@dataclass
class _Status:
    status: "UpdateStatus"
    latest_version: str
    download_pct: int
    error_message: str
    last_checked_display: str
    auto_prompt_pending: bool


class UpdateManager:
    """Owns the Companion window's self-update background work.

    Public API (safe to call from the render thread):
        start_check(current_version, is_automatic=False)
        start_download()
        install_and_quit() -> bool
        dismiss_auto_prompt()
        sync_to(settings)

    The actual network calls run on short-lived daemon threads spawned
    here -- never call check_app_update()/download_app() directly from the
    render thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: "UpdateStatus" = UpdateStatus.IDLE if UpdateStatus is not None else None
        self._latest_version = ""
        self._download_pct = 0
        self._error_message = ""
        self._last_checked_display = "Never checked"
        self._installer_path: Optional[Path] = None
        self._auto_prompt_pending = False
        # Guards against a double-click (or the auto-check racing a manual
        # click) spawning two overlapping worker threads.
        self._busy = False

    # ------------------------------------------------------------------
    # Commands (spawn background threads; return immediately)
    # ------------------------------------------------------------------

    def start_check(self, current_version: str, is_automatic: bool = False) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
            self._status = UpdateStatus.CHECKING
            self._error_message = ""
        threading.Thread(
            target=self._do_check, args=(current_version, is_automatic), daemon=True, name="UpdaterCheck"
        ).start()

    def _do_check(self, current_version: str, is_automatic: bool) -> None:
        try:
            avail, latest = check_app_update(current_version)
        except Exception:
            _log.exception("Update check failed")
            with self._lock:
                self._status = UpdateStatus.ERROR
                self._error_message = "Check failed -- see log"
                self._busy = False
            return
        with self._lock:
            self._last_checked_display = f"Last checked: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            if avail:
                self._status = UpdateStatus.AVAILABLE
                self._latest_version = latest
                if is_automatic:
                    self._auto_prompt_pending = True
            else:
                self._status = UpdateStatus.UP_TO_DATE
            self._busy = False

    def start_download(self) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
            self._status = UpdateStatus.DOWNLOADING
            self._download_pct = 0
            self._error_message = ""
        threading.Thread(target=self._do_download, daemon=True, name="UpdaterDownload").start()

    def _do_download(self) -> None:
        if not getattr(sys, "frozen", False):
            # Dev build -- a real, expected state, not an error.
            with self._lock:
                self._status = UpdateStatus.ERROR
                self._error_message = "Update install only works in a packaged build, not from source"
                self._busy = False
            return
        try:
            def progress(pct: int) -> None:
                with self._lock:
                    self._download_pct = pct

            path = download_app(progress)
        except Exception:
            _log.exception("Update download failed")
            with self._lock:
                self._status = UpdateStatus.ERROR
                self._error_message = "Download failed -- see log"
                self._busy = False
            return
        with self._lock:
            self._installer_path = path
            self._status = UpdateStatus.READY
            self._busy = False

    def install_and_quit(self) -> bool:
        """Hands off to the extracted installer. Returns True if the caller
        should now quit the app -- the caller is expected to immediately
        follow a True return with `hi.get_runner_params().app_shall_exit =
        True` so normal before_exit teardown still runs. Returns False
        (recording an error) if the handoff itself fails."""
        with self._lock:
            path = self._installer_path
        if path is None:
            with self._lock:
                self._status = UpdateStatus.ERROR
                self._error_message = "No installer downloaded yet"
            return False
        try:
            launch_installer_and_quit(path)
        except Exception:
            _log.exception("Installer handoff failed")
            with self._lock:
                self._status = UpdateStatus.ERROR
                self._error_message = "Install handoff failed -- see log"
            return False
        with self._lock:
            self._status = UpdateStatus.INSTALLING
        return True

    def dismiss_auto_prompt(self) -> None:
        """'Later' on the automatic check-on-launch prompt -- a session-only
        skip; never touches SettingsState.check_for_updates_on_launch."""
        with self._lock:
            self._auto_prompt_pending = False

    # ------------------------------------------------------------------
    # Frame sync (read-direction lock-guarded snapshot)
    # ------------------------------------------------------------------

    def sync_to(self, settings: "SettingsState") -> None:
        """Call once per Companion-window frame, before render. Opposite
        direction from remapper_engine.update_snapshot() (copies the
        engine's result out into AppState instead of in) but the same
        lock-guarded-snapshot pattern. Never mutate AppState.settings'
        update_* fields from anywhere else."""
        with self._lock:
            settings.update_status = self._status
            settings.update_latest_version = self._latest_version
            settings.update_download_pct = self._download_pct
            settings.update_error_message = self._error_message
            settings.last_checked_display = self._last_checked_display
            settings.auto_update_prompt_pending = self._auto_prompt_pending


# Process-wide singleton -- main.py drives this alongside the Companion
# window's lifecycle, same pattern as remapper_engine/macro_engine/hud_overlay.
update_manager = UpdateManager()
