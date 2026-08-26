"""hud_overlay.py -- the HUD overlay window: a separate, non-injecting,
click-through top-level window that DWM composites on top of the game via
DirectComposition. This is NOT a hook into any game's own DX11/DX12/Vulkan
swap chain, and nothing here is ever injected into another process. See
.claude/agents/ui-agent.md for the full set of hard architectural rules this
module follows.

Scope of this first slice: the accessibility crosshair ONLY (see
app_state.CrosshairState / panels/overlay.py's `_render_crosshair`). The
stats HUD and module status indicators stay settings-only for now -- this
was a deliberate scoping decision, not an oversight (see the task that
produced this file). Adding them later means porting the text-rendering
pipeline back in from R9Tools' dx11_renderer.py/dx11_overlay.py and adding
their own snapshot fields here, following the same pattern as
_CrosshairSnapshot below.

Threading model:
  - Runs entirely on its own background thread (start()/stop() from the
    Companion window's main thread).
  - The Companion window calls `update_crosshair(CrosshairState)` once per
    its own frame (see main.py's `_show_gui`). That copies the handful of
    plain-data fields it needs into a lock-guarded snapshot -- the
    CrosshairState dataclass instance itself is never handed across the
    thread boundary, per ui-agent.md's "lock-guarded snapshot, not
    shared-mutable-state" rule.
  - The render thread reads that snapshot once per frame under the same
    lock. This mirrors R9Tools' `update_stats(dict)` /
    `threading.Lock` pattern (see dx11_overlay.py there).

Overlay visibility is intentionally NEVER gated by window focus or by
engine-agent's window-filter state (see ui-agent.md's "Overlay visibility is
never gated by the process-select window filter" scoping rule) -- the
crosshair renders whenever CrosshairState.enabled is True, full stop,
regardless of which window currently has OS focus. This is a deliberate
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
    color: tuple = (0.24, 0.86, 0.52, 1.0)


_DEFAULT_SNAPSHOT = _CrosshairSnapshot()

_BLACK = (0.0, 0.0, 0.0, 1.0)


# ---------------------------------------------------------------------------
# HudOverlay
# ---------------------------------------------------------------------------


class HudOverlay:
    """The click-through HUD overlay window + its own DX11/DComp swap chain.

    Public thread-safe methods (callable from the Companion window's thread):
      start()
      stop()
      update_crosshair(crosshair_state)
    """

    _CLASS_NAME = "ShatteredGamingOverlayHUD"
    _WINDOW_TITLE = "Shattered Gaming Overlay HUD"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: _CrosshairSnapshot = _DEFAULT_SNAPSHOT

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
            color=tuple(crosshair_state.color),
        )
        with self._lock:
            self._snapshot = snap

    def _read_snapshot(self) -> _CrosshairSnapshot:
        with self._lock:
            return self._snapshot

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
        # re-verified independently below with a live WindowFromPoint test --
        # see qa notes in the task report).
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
            needs_draw = snap.enabled

            if needs_draw:
                _had_content = True
                self._render_frame(snap)
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

    def _render_frame(self, snap: _CrosshairSnapshot) -> None:
        ctx = self._context
        r = self._renderer

        dx.ctx_clear_rtv(ctx, self._rtv, 0.0, 0.0, 0.0, 0.0)
        dx.ctx_set_rtv(ctx, self._rtv)

        r.begin()
        self._draw_crosshair(r, snap)
        r.end()

        hr = dx.swap_present(self._swap_chain, 0, 0)  # no vsync -- game controls timing
        self._check_present_result(hr)

    # ------------------------------------------------------------------
    # Crosshair drawing
    # ------------------------------------------------------------------

    def _draw_crosshair(self, r: Renderer, snap: _CrosshairSnapshot) -> None:
        style = snap.style
        size = max(1.0, snap.size)
        thick = max(0.5, snap.thickness)
        fg = snap.color
        bg = _BLACK

        cx = self._sw * 0.5
        cy = self._sh * 0.5

        # Small readability outline drawn in black behind the accent color,
        # same technique R9Tools used -- keeps the crosshair visible against
        # both bright and dark backgrounds without adding new CrosshairState
        # fields (see app_state.CrosshairState -- no outline field exists,
        # this is a fixed, non-configurable readability aid). 1px per side,
        # per user request (was 1.5).
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
            r.draw_circle(cx, cy, size, bg, thickness=thick + outline * 2)
            r.draw_circle(cx, cy, size, fg, thickness=thick)
            dot_radius = max(1.5, size * 0.15)
            r.draw_circle_filled(cx, cy, dot_radius + outline, bg)
            r.draw_circle_filled(cx, cy, dot_radius, fg)
            return

        if style == "T-Shape":
            gap = max(2.0, size * 0.25)
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

        # Default: "Cross" -- 4 arms with a small center gap.
        gap = max(2.0, size * 0.25)
        ow = thick + outline * 2

        def draw_cross(col, w):
            r.draw_line(cx - gap - size, cy, cx - gap, cy, w, col)
            r.draw_line(cx + gap, cy, cx + gap + size, cy, w, col)
            r.draw_line(cx, cy - gap - size, cx, cy - gap, w, col)
            r.draw_line(cx, cy + gap, cx, cy + gap + size, w, col)

        draw_cross(bg, ow)
        draw_cross(fg, thick)

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
