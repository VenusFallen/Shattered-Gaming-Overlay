"""dx11_bridge.py -- Direct3D 11 / DXGI COM interface wrappers for the HUD
overlay's DirectComposition swap chain and renderer.

Uses ctypes vtable calls only -- no extra dependencies beyond the Windows OS.
Only the exact methods the HUD overlay needs are implemented. Vtable offsets
are fixed by the Windows SDK ABI and do not change across D3D11 versions.
"""

from __future__ import annotations

import ctypes
from ctypes import (
    c_int, c_uint, c_float, c_void_p,
    POINTER, byref, Structure, c_ulong, c_ushort, c_ubyte,
)

_d3d11 = ctypes.windll.d3d11
_dxgi = ctypes.windll.dxgi

# ---------------------------------------------------------------------------
# HRESULT helper
# ---------------------------------------------------------------------------


def _check(hr: int, label: str = "D3D11") -> int:
    if hr < 0:
        raise OSError(f"{label} failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    return hr


# ---------------------------------------------------------------------------
# GUID
# ---------------------------------------------------------------------------


class GUID(Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]


def _guid(s: str) -> GUID:
    s = s.strip("{} ").replace("-", "")
    g = GUID()
    g.Data1 = int(s[0:8], 16)
    g.Data2 = int(s[8:12], 16)
    g.Data3 = int(s[12:16], 16)
    raw = bytes.fromhex(s[16:32])
    g.Data4 = (c_ubyte * 8)(*raw)
    return g


_IID_IDXGIFactory2 = _guid("{50c83a1c-e072-4c48-87b0-3630fa36a6d0}")
_IID_ID3D11Texture2D = _guid("{6f15aaf2-d208-4e89-9ab4-489535d34f9c}")
_IID_IDXGIDevice = _guid("{54ec77fa-1377-44e6-8c32-88fd5f44c84c}")


# ---------------------------------------------------------------------------
# COM vtable helper
# ---------------------------------------------------------------------------


def _com(ptr: int, idx: int, res_type, arg_types: list, *args):
    """Call the COM vtable method at index *idx* on the object at *ptr*.

    ptr -- raw integer value of an interface pointer
    idx -- zero-based vtable slot index
    """
    if not ptr:
        raise ValueError(f"_com called with null pointer (slot {idx})")
    vtbl = c_void_p.from_address(ptr).value
    fn_addr = c_void_p.from_address(vtbl + idx * 8).value
    if not fn_addr:
        raise ValueError(f"null function pointer at vtable slot {idx} on object {ptr:#x}")
    FT = ctypes.WINFUNCTYPE(res_type, c_void_p, *arg_types)
    return FT(fn_addr)(c_void_p(ptr), *args)


def _release(ptr: int) -> None:
    if ptr:
        _com(ptr, 2, c_uint, [])


def _query(ptr: int, iid: GUID) -> int:
    out = c_void_p(None)
    hr = _com(ptr, 0, c_int, [POINTER(GUID), POINTER(c_void_p)], byref(iid), byref(out))
    _check(hr, "QueryInterface")
    return out.value


# ---------------------------------------------------------------------------
# DXGI / D3D11 constants
# ---------------------------------------------------------------------------

_D3D_DRIVER_TYPE_HARDWARE = 1
_D3D_FEATURE_LEVEL_11_0 = 0xB000
_D3D11_SDK_VERSION = 7
_D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20  # required for B8G8R8A8 render targets (DComp path)

DXGI_FORMAT_B8G8R8A8_UNORM = 87  # DComp composition path uses BGRA
DXGI_USAGE_RT_OUTPUT = 0x00000020
DXGI_SWAP_EFFECT_FLIP_DISCARD = 4  # flip model -- used with DComp
DXGI_ALPHA_MODE_PREMULTIPLIED = 1  # requires CreateSwapChainForComposition
DXGI_SCALING_STRETCH = 0

DXGI_ERROR_DEVICE_REMOVED = 0x887A0005
DXGI_ERROR_DEVICE_RESET = 0x887A0007


# ---------------------------------------------------------------------------
# DXGI structures
# ---------------------------------------------------------------------------


class _SampleDesc(Structure):
    _fields_ = [("Count", c_uint), ("Quality", c_uint)]


class SwapChainDesc1(Structure):
    _fields_ = [
        ("Width", c_uint),
        ("Height", c_uint),
        ("Format", c_uint),
        ("Stereo", c_int),  # BOOL
        ("SampleDesc", _SampleDesc),
        ("BufferUsage", c_uint),
        ("BufferCount", c_uint),
        ("Scaling", c_uint),
        ("SwapEffect", c_uint),
        ("AlphaMode", c_uint),
        ("Flags", c_uint),
    ]


class D3D11Viewport(Structure):
    _fields_ = [
        ("TopLeftX", c_float),
        ("TopLeftY", c_float),
        ("Width", c_float),
        ("Height", c_float),
        ("MinDepth", c_float),
        ("MaxDepth", c_float),
    ]


def create_device() -> tuple[int, int]:
    """Create a hardware D3D11 device + immediate context (no swap chain).

    BGRA_SUPPORT flag is set -- required for B8G8R8A8 render targets used by
    DComp. Returns (ID3D11Device*, ID3D11DeviceContext*) as raw integer
    pointers.
    """
    _CreateDevice = _d3d11.D3D11CreateDevice
    _CreateDevice.restype = c_int
    _CreateDevice.argtypes = [
        c_void_p, c_uint, c_void_p, c_uint,
        POINTER(c_uint), c_uint, c_uint,
        POINTER(c_void_p), POINTER(c_uint), POINTER(c_void_p),
    ]
    device = c_void_p(None)
    context = c_void_p(None)
    feat_lvl = c_uint(0)
    levels = (c_uint * 1)(_D3D_FEATURE_LEVEL_11_0)
    hr = _CreateDevice(
        None, _D3D_DRIVER_TYPE_HARDWARE, None,
        _D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        levels, 1, _D3D11_SDK_VERSION,
        byref(device), byref(feat_lvl), byref(context),
    )
    _check(hr, "D3D11CreateDevice")
    return device.value, context.value


# ID3D11Device::GetDeviceRemovedReason -- vtable slot 39 (IUnknown 0-2 +
# ID3D11Device's own 40 methods, GetDeviceRemovedReason is the 37th of those).
_DEV_SLOT_GET_DEVICE_REMOVED_REASON = 39


def device_get_device_removed_reason(device: int) -> int:
    """Diagnostic only: query why the device was removed/reset. Only
    meaningful to call after a Present()/etc call fails with
    DXGI_ERROR_DEVICE_REMOVED or DXGI_ERROR_DEVICE_RESET."""
    return _com(device, _DEV_SLOT_GET_DEVICE_REMOVED_REASON, c_int, [])


_CreateDXGIFactory2 = _dxgi.CreateDXGIFactory2
_CreateDXGIFactory2.restype = c_int
_CreateDXGIFactory2.argtypes = [c_uint, POINTER(GUID), POINTER(c_void_p)]


def get_factory2() -> int:
    """Create an IDXGIFactory2 directly via CreateDXGIFactory2 (DXGI 1.3,
    Windows 8.1+). Returns IDXGIFactory2* (caller must Release).

    Simpler and more reliable than walking device -> IDXGIDevice -> adapter
    -> GetParent. D3D_DRIVER_TYPE_HARDWARE always uses adapter 0, which
    CreateDXGIFactory2 also enumerates first -- no adapter mismatch on
    single-GPU systems.
    """
    factory = c_void_p(None)
    hr = _CreateDXGIFactory2(0, byref(_IID_IDXGIFactory2), byref(factory))
    _check(hr, "CreateDXGIFactory2")
    return factory.value


def create_swap_chain_for_composition(factory2: int, device: int, width: int, height: int) -> int:
    """Create a flip-model swap chain suitable for DirectComposition.

    Format: B8G8R8A8_UNORM, premultiplied alpha, FLIP_DISCARD, 2 buffers.
    Returns IDXGISwapChain1* (caller must Release).
    """
    desc = SwapChainDesc1()
    desc.Width = width
    desc.Height = height
    desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM
    desc.Stereo = 0
    desc.SampleDesc.Count = 1
    desc.SampleDesc.Quality = 0
    desc.BufferUsage = DXGI_USAGE_RT_OUTPUT
    desc.BufferCount = 2  # flip model requires >= 2
    desc.Scaling = DXGI_SCALING_STRETCH
    desc.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD
    desc.AlphaMode = DXGI_ALPHA_MODE_PREMULTIPLIED
    desc.Flags = 0
    # IDXGIFactory2::CreateSwapChainForComposition -- vtable slot 24
    # IUnknown(0-2) + IDXGIObject(3-6) + IDXGIFactory(7-11) + IDXGIFactory1(12-13)
    # + IDXGIFactory2 own methods (14-24): IsWindowedStereoEnabled(14),
    # CreateSwapChainForHwnd(15), CreateSwapChainForCoreWindow(16),
    # GetSharedResourceAdapterLuid(17), RegisterStereoStatusWindow(18),
    # RegisterStereoStatusEvent(19), UnregisterStereoStatus(20),
    # RegisterOcclusionStatusWindow(21), RegisterOcclusionStatusEvent(22),
    # UnregisterOcclusionStatus(23), CreateSwapChainForComposition(24)
    swap_chain = c_void_p(None)
    hr = _com(
        factory2, 24, c_int,
        [c_void_p, POINTER(SwapChainDesc1), c_void_p, POINTER(c_void_p)],
        c_void_p(device), byref(desc), None, byref(swap_chain),
    )
    _check(hr, "CreateSwapChainForComposition")
    if not swap_chain.value:
        raise OSError("CreateSwapChainForComposition returned null (check vtable slot)")
    return swap_chain.value


# ---------------------------------------------------------------------------
# IDXGISwapChain vtable helpers
#   Present       -- slot 8
#   GetBuffer     -- slot 9
#   ResizeBuffers -- slot 13
# ---------------------------------------------------------------------------


def swap_present(swap_chain: int, sync_interval: int = 0, flags: int = 0) -> int:
    return _com(swap_chain, 8, c_int, [c_uint, c_uint], sync_interval, flags)


def swap_get_buffer(swap_chain: int) -> int:
    """Returns ID3D11Texture2D* for the back buffer (buffer index 0)."""
    tex = c_void_p(None)
    hr = _com(
        swap_chain, 9, c_int,
        [c_uint, POINTER(GUID), POINTER(c_void_p)],
        c_uint(0), byref(_IID_ID3D11Texture2D), byref(tex),
    )
    _check(hr, "GetBuffer")
    return tex.value


def swap_resize(swap_chain: int, width: int, height: int) -> None:
    """Resize swap chain buffers (call after WM_SIZE, after releasing RTV)."""
    # ResizeBuffers(BufferCount=0 -> keep, Width, Height, Format=0 -> keep, Flags)
    hr = _com(
        swap_chain, 13, c_int,
        [c_uint, c_uint, c_uint, c_uint, c_uint],
        0, width, height, 0, 0,
    )
    _check(hr, "ResizeBuffers")


# ---------------------------------------------------------------------------
# ID3D11Device::CreateRenderTargetView (slot 9)
# ---------------------------------------------------------------------------


def device_create_rtv(device: int, texture: int) -> int:
    """Create default RTV for *texture*. Returns ID3D11RenderTargetView*."""
    rtv = c_void_p(None)
    hr = _com(
        device, 9, c_int,
        [c_void_p, c_void_p, POINTER(c_void_p)],
        c_void_p(texture), None, byref(rtv),
    )
    _check(hr, "CreateRenderTargetView")
    return rtv.value


# ---------------------------------------------------------------------------
# ID3D11DeviceContext helpers
#   OMSetRenderTargets    -- slot 33
#   ClearRenderTargetView -- slot 50
#   RSSetViewports        -- slot 44
# ---------------------------------------------------------------------------


def ctx_set_rtv(context: int, rtv: int) -> None:
    rtv_arr = (c_void_p * 1)(rtv)
    _com(context, 33, None, [c_uint, POINTER(c_void_p * 1), c_void_p], 1, rtv_arr, None)


def ctx_clear_rtv(context: int, rtv: int, r: float, g: float, b: float, a: float) -> None:
    col = (c_float * 4)(r, g, b, a)
    _com(context, 50, None, [c_void_p, c_float * 4], c_void_p(rtv), col)


def ctx_set_viewport(context: int, width: float, height: float) -> None:
    vp = D3D11Viewport(0, 0, width, height, 0.0, 1.0)
    vp_arr = (D3D11Viewport * 1)(vp)
    _com(context, 44, None, [c_uint, POINTER(D3D11Viewport * 1)], 1, vp_arr)


# ---------------------------------------------------------------------------
# Convenience: create back-buffer RTV from swap chain
# ---------------------------------------------------------------------------


def make_rtv(device: int, swap_chain: int) -> int:
    """Get back buffer and create its RTV. Releases the texture immediately."""
    tex = swap_get_buffer(swap_chain)
    if not tex:
        raise OSError("GetBuffer returned null -- swap chain may not be initialised")
    try:
        rtv = device_create_rtv(device, tex)
    finally:
        _release(tex)
    return rtv
