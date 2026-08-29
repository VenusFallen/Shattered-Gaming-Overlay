"""updater.py -- self-update for Shattered Gaming Overlay against GitHub
Releases.

Ported from R9Tools' `updater.py` (`D:\\Projects\\Python\\Testing\\R9Tools\\
updater.py`): Check -> Update -> Install flow, matching R9Tools' pattern --
download the new release, then re-run the installer silently in the
background and let the app close itself so the install can complete. The
network/download/installer-handoff functions below
(`_fetch_release`, `_download_url`, `check_app_update`, `download_app`,
`_quote_ps_single`, `_build_relaunch_command`, `launch_installer_and_quit`)
are a close port of R9Tools' own -- that logic took real, hard-won iteration
to get right there (see `launch_installer_and_quit`'s docstring below for
the exact race it fixes), so this is deliberately "port and adapt", not
"reinvent". Uses only the stdlib (urllib, zipfile, json) -- no extra
dependencies, same as R9Tools.

What's different from R9Tools here:
  - `_APP_REPO` now points at the real repo (`VenusFallen/Shattered-Gaming-
    Overlay`), but no release has been published to it yet -- `check_app_update`
    will correctly 404 until one exists. `repo_configured()` lets callers
    (panels/settings.py) tell "no repo configured" apart from "repo configured,
    just no releases yet"; only the former should suppress the "View releases"
    link, since the latter is a completely normal, real, temporary state.
  - `_ASSET_ZIP_PREFIX` / `_INSTALLER_EXE_NAME` below mirror R9Tools'
    `R9Tools_v<version>.zip` containing `R9Tools_Setup.exe` naming convention --
    confirm/adjust these once the real installer + release pipeline produces
    its actual asset names.
  - No Qt event loop / signals here (this app is Dear ImGui via
    `imgui_bundle`/Hello ImGui, rendered once per frame, not an event-driven
    Qt app) -- R9Tools hands background-thread results back to its UI via a
    Qt Signal (auto-queued onto the main thread by PySide6). This project's
    established equivalent, used by remapper.py/macro_engine.py/
    hud_overlay.py, is a lock-guarded snapshot copied across the thread
    boundary once per frame -- see `UpdateManager` below, whose `sync_to()`
    is called from main.py's `_show_gui` exactly where those other engines'
    own `update_snapshot()` calls already are.
  - No `crash_logging.py` module exists in this project (R9Tools' installer
    log path uses it) -- `_log_dir()` below picks a sensible
    %LOCALAPPDATA%-based location directly instead of depending on a module
    that doesn't exist here. If this project ever grows its own crash-log
    module, point this at it instead of duplicating the convention.
  - No AppMutex / Restart-Manager `CloseApplications=yes` wiring exists yet
    either (that lives in the .iss installer script, which is out of scope
    here) -- see `launch_installer_and_quit`'s docstring for what that means
    for this port's safety margin.
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

# Guarded import, mirroring remapper.py's own pattern -- keeps this module
# importable/unit-testable (e.g. from tests/ or a REPL) without pulling in
# imgui_bundle or any other UI dependency; only needed here for a type hint.
try:  # pragma: no cover - only used for type hints
    from app_state import SettingsState, UpdateStatus
except Exception:  # pragma: no cover
    SettingsState = object  # type: ignore
    UpdateStatus = None  # type: ignore

_log = logging.getLogger("shattered_overlay.updater")

# ---------------------------------------------------------------------------
# Placeholder repo/asset configuration -- see module docstring
# ---------------------------------------------------------------------------

# TODO(release): replace with the real GitHub "owner/repo" once a public
# repository + Releases actually exist for Shattered Gaming Overlay. Left as
# an obviously-fake placeholder on purpose rather than guessing at
# "VenusFallen/Shattered-Gaming-Overlay" or similar.
_APP_REPO = "VenusFallen/Shattered-Gaming-Overlay"
_API = "https://api.github.com/repos/{repo}/releases/latest"

# Placeholder asset-naming convention (no installer/ build exists yet --
# that's separate, future packaging work). Mirrors
# R9Tools' own "R9Tools_v<version>.zip containing R9Tools_Setup.exe"
# convention with this project's name substituted in. Confirm/adjust once a
# real installer + release pipeline exists.
_ASSET_ZIP_PREFIX = "ShatteredGamingOverlay_v"
_INSTALLER_EXE_NAME = "ShatteredGamingOverlay_Setup.exe"


def repo_configured() -> bool:
    """True once `_APP_REPO` has been pointed at a real GitHub repo. Lets
    panels/settings.py avoid presenting a "View releases on GitHub" link
    that would 404, without hardcoding the placeholder check in the UI."""
    return "/" in _APP_REPO and not _APP_REPO.startswith("REPLACE_ME")


def releases_url() -> str:
    return f"https://github.com/{_APP_REPO}/releases"


# ---------------------------------------------------------------------------
# Shared helpers (direct port of R9Tools' updater.py)
# ---------------------------------------------------------------------------


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
    """
    Returns (update_available, latest_version_str).
    Raises on network error (including "repo not configured yet" --
    urlopen against the REPLACE_ME placeholder will fail DNS resolution,
    which is the correct behavior: callers should surface that as a normal
    check-failed error, not pretend it succeeded).
    """
    data = _fetch_release(_APP_REPO)
    latest = data.get("tag_name", "").lstrip("v")
    cur = current_version.lstrip("v")
    return (bool(latest) and latest != cur), latest


def _find_zip_asset_url(data: dict) -> str:
    """
    Pick the release zip asset. Mirrors R9Tools' `_find_zip_asset_url`:
    match on both the ``.zip`` extension and the expected name prefix so
    this doesn't accidentally grab an unrelated asset if a release ever
    ships more than one, falling back to any ``.zip`` if the naming
    convention ever changes.
    """
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
    """
    Download the latest release zip and extract the installer exe from it
    into a fresh temp directory.

    Only works when frozen (PyInstaller build). Raises otherwise -- this
    project has no PyInstaller build yet either, so this will always raise
    when run from source; that's expected, not a bug (see module docstring).

    Returns the path to the extracted installer exe. Nothing is executed
    here -- call launch_installer_and_quit() with the returned path once the
    caller is ready to hand off to the installer.
    """
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


# ---------------------------------------------------------------------------
# Installer handoff (direct port of R9Tools' updater.py)
# ---------------------------------------------------------------------------


def _quote_ps_single(value: str) -> str:
    """
    Wrap ``value`` in single quotes for safe embedding in a PowerShell
    -Command string, doubling any embedded single quotes (PowerShell's
    single-quoted-string escape rule) so paths with apostrophes still
    round-trip as one literal token.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _build_relaunch_command(pid: int, installer_path: Path, install_args: list[str]) -> str:
    """
    Build the PowerShell -Command string that waits for the process ``pid``
    to fully exit, then starts ``installer_path`` with ``install_args``.

    Split out as its own pure function (no subprocess spawning), same as
    R9Tools' version, so the generated command text can be unit-tested
    directly without actually spawning anything.
    """
    arg_list = ", ".join(_quote_ps_single(a) for a in install_args)
    return (
        f"Wait-Process -Id {pid} -Timeout 10 -ErrorAction SilentlyContinue; "
        f"Start-Process -FilePath {_quote_ps_single(str(installer_path))} "
        f"-ArgumentList @({arg_list}) -WindowStyle Hidden"
    )


def _quote_cmdline_arg(value: str) -> str:
    """
    Quote ``value`` as a single Win32 command-line argument, following the
    same rules `CommandLineToArgvW` uses to parse a command line back apart
    (the rules `ShellExecuteExW`'s `lpParameters` is parsed with) -- NOT
    `_quote_ps_single`'s PowerShell single-quoted-string rules above, which
    operate one layer further in, inside a `-Command` string that has
    already survived this outer layer intact.

    A run of N backslashes immediately followed by a literal `"` becomes
    2N+1 backslashes then a `"` (escaping both the quote and every
    backslash guarding it); a run of N backslashes at the very end of the
    argument (immediately before the closing quote this function adds)
    becomes 2N backslashes (escaping only themselves, since there's no
    literal `"` after them to protect). This is the identical
    Microsoft-documented algorithm CPython's own
    `subprocess.list2cmdline` implements -- reproduced here rather than
    imported from `subprocess` so this stays a small, pure, standalone
    function usable with `ShellExecuteExW`'s single-string `lpParameters`
    (subprocess's own version is only reachable via its argv-list APIs).
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
    """
    Build the single `lpParameters` string `ShellExecuteExW` expects for
    launching ``powershell.exe -NoProfile -NonInteractive -ExecutionPolicy
    Bypass -WindowStyle Hidden -Command <ps_command>``.

    `lpParameters` is one raw command-line string parsed by
    `CommandLineToArgvW`, not the argv list the old `subprocess.Popen` call
    used -- each argument (including ``ps_command`` itself, which already
    contains its own inner PowerShell single-quoting from
    `_build_relaunch_command`/`_quote_ps_single`) is quoted independently
    via `_quote_cmdline_arg` so this outer layer can't corrupt anything
    `_build_relaunch_command` already produced.
    """
    args = [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-Command", ps_command,
    ]
    return " ".join(_quote_cmdline_arg(a) for a in args)


# ---------------------------------------------------------------------------
# ShellExecuteExW ("runas") bindings -- see launch_installer_and_quit()'s
# docstring for why elevation is triggered here instead of via a
# subprocess.Popen'd watcher. Raw ctypes, no pywin32, matching this
# project's established convention (tray_icon.py, titlebar.py,
# window_select.py). Declared at module scope, same as window_select.py's
# own user32/dwmapi bindings, rather than gated behind an
# `if sys.platform == "win32"` import guard -- this project's Win32-backed
# modules already assume a Windows host to import cleanly (see
# window_select.py), so matching that precedent here rather than inventing
# a new one.
# ---------------------------------------------------------------------------

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
    """
    Launch ``lpFile`` elevated (UAC "runas" verb) via `ShellExecuteExW`,
    called from this (still-alive, foreground, interactive) process. Raises
    OSError on failure, mirroring how a failed `subprocess.Popen` call would
    have surfaced to `launch_installer_and_quit`'s caller.
    """
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
    """%LOCALAPPDATA%\\Shattered Gaming Overlay\\logs -- this project has no
    crash_logging.py module (unlike R9Tools) to source this path from, so it
    is picked directly here instead of inventing a dependency on a module
    that doesn't exist. If a real crash-log module is ever added, point this
    at it instead."""
    base = os.getenv("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "Shattered Gaming Overlay" / "logs"


def launch_installer_and_quit(installer_path: Path) -> None:
    """
    Hand off to the extracted installer, then return so the caller can quit
    the app.

    This does NOT launch the installer directly. It elevates and starts a
    detached PowerShell watcher that first runs ``Wait-Process -Id <this
    pid>`` (captured via os.getpid() before any quitting happens, with a 10s
    safety-net timeout) and only starts the installer once that resolves --
    i.e. once this process has actually, fully terminated, not merely been
    asked to quit.

    Why the Wait-Process watcher exists at all (ported verbatim from
    R9Tools' own hard-won fix -- see its updater.py): a naive version of
    this raced an installer's file-copy step against this process's own
    file lock on its exe/dlls actually releasing. R9Tools originally tried
    to close that race with only its own Inno-Setup-side
    `CloseApplications=yes` + `AppMutex` (Restart Manager closing the app),
    and a real failed-update install log showed that assumption was wrong
    -- the installer's own AppMutex check can abort within milliseconds of
    launch, faster than a Restart-Manager-mediated close-and-wait can
    complete. Guaranteeing real process death before the installer even
    starts (this watcher) removes the race entirely instead of trying to
    win it.

    Why the watcher is launched via `ShellExecuteExW("runas", ...)` instead
    of a plain `subprocess.Popen`: since v1.1.3/1.1.4 the installed app (and
    its installer) require admin elevation (PresentMon's FPS tracking needs
    an elevated ETW trace session -- see ShatteredGamingOverlay.spec/.iss).
    A `subprocess.Popen`'d, non-elevated PowerShell watcher trying to
    `Start-Process` an admin-manifested installer forces Windows to broker a
    UAC prompt from deep inside a detached, no-window, job-broken-away
    background process -- confirmed live to fail silently (the UAC
    `consent.exe` process appears but the prompt never reaches the user's
    interactive desktop, the installer never runs, and nothing is left
    hung). `ShellExecuteExW`'s `runas` verb, called here, while this process
    is still alive and still owns a normal foreground GUI window, is the
    same mechanism Explorer's own "Run as administrator" uses and reliably
    surfaces the UAC dialog on the correct interactive session. Once the
    user approves that one prompt, the resulting PowerShell process is
    already elevated, so its own later `Start-Process` on the installer
    inherits that elevation with no second prompt.

    The caller should still make the app quit (e.g.
    `hi.get_runner_params().app_shall_exit = True`, mirroring titlebar.py's
    own close path) immediately after this returns -- that's what the
    watcher above is waiting on.

    Flags (Inno Setup command-line silent-install switches -- same as
    R9Tools; assumes the eventual installer is also Inno-Setup-based, since
    that's this repo's only installer precedent to go on):
      /VERYSILENT          - no wizard UI at all
      /SUPPRESSMSGBOXES    - suppress informational message boxes
      /NORESTART            - never prompt for or force a reboot
      /LOG=<path>            - write a full Inno Setup install log to
                              %LOCALAPPDATA%\\Shattered Gaming Overlay\\logs
                              so a silent update attempt (successful or not)
                              leaves a real diagnostic trail

    The PowerShell watcher itself still runs hidden (`-WindowStyle Hidden`
    plus `ShellExecuteExW`'s own `nShow=SW_HIDE`) and detached -- only the
    OS-owned UAC consent dialog is ever shown to the user; the installer it
    eventually launches via `Start-Process` inherits that same
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


# ---------------------------------------------------------------------------
# UpdateManager -- the stateful, thread-safe orchestration R9Tools got for
# free from Qt Signals (see module docstring). Mirrors remapper.py/
# hud_overlay.py's "module owns a singleton, background work stays behind a
# lock, the render thread only ever calls a documented thread-safe method"
# pattern used everywhere else in this project.
# ---------------------------------------------------------------------------


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

    Public API (all safe to call from the Companion window's own/render
    thread):
        start_check(current_version, is_automatic=False)
        start_download()
        install_and_quit() -> bool
        dismiss_auto_prompt()
        sync_to(settings)

    Everything else (the actual network calls) runs on short-lived daemon
    threads spawned here -- never call check_app_update()/download_app()
    directly from the render thread, exactly like remapper.py's warning
    about never reading engine-owned state off the wrong thread, just
    inverted (here it's *writes* from a background thread that must go
    through the lock instead of touching AppState directly).
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
            # Mirrors R9Tools' "Dev build -- skipped" UX: a real, expected
            # state (this app has no PyInstaller build yet), not an error.
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
        should now quit the app -- the caller (panels/settings.py) is
        expected to immediately follow a True return with
        `hi.get_runner_params().app_shall_exit = True`, the exact same
        mechanism titlebar.py's own close button uses, so the normal
        before_exit teardown still runs. Returns False (recording an error)
        without asking the caller to quit if the handoff itself fails."""
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
        skip; never touches SettingsState.check_for_updates_on_launch (per
        R9Tools' README-documented behavior)."""
        with self._lock:
            self._auto_prompt_pending = False

    # ------------------------------------------------------------------
    # Frame sync (read-direction lock-guarded snapshot)
    # ------------------------------------------------------------------

    def sync_to(self, settings: "SettingsState") -> None:
        """Call once per Companion-window frame, before the frame is
        rendered (see main.py's `_show_gui`, right alongside
        remapper_engine.update_snapshot()/macro_engine's own equivalents --
        those copy AppState *into* the engine; this copies the engine's
        result *out* into AppState, same lock-guarded-snapshot pattern,
        opposite direction). Never mutate AppState.settings' update_* fields
        from anywhere else."""
        with self._lock:
            settings.update_status = self._status
            settings.update_latest_version = self._latest_version
            settings.update_download_pct = self._download_pct
            settings.update_error_message = self._error_message
            settings.last_checked_display = self._last_checked_display
            settings.auto_update_prompt_pending = self._auto_prompt_pending


# ---------------------------------------------------------------------------
# Process-wide singleton -- main.py drives this alongside the Companion
# window's own lifecycle, same pattern as remapper_engine/macro_engine/
# hud_overlay.
# ---------------------------------------------------------------------------

update_manager = UpdateManager()
