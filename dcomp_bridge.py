"""dcomp_bridge.py -- DirectComposition (dcomp.dll) COM vtable wrappers.
Lets the HUD overlay register its DXGI swap chain directly in DWM's
composition tree as its own visual, so hardware Multi-Plane Overlay can give
it its own GPU plane while the game's swap chain keeps Independent Flip.
Only the handful of methods the HUD overlay needs are wrapped; vtable
offsets are fixed by the Windows SDK ABI.
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
