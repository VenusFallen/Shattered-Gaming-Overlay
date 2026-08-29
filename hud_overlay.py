"""hud_overlay.py -- the HUD overlay window: a separate, non-injecting,
click-through top-level window that DWM composites on top of the game via
DirectComposition. This is NOT a hook into any game's own DX11/DX12/Vulkan
swap chain, and nothing here is ever injected into another process. This
module follows the project's hard architectural rules for HUD-style
overlays.

Renders three things: the accessibility crosshair (first slice), the Stats
box (CPU/GPU/VRAM/RAM/FPS), and the two module status badges (Remapper /
Macros enabled-count). Text rendering (Stats box numbers, badge counts) uses
overlay_renderer.Renderer's GDI-backed text pipeline, ported in from
R9Tools' dx11_renderer.py -- see that module's docstring.

Threading model:
  - Runs entirely on its own background thread (start()/stop() from the
    Companion window's main thread).
  - The Companion window calls `update_crosshair(CrosshairState)`,
    `update_stats(StatsHudState, StatsSnapshot)`,
    `update_indicators(StatusIndicatorsState, remap_count, macro_count)`, and
    `update_theme(Theme)` once per its own frame (see main.py's `_show_gui`
    and shell.py's `render_frame`). Each copies only the handful of
    plain-data fields it needs into its own lock-guarded snapshot -- the
    live dataclass instances themselves are never handed across the thread
    boundary, per this project's "lock-guarded snapshot, not
    shared-mutable-state" rule.
  - The render thread reads all snapshots once per frame under the same
    lock. This mirrors R9Tools' `update_stats(dict)` / `threading.Lock`
    pattern (see dx11_overlay.py there).

Overlay visibility is intentionally NEVER gated by window focus or by
the Remapper/Macro engine's window-filter state (per this module's own
"Overlay visibility is never gated by the process-select window filter"
scoping rule) -- each element renders whenever its own `enabled` flag is
True, full stop, regardless of which window currently has OS focus. This is
a deliberate
divergence from R9Tools' model, where the crosshair hid on focus loss.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import dcomp_bridge as dcomp
import dx11_bridge as dx
import theme as theme_module
from overlay_renderer import Renderer

_log = logging.getLogger("shattered_overlay.hud")

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x00000008
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOREDIRECTIONBITMAP = 0x00200000  # no GDI surface -- DComp owns the content

_WM_DESTROY = 0x0002
_WM_SIZE = 0x0005
_WM_DISPLAYCHANGE = 0x007E
_WM_QUIT = 0x0012

_PM_REMOVE = 0x0001

_SM_CXSCREEN = 0
_SM_CYSCREEN = 1

_GWL_EXSTYLE = -20

_HWND_TOPMOST = -1
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010

# GetWindowLongPtrW is the 64-bit-safe variant; fall back to GetWindowLongW
# on builds where the Ptr variant isn't exported (32-bit).
_GetWindowLongPtrW = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
_GetWindowLongPtrW.restype = ctypes.c_int64 if hasattr(_user32, "GetWindowLongPtrW") else ctypes.c_int32
_GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]

_SetWindowPos = _user32.SetWindowPos
_SetWindowPos.restype = wintypes.BOOL
_SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_uint]

# On x64 Windows WPARAM/LPARAM/LRESULT are all 64-bit; ctypes.wintypes
# defines them as 32-bit, which causes OverflowError for large lparams.
_WPARAM = ctypes.c_uint64
_LPARAM = ctypes.c_int64
_LRESULT = ctypes.c_int64

_DefWindowProcW = _user32.DefWindowProcW
_DefWindowProcW.restype = _LRESULT
_DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM]

_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM)


class _WndClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


# ---------------------------------------------------------------------------
# Thread-safe snapshot handed from the Companion window to the render thread
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CrosshairSnapshot:
    """Plain-data copy of app_state.CrosshairState's fields. Frozen +
    immutable so handing a reference across the lock boundary is safe
    without needing to defensively copy again on read."""

    enabled: bool = False
    style: str = "Cross"
    size: float = 12.0
    thickness: float = 2.0
    gap: float = 3.0
    color: tuple = (0.24, 0.86, 0.52, 1.0)


_DEFAULT_SNAPSHOT = _CrosshairSnapshot()

_BLACK = (0.0, 0.0, 0.0, 1.0)


@dataclass(frozen=True)
class _StatsSnapshot:
    """Plain-data copy of app_state.StatsHudState's fields PLUS the latest
    stats_poller.StatsSnapshot values, combined here since the render thread
    needs both together every frame and only ever reads them together (see
    main.py's `_show_gui` -- one `update_stats()` call per frame)."""

    enabled: bool = False
    show_cpu: bool = True
    show_gpu: bool = True
    show_ram: bool = True
    show_fps: bool = True
    corner: str = "Top Right"
    scale: float = 1.0
    color: tuple = (0.93, 0.94, 0.96, 1.0)
    bg_alpha: float = 0.55

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


_DEFAULT_STATS_SNAPSHOT = _StatsSnapshot()


@dataclass(frozen=True)
class _IndicatorSnapshot:
    """Plain-data copy of app_state.StatusIndicatorsState's fields plus the
    live enabled-entry counts (computed each frame from
    app_state.remapper.entries / app_state.macros.macros -- see main.py)."""

    enabled: bool = False
    show_remap_badge: bool = True
    show_macro_badge: bool = True
    corner: str = "Bottom Left"
    scale: float = 1.0
    remap_count: int = 0
    macro_count: int = 0


_DEFAULT_INDICATOR_SNAPSHOT = _IndicatorSnapshot()


@dataclass(frozen=True)
class _ThemeSnapshot:
    """The handful of theme.Theme fields the Stats box / status badges
    actually need to visually track the Companion window's active theme
    (including live Color Cycle drift -- see shell.py's `render_frame`,
    which resolves Color Cycle to a real Theme every frame before calling
    `update_theme()`). Defaults reuse theme.DARK's own values so the overlay
    never renders an arbitrary placeholder color before the first real
    `update_theme()` call lands."""

    accent: tuple = theme_module.DARK.accent
    accent_text: tuple = theme_module.DARK.accent_text
    text_primary: tuple = theme_module.DARK.text_primary
    text_secondary: tuple = theme_module.DARK.text_secondary
    bg_card: tuple = theme_module.DARK.bg_card
    border: tuple = theme_module.DARK.border


_DEFAULT_THEME_SNAPSHOT = _ThemeSnapshot()


def _fmt_pct(v: Optional[float]) -> str:
    return f"{v:.0f}%" if v is not None else "--"


def _fmt_gb(v: Optional[float]) -> str:
    return f"{v:.1f}" if v is not None else "--"


def _fmt_temp(v: Optional[float]) -> str:
    return f"{v:.0f}°C" if v is not None else "--"


def _corner_origin(corner: str, margin: float, w: float, h: float, sw: float, sh: float) -> tuple:
    """Top-left (x, y) for a `w`x`h` box anchored to `corner` of the screen,
    inset by `margin`. Shared by the Stats box and the status badges --
    same position-name convention as panels/overlay.py's `_POSITIONS`
    (renamed from `_CORNERS` when Middle Left/Right were added -- it's no
    longer just the four corners)."""
    if corner == "Top Left":
        return margin, margin
    if corner == "Top Middle":
        return (sw - w) / 2.0, margin
    if corner == "Top Right":
        return sw - w - margin, margin
    if corner == "Middle Left":
        return margin, (sh - h) / 2.0
    if corner == "Middle Right":
        return sw - w - margin, (sh - h) / 2.0
    if corner == "Bottom Left":
        return margin, sh - h - margin
    if corner == "Bottom Middle":
        return (sw - w) / 2.0, sh - h - margin
    return sw - w - margin, sh - h - margin  # "Bottom Right" default


# ---------------------------------------------------------------------------
# HudOverlay
# ---------------------------------------------------------------------------


class HudOverlay:
    """The click-through HUD overlay window + its own DX11/DComp swap chain.

    Public thread-safe methods (callable from the Companion window's thread):
      start()
      stop()
      update_crosshair(crosshair_state)
      update_stats(stats_hud_state, stats_poller_snapshot)
      update_indicators(status_indicators_state, remap_count, macro_count)
      update_theme(theme)
    """

    _CLASS_NAME = "ShatteredGamingOverlayHUD"
    _WINDOW_TITLE = "Shattered Gaming Overlay HUD"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: _CrosshairSnapshot = _DEFAULT_SNAPSHOT
        self._stats_snapshot: _StatsSnapshot = _DEFAULT_STATS_SNAPSHOT
        self._indicator_snapshot: _IndicatorSnapshot = _DEFAULT_INDICATOR_SNAPSHOT
        self._theme_snapshot: _ThemeSnapshot = _DEFAULT_THEME_SNAPSHOT

        self._running = False
        self._hwnd = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()  # set once window + DX are initialised

        self._sw = 0
        self._sh = 0

        # DX11 + DComp objects (owned exclusively by the render thread)
        self._device = 0
        self._context = 0
        self._swap_chain = 0
        self._rtv = 0
        self._renderer: Optional[Renderer] = None
        self._dcomp_device = 0
        self._dcomp_target = 0
        self._dcomp_visual = 0

        self._present_failure_logged = False
        self._wnd_proc_cb = None  # keep the ctypes callback alive for the window's lifetime

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="HudOverlay")
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._running = False
        if self._hwnd:
            _user32.PostMessageW(self._hwnd, _WM_DESTROY, 0, 0)
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None

    def update_crosshair(self, crosshair_state) -> None:
        """Called once per Companion-window frame (see main.py). Copies the
        handful of plain-data fields it needs out of the live CrosshairState
        dataclass into an immutable snapshot under a lock -- the render
        thread never touches the CrosshairState instance itself."""
        snap = _CrosshairSnapshot(
            enabled=bool(crosshair_state.enabled),
            style=str(crosshair_state.style),
            size=float(crosshair_state.size),
            thickness=float(crosshair_state.thickness),
            gap=float(crosshair_state.gap),
            color=tuple(crosshair_state.color),
        )
        with self._lock:
            self._snapshot = snap

    def update_stats(self, stats_hud_state, stats_poller_snapshot) -> None:
        """Called once per Companion-window frame (see main.py). Combines
        app_state.StatsHudState's toggles/style fields with the latest
        stats_poller.StatsSnapshot values into one immutable snapshot under
        a lock -- same "copy plain data, don't share the live object" rule
        as update_crosshair() above."""
        snap = _StatsSnapshot(
            enabled=bool(stats_hud_state.enabled),
            show_cpu=bool(stats_hud_state.show_cpu),
            show_gpu=bool(stats_hud_state.show_gpu),
            show_ram=bool(stats_hud_state.show_ram),
            show_fps=bool(stats_hud_state.show_fps),
            corner=str(stats_hud_state.corner),
            scale=float(stats_hud_state.scale),
            color=tuple(stats_hud_state.color),
            bg_alpha=float(stats_hud_state.bg_alpha),
            available=bool(stats_poller_snapshot.available),
            error=stats_poller_snapshot.error,
            cpu_pct=stats_poller_snapshot.cpu_pct,
            cpu_temp=stats_poller_snapshot.cpu_temp,
            gpu_pct=stats_poller_snapshot.gpu_pct,
            gpu_temp=stats_poller_snapshot.gpu_temp,
            gpu_vram_used_gb=stats_poller_snapshot.gpu_vram_used_gb,
            gpu_vram_total_gb=stats_poller_snapshot.gpu_vram_total_gb,
            ram_used_gb=stats_poller_snapshot.ram_used_gb,
            ram_total_gb=stats_poller_snapshot.ram_total_gb,
            fps=stats_poller_snapshot.fps,
            fps_error=stats_poller_snapshot.fps_error,
        )
        with self._lock:
            self._stats_snapshot = snap

    def update_indicators(self, status_indicators_state, remap_count: int, macro_count: int) -> None:
        """Called once per Companion-window frame (see main.py). `remap_count`/
        `macro_count` are the live counts of *enabled* RemapEntry/MacroDef
        entries -- computed by the caller from app_state.remapper.entries /
        app_state.macros.macros, since this module never touches app_state
        directly (see module docstring)."""
        snap = _IndicatorSnapshot(
            enabled=bool(status_indicators_state.enabled),
            show_remap_badge=bool(status_indicators_state.show_remap_badge),
            show_macro_badge=bool(status_indicators_state.show_macro_badge),
            corner=str(status_indicators_state.corner),
            scale=float(status_indicators_state.scale),
            remap_count=int(remap_count),
            macro_count=int(macro_count),
        )
        with self._lock:
            self._indicator_snapshot = snap

    def update_theme(self, theme) -> None:
        """Called once per Companion-window frame from shell.py's
        `render_frame`, right after it resolves the active theme (including
        the live, time-varying Color Cycle resolution -- see
        theme.resolve_color_cycle_theme()) -- so the Stats box border and
        the status badges' accent rings/glow visibly track whatever theme
        (or Color Cycle instant) is currently active, the same way the rest
        of the Companion window's UI does."""
        snap = _ThemeSnapshot(
            accent=tuple(theme.accent),
            accent_text=tuple(theme.accent_text),
            text_primary=tuple(theme.text_primary),
            text_secondary=tuple(theme.text_secondary),
            bg_card=tuple(theme.bg_card),
            border=tuple(theme.border),
        )
        with self._lock:
            self._theme_snapshot = snap

    def _read_snapshot(self) -> _CrosshairSnapshot:
        with self._lock:
            return self._snapshot

    def _read_stats_snapshot(self) -> _StatsSnapshot:
        with self._lock:
            return self._stats_snapshot

    def _read_indicator_snapshot(self) -> _IndicatorSnapshot:
        with self._lock:
            return self._indicator_snapshot

    def _read_theme_snapshot(self) -> _ThemeSnapshot:
        with self._lock:
            return self._theme_snapshot

    # ------------------------------------------------------------------
    # Background thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._create_window()
            self._create_dx11()
            self._ready.set()
            self._render_loop()
        except Exception:
            logging.exception("[HudOverlay] Fatal error in render thread")
            self._ready.set()
        finally:
            self._teardown()

    # ------------------------------------------------------------------
    # Window creation
    # ------------------------------------------------------------------

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == _WM_DESTROY:
            self._running = False
            _user32.PostQuitMessage(0)
            return 0
        if msg == _WM_SIZE:
            if self._swap_chain and self._renderer:
                w = lparam & 0xFFFF
                h = (lparam >> 16) & 0xFFFF
                if w > 0 and h > 0:
                    self._on_resize(w, h)
            return 0
        if msg == _WM_DISPLAYCHANGE:
            self._handle_display_change()
            return 0
        return _DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self) -> None:
        hinstance = _kernel32.GetModuleHandleW(None)
        self._wnd_proc_cb = _WNDPROC(self._wnd_proc)

        wc = _WndClass()
        wc.style = 0
        wc.lpfnWndProc = self._wnd_proc_cb
        wc.hInstance = hinstance
        wc.lpszClassName = self._CLASS_NAME

        _user32.RegisterClassW(ctypes.byref(wc))

        sw = _user32.GetSystemMetrics(_SM_CXSCREEN)
        sh = _user32.GetSystemMetrics(_SM_CYSCREEN)
        self._sw = sw
        self._sh = sh

        # WS_EX_NOREDIRECTIONBITMAP: no GDI-accessible surface for this
        # window; DComp owns the visual content and DWM reads the DXGI swap
        # chain directly. WS_EX_LAYERED is required for real click-through
        # here even though SetLayeredWindowAttributes/UpdateLayeredWindow are
        # never called -- WS_EX_TRANSPARENT alone is not sufficient on this
        # window type (verified against R9Tools' own prior finding, and
        # re-verified independently below with a live WindowFromPoint test).
        ex_style = (_WS_EX_TOPMOST | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
                    | _WS_EX_TRANSPARENT | _WS_EX_NOREDIRECTIONBITMAP
                    | _WS_EX_LAYERED)

        hwnd = _user32.CreateWindowExW(
            ex_style,
            self._CLASS_NAME, self._WINDOW_TITLE,
            _WS_POPUP,
            0, 0, sw, sh,
            None, None, hinstance, None,
        )
        if not hwnd:
            _user32.UnregisterClassW(self._CLASS_NAME, hinstance)
            raise OSError(f"CreateWindowEx failed: {ctypes.GetLastError()}")
        self._hwnd = hwnd

        # Show immediately -- with DComp/premultiplied-alpha the window is
        # visually transparent until a non-zero-alpha frame is presented.
        _user32.ShowWindow(hwnd, 1)  # SW_SHOWNORMAL

        # Defensive: WS_EX_TOPMOST passed to CreateWindowExW is usually
        # honoured for initial z-order placement but not universally reliable
        # (some games repeatedly reassert their own HWND_TOPMOST). Explicitly
        # re-insert at the top of the topmost band right after creation.
        _SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)

        exstyle = _GetWindowLongPtrW(hwnd, _GWL_EXSTYLE)
        _log.info(
            "[HudOverlay] hwnd=0x%X exstyle=0x%08X TRANSPARENT=%s TOPMOST=%s LAYERED=%s NOREDIRECTIONBITMAP=%s",
            hwnd, exstyle & 0xFFFFFFFF,
            bool(exstyle & _WS_EX_TRANSPARENT),
            bool(exstyle & _WS_EX_TOPMOST),
            bool(exstyle & _WS_EX_LAYERED),
            bool(exstyle & _WS_EX_NOREDIRECTIONBITMAP),
        )

    # ------------------------------------------------------------------
    # DX11 + DirectComposition setup
    # ------------------------------------------------------------------

    def _create_dx11(self) -> None:
        # 1. D3D11 device (BGRA_SUPPORT flag required for B8G8R8A8 swap chain)
        self._device, self._context = dx.create_device()

        # 2. DComp device shares our GPU device
        dxgi_dev = dx._query(self._device, dx._IID_IDXGIDevice)
        self._dcomp_device = dcomp.create_dcomp_device(dxgi_dev)
        dx._release(dxgi_dev)

        # 3. Flip-model swap chain registered with DComp
        factory2 = dx.get_factory2()
        self._swap_chain = dx.create_swap_chain_for_composition(factory2, self._device, self._sw, self._sh)
        dx._release(factory2)

        # 4. Wire the swap chain into DWM's composition tree and commit
        self._dcomp_target = dcomp.create_target(self._dcomp_device, self._hwnd)
        self._dcomp_visual = dcomp.create_visual(self._dcomp_device)
        dcomp.visual_set_content(self._dcomp_visual, self._swap_chain)
        dcomp.target_set_root(self._dcomp_target, self._dcomp_visual)
        dcomp.commit(self._dcomp_device)

        # 5. RTV and renderer
        self._rtv = dx.make_rtv(self._device, self._swap_chain)
        self._renderer = Renderer(self._device, self._context, self._sw, self._sh)
        dx.ctx_set_viewport(self._context, float(self._sw), float(self._sh))
        dx.ctx_set_rtv(self._context, self._rtv)

    # ------------------------------------------------------------------
    # Render loop
    # ------------------------------------------------------------------

    def _render_loop(self) -> None:
        msg = _MSG()
        FRAME = 1.0 / 60.0
        _had_content = False

        while self._running:
            t0 = time.monotonic()

            while _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
                if msg.message == _WM_QUIT:
                    self._running = False
                    break

            if not self._running:
                break

            snap = self._read_snapshot()
            stats_snap = self._read_stats_snapshot()
            indicator_snap = self._read_indicator_snapshot()
            theme_snap = self._read_theme_snapshot()
            needs_draw = (
                snap.enabled
                or stats_snap.enabled
                or (indicator_snap.enabled
                    and (indicator_snap.show_remap_badge or indicator_snap.show_macro_badge))
            )

            if needs_draw:
                _had_content = True
                self._render_frame(snap, stats_snap, indicator_snap, theme_snap)
            elif _had_content:
                # Present one fully transparent frame to clear the overlay.
                dx.ctx_clear_rtv(self._context, self._rtv, 0.0, 0.0, 0.0, 0.0)
                hr = dx.swap_present(self._swap_chain, 0, 0)
                self._check_present_result(hr)
                _had_content = False

            elapsed = time.monotonic() - t0
            sleep = FRAME - elapsed
            if sleep > 0.001:
                time.sleep(sleep)

    def _check_present_result(self, hr: int) -> None:
        if hr >= 0:
            self._present_failure_logged = False
            return
        if self._present_failure_logged:
            return
        self._present_failure_logged = True
        hr_u = hr & 0xFFFFFFFF
        _log.error("[HudOverlay] swap_present failed: HRESULT 0x%08X", hr_u)
        if hr_u in (dx.DXGI_ERROR_DEVICE_REMOVED, dx.DXGI_ERROR_DEVICE_RESET):
            try:
                reason = dx.device_get_device_removed_reason(self._device)
                _log.error("[HudOverlay] GetDeviceRemovedReason: HRESULT 0x%08X", reason & 0xFFFFFFFF)
            except Exception:
                _log.exception("[HudOverlay] GetDeviceRemovedReason call failed")

    def _render_frame(self, snap: _CrosshairSnapshot, stats_snap: _StatsSnapshot,
                       indicator_snap: _IndicatorSnapshot, theme_snap: _ThemeSnapshot) -> None:
        ctx = self._context
        r = self._renderer

        dx.ctx_clear_rtv(ctx, self._rtv, 0.0, 0.0, 0.0, 0.0)
        dx.ctx_set_rtv(ctx, self._rtv)

        r.begin()
        self._draw_crosshair(r, snap)
        self._draw_stats_box(r, stats_snap, theme_snap)
        self._draw_indicators(r, indicator_snap, theme_snap)
        r.end()

        hr = dx.swap_present(self._swap_chain, 0, 0)  # no vsync -- game controls timing
        self._check_present_result(hr)

    # ------------------------------------------------------------------
    # Crosshair drawing
    # ------------------------------------------------------------------

    def _draw_crosshair(self, r: Renderer, snap: _CrosshairSnapshot) -> None:
        if not snap.enabled:
            # Mirrors _draw_stats_box/_draw_indicators' own early-out. Without
            # this, the crosshair rendered unconditionally any time
            # `_render_loop`'s `needs_draw` was True for a DIFFERENT reason
            # (e.g. only the Stats HUD toggle on) -- `snap.enabled` was
            # already one of `needs_draw`'s OR-conditions (so the loop
            # correctly skips presenting when EVERYTHING is off), but nothing
            # re-checked it here once any element made needs_draw True.
            # Caught via an actual screenshot with Stats HUD on and Crosshair
            # off, per this project's own screenshot-verification rule.
            return
        style = snap.style
        size = max(1.0, snap.size)
        thick = max(0.5, snap.thickness)
        gap = max(0.0, snap.gap)
        fg = snap.color
        bg = _BLACK

        cx = self._sw * 0.5
        cy = self._sh * 0.5

        # Small readability outline drawn in black behind the accent color,
        # same technique R9Tools used -- keeps the crosshair visible against
        # both bright and dark backgrounds without adding new CrosshairState
        # fields (see app_state.CrosshairState -- no outline field exists,
        # this is a fixed, non-configurable readability aid). 1px per side,
        # tightened down from an initial 1.5 for a less heavy line.
        outline = 1.0

        if style == "Dot":
            radius = size * 0.4
            r.draw_circle_filled(cx, cy, radius + outline, bg)
            r.draw_circle_filled(cx, cy, radius, fg)
            return

        if style == "Circle":
            r.draw_circle(cx, cy, size, bg, thickness=thick + outline * 2)
            r.draw_circle(cx, cy, size, fg, thickness=thick)
            return

        if style == "Circle + Dot":
            # `gap` is an independent offset on top of `size` here -- moves
            # the ring closer to/further from the center dot without
            # changing the dot's own radius (still size*0.15 below) or the
            # ring's thickness (`thick`, set by the Thickness slider).
            ring_radius = size + gap
            r.draw_circle(cx, cy, ring_radius, bg, thickness=thick + outline * 2)
            r.draw_circle(cx, cy, ring_radius, fg, thickness=thick)
            dot_radius = max(1.5, size * 0.15)
            r.draw_circle_filled(cx, cy, dot_radius + outline, bg)
            r.draw_circle_filled(cx, cy, dot_radius, fg)
            return

        if style == "T-Shape":
            ow = thick + outline * 2
            # Horizontal bar (left + right arms) + a single vertical arm
            # below center -- no arm above center, forming a "T".
            r.draw_line(cx - gap - size, cy, cx - gap, cy, ow, bg)
            r.draw_line(cx + gap, cy, cx + gap + size, cy, ow, bg)
            r.draw_line(cx, cy + gap, cx, cy + gap + size, ow, bg)

            r.draw_line(cx - gap - size, cy, cx - gap, cy, thick, fg)
            r.draw_line(cx + gap, cy, cx + gap + size, cy, thick, fg)
            r.draw_line(cx, cy + gap, cx, cy + gap + size, thick, fg)
            return

        # Default: "Cross" -- 4 arms, `gap` apart at the center (0 = the
        # arms touch, forming an unbroken plus rather than an open cross).
        ow = thick + outline * 2

        def draw_cross(col, w):
            r.draw_line(cx - gap - size, cy, cx - gap, cy, w, col)
            r.draw_line(cx + gap, cy, cx + gap + size, cy, w, col)
            r.draw_line(cx, cy - gap - size, cx, cy - gap, w, col)
            r.draw_line(cx, cy + gap, cx, cy + gap + size, w, col)

        draw_cross(bg, ow)
        draw_cross(fg, thick)

    # ------------------------------------------------------------------
    # Stats box drawing -- deliberately plain: rounded-corner box,
    # semi-transparent background, simple stacked label lines. Contrasts on
    # purpose with the "artsy" status badges below (see _draw_indicators).
    # ------------------------------------------------------------------

    def _draw_stats_box(self, r: Renderer, snap: _StatsSnapshot, theme: _ThemeSnapshot) -> None:
        if not snap.enabled:
            return

        scale = max(0.1, snap.scale)
        font_size = max(8, int(round(13 * scale)))
        pad = 12.0 * scale
        line_gap = 4.0 * scale
        font_face = "Segoe UI"

        lines: list = []
        if not snap.available:
            lines.append(snap.error or "Stats unavailable")
        else:
            if snap.show_cpu:
                cpu_line = f"CPU   {_fmt_pct(snap.cpu_pct)}"
                if snap.cpu_temp is not None:
                    cpu_line += f"   {_fmt_temp(snap.cpu_temp)}"
                lines.append(cpu_line)
            if snap.show_gpu:
                gpu_line = f"GPU   {_fmt_pct(snap.gpu_pct)}"
                if snap.gpu_temp is not None:
                    gpu_line += f"   {_fmt_temp(snap.gpu_temp)}"
                lines.append(gpu_line)
                if snap.gpu_vram_used_gb is not None or snap.gpu_vram_total_gb is not None:
                    lines.append(f"VRAM  {_fmt_gb(snap.gpu_vram_used_gb)} / {_fmt_gb(snap.gpu_vram_total_gb)} GB")
            if snap.show_ram:
                lines.append(f"RAM   {_fmt_gb(snap.ram_used_gb)} / {_fmt_gb(snap.ram_total_gb)} GB")
            if snap.show_fps:
                if snap.fps is not None:
                    lines.append(f"FPS   {snap.fps:.0f}")
                else:
                    lines.append("FPS   --")

        if not lines:
            return

        sizes = [r.measure_text(text, font_size, font_face) for text in lines]
        text_w = max(w for w, _h in sizes) if sizes else 0
        line_h = sizes[0][1] if sizes else font_size

        box_w = text_w + pad * 2
        box_h = pad * 2 + len(lines) * line_h + max(0, len(lines) - 1) * line_gap

        x, y = _corner_origin(snap.corner, 20.0 * scale, box_w, box_h, self._sw, self._sh)

        bg = (theme.bg_card[0], theme.bg_card[1], theme.bg_card[2], max(0.0, min(1.0, snap.bg_alpha)))
        border_col = (theme.accent[0], theme.accent[1], theme.accent[2], 0.55)

        r.draw_rounded_rect_filled(x, y, box_w, box_h, 10.0 * scale, bg)
        r.draw_rounded_rect(x, y, box_w, box_h, 10.0 * scale, border_col, thickness=1.5 * scale)

        ty = y + pad
        for text, (_w, h) in zip(lines, sizes):
            r.draw_text(text, x + pad, ty, snap.color, font_size, font_face)
            ty += h + line_gap

    # ------------------------------------------------------------------
    # Module status badges -- "artsy" by explicit request, deliberately
    # contrasting the Stats box's plain design: circular badges with a
    # themed accent ring + soft glow, count centered inside, small label
    # beneath. Always built from the live theme snapshot (never fixed
    # colors) so these visibly track theme changes and Color Cycle drift.
    # ------------------------------------------------------------------

    def _draw_indicator_badge(self, r: Renderer, cx: float, cy: float, radius: float,
                               count: int, label: str, theme: _ThemeSnapshot, scale: float) -> None:
        accent = theme.accent
        # Soft outward glow: a few concentric rings of decreasing alpha --
        # cheap stand-in for a real blur (no blur shader in this geometry
        # pipeline), same idea as a CSS box-shadow ring.
        for i, dr in enumerate((7.0, 4.5, 2.0)):
            alpha = 0.10 - i * 0.03
            if alpha <= 0.0:
                continue
            r.draw_circle(cx, cy, radius + dr * scale, (accent[0], accent[1], accent[2], alpha),
                           thickness=3.0 * scale, segments=40)

        # Badge background -- translucent themed card color.
        r.draw_circle_filled(cx, cy, radius, (theme.bg_card[0], theme.bg_card[1], theme.bg_card[2], 0.72),
                              segments=48)

        # Crisp accent ring right at the badge edge.
        r.draw_circle(cx, cy, radius, (accent[0], accent[1], accent[2], 0.95), thickness=2.5 * scale, segments=48)

        # Count, centered.
        num_str = str(count)
        num_font = max(8, int(round(radius * 0.85)))
        nw, nh = r.measure_text(num_str, num_font, "Segoe UI")
        r.draw_text(num_str, cx - nw / 2.0, cy - nh / 2.0 - radius * 0.16, theme.text_primary, num_font, "Segoe UI")

        # Small label beneath the count.
        label_font = max(7, int(round(radius * 0.32)))
        lw, lh = r.measure_text(label, label_font, "Segoe UI")
        r.draw_text(label, cx - lw / 2.0, cy + radius * 0.30, theme.text_secondary, label_font, "Segoe UI")

    def _draw_indicators(self, r: Renderer, snap: _IndicatorSnapshot, theme: _ThemeSnapshot) -> None:
        if not snap.enabled:
            return
        badges = []
        if snap.show_remap_badge:
            badges.append(("Remap", snap.remap_count))
        if snap.show_macro_badge:
            badges.append(("Macros", snap.macro_count))
        if not badges:
            return

        scale = max(0.1, snap.scale)
        radius = 30.0 * scale
        gap = 18.0 * scale
        margin = 20.0 * scale

        total_w = len(badges) * radius * 2.0 + max(0, len(badges) - 1) * gap
        total_h = radius * 2.0

        x0, y0 = _corner_origin(snap.corner, margin, total_w, total_h, self._sw, self._sh)
        cx = x0 + radius
        cy = y0 + radius
        for label, count in badges:
            self._draw_indicator_badge(r, cx, cy, radius, count, label, theme, scale)
            cx += radius * 2.0 + gap

    # ------------------------------------------------------------------
    # Resize
    # ------------------------------------------------------------------

    def _handle_display_change(self) -> None:
        sw = _user32.GetSystemMetrics(_SM_CXSCREEN)
        sh = _user32.GetSystemMetrics(_SM_CYSCREEN)
        if sw <= 0 or sh <= 0:
            return
        _SetWindowPos(self._hwnd, _HWND_TOPMOST, 0, 0, sw, sh, _SWP_NOACTIVATE)

    def _on_resize(self, w: int, h: int) -> None:
        self._sw = w
        self._sh = h
        if self._rtv:
            dx._release(self._rtv)
            self._rtv = 0
        dx.swap_resize(self._swap_chain, w, h)
        self._rtv = dx.make_rtv(self._device, self._swap_chain)
        if self._renderer:
            self._renderer.resize(w, h)
        dx.ctx_set_viewport(self._context, float(w), float(h))
        dx.ctx_set_rtv(self._context, self._rtv)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def _teardown(self) -> None:
        if self._renderer:
            self._renderer.release()
            self._renderer = None
        for attr in ("_rtv", "_swap_chain", "_context", "_device"):
            v = getattr(self, attr, 0)
            if v:
                dx._release(v)
                setattr(self, attr, 0)
        # DComp objects must be released before the window is destroyed
        for attr in ("_dcomp_visual", "_dcomp_target", "_dcomp_device"):
            v = getattr(self, attr, 0)
            if v:
                dx._release(v)
                setattr(self, attr, 0)
        if self._hwnd:
            _user32.DestroyWindow(self._hwnd)
            _user32.UnregisterClassW(self._CLASS_NAME, _kernel32.GetModuleHandleW(None))
            self._hwnd = 0


# ---------------------------------------------------------------------------
# Process-wide singleton -- main.py starts/stops this alongside the
# Companion window's own lifecycle (see main.py's _post_init/_before_exit).
# ---------------------------------------------------------------------------

hud_overlay = HudOverlay()
