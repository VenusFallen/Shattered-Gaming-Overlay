"""dcomp_bridge.py -- DirectComposition COM vtable wrappers.

DirectComposition (dcomp.dll) lets the HUD overlay register a DXGI swap
chain directly in DWM's composition tree as a separate visual. When hardware
Multi-Plane Overlay (MPO) is available, DWM assigns the overlay to its own
GPU plane and the game's own swap chain can still use Independent Flip --
zero composition overhead. This is why this project uses DirectComposition
rather than any game-swap-chain hook.

Only the handful of methods the HUD overlay uses are wrapped here. Vtable
offsets are fixed by the Windows SDK ABI.

IDCompositionDevice vtable (IUnknown 0-2, own methods from 3):
    Commit                  = 3
    WaitForCommitCompletion = 4
    GetFrameStatistics      = 5
    CreateTargetForHwnd     = 6
    CreateVisual            = 7

IDCompositionTarget vtable (IUnknown 0-2):
    SetRoot = 3

IDCompositionVisual vtable (IUnknown 0-2, overloaded pairs count as separate
slots):
    SetOffsetX(float)          = 3
    SetOffsetX(anim*)          = 4
    SetOffsetY(float)          = 5
    SetOffsetY(anim*)          = 6
    SetTransform(matrix*)      = 7
    SetTransform(iface*)       = 8
    SetTransformParent         = 9
    SetEffect                  = 10
    SetBitmapInterpolationMode = 11
    SetBorderMode               = 12
    SetClip(rect*)              = 13
    SetClip(iface*)             = 14
    SetContent                  = 15
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
from ctypes import c_int, c_void_p, POINTER, byref

from dx11_bridge import _com, _check, GUID, _guid

# ---------------------------------------------------------------------------
# dcomp.dll
# ---------------------------------------------------------------------------

_dcomp = ctypes.windll.LoadLibrary("dcomp.dll")

# ---------------------------------------------------------------------------
# GUIDs
# ---------------------------------------------------------------------------

_IID_IDCompositionDevice = _guid("{C37EA93A-E7AA-450D-B16F-9746CB0407F3}")

# ---------------------------------------------------------------------------
# DCompositionCreateDevice
# ---------------------------------------------------------------------------

_DCompositionCreateDevice = _dcomp.DCompositionCreateDevice
_DCompositionCreateDevice.restype = c_int
_DCompositionCreateDevice.argtypes = [c_void_p, POINTER(GUID), POINTER(c_void_p)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_dcomp_device(dxgi_device: int) -> int:
    """Create an IDCompositionDevice sharing the given IDXGIDevice's GPU.
    Returns IDCompositionDevice* (caller must Release)."""
    dev = c_void_p(None)
    hr = _DCompositionCreateDevice(
        c_void_p(dxgi_device),
        byref(_IID_IDCompositionDevice),
        byref(dev),
    )
    _check(hr, "DCompositionCreateDevice")
    return dev.value


def create_target(device: int, hwnd: int) -> int:
    """IDCompositionDevice::CreateTargetForHwnd (slot 6, topmost=True).
    Returns IDCompositionTarget*."""
    target = c_void_p(None)
    hr = _com(
        device, 6, c_int,
        [wintypes.HWND, c_int, POINTER(c_void_p)],
        hwnd, 1, byref(target),
    )
    _check(hr, "IDCompositionDevice::CreateTargetForHwnd")
    return target.value


def create_visual(device: int) -> int:
    """IDCompositionDevice::CreateVisual (slot 7). Returns IDCompositionVisual*."""
    visual = c_void_p(None)
    hr = _com(device, 7, c_int, [POINTER(c_void_p)], byref(visual))
    _check(hr, "IDCompositionDevice::CreateVisual")
    return visual.value


def visual_set_content(visual: int, swap_chain: int) -> None:
    """IDCompositionVisual::SetContent (slot 15) -- bind swap chain to visual."""
    hr = _com(visual, 15, c_int, [c_void_p], c_void_p(swap_chain))
    _check(hr, "IDCompositionVisual::SetContent")


def target_set_root(target: int, visual: int) -> None:
    """IDCompositionTarget::SetRoot (slot 3)."""
    hr = _com(target, 3, c_int, [c_void_p], c_void_p(visual))
    _check(hr, "IDCompositionTarget::SetRoot")


def commit(device: int) -> None:
    """IDCompositionDevice::Commit (slot 3) -- push pending changes to DWM."""
    hr = _com(device, 3, c_int, [])
    _check(hr, "IDCompositionDevice::Commit")
