"""overlay_renderer.py -- minimal DX11 2D geometry renderer for the HUD
overlay.

This first HUD overlay slice draws the accessibility crosshair only (lines
and circles) -- no text rendering, so unlike R9Tools' dx11_renderer.py this
module deliberately omits the GDI-backed text pipeline entirely. If a later
slice adds the stats HUD or module status indicators (out of scope for this
task, see .claude/agents/ui-agent.md's scoping note), that text pipeline can
be ported back in from R9Tools' dx11_renderer.py at that point.

Builds on dx11_bridge.py. Provides:
  * Renderer -- manages shaders, blend state, constant buffer, vertex buffer
  * draw_line          -- single line segment (as two triangles)
  * draw_circle        -- hollow circle (line loop)
  * draw_circle_filled -- filled circle (triangle fan)

All colors are (r, g, b, a) floats 0-1, NOT premultiplied by the caller --
the geometry pixel shader below premultiplies before writing to the
DirectComposition premultiplied-alpha swap chain (see dcomp_bridge.py).

Usage:
    r = Renderer(device, context, width, height)
    # each frame:
    r.begin()
    r.draw_line(x0, y0, x1, y1, thickness, col)
    r.draw_circle(cx, cy, radius, col, segments=64)
    r.end()
    # on resize:
    r.resize(new_w, new_h)
    # on shutdown:
    r.release()
"""

from __future__ import annotations

import ctypes
from ctypes import (
    c_int, c_uint, c_float, c_void_p, c_char_p,
    POINTER, byref, Structure, c_ubyte, c_size_t,
)
import math
import struct

import dx11_bridge as dx

# ---------------------------------------------------------------------------
# D3DCompile
# ---------------------------------------------------------------------------

_d3dcompiler = ctypes.windll.LoadLibrary("d3dcompiler_47.dll")
_D3DCompile = _d3dcompiler.D3DCompile
_D3DCompile.restype = c_int
_D3DCompile.argtypes = [
    c_void_p,  # pSrcData
    c_size_t,  # SrcDataSize
    c_char_p,  # pSourceName
    c_void_p,  # pDefines
    c_void_p,  # pInclude
    c_char_p,  # pEntrypoint
    c_char_p,  # pTarget
    c_uint,  # Flags1
    c_uint,  # Flags2
    POINTER(c_void_p),  # ppCode (ID3DBlob*)
    POINTER(c_void_p),  # ppErrorMsgs (ID3DBlob*)
]


def _blob_data(blob: int) -> bytes:
    """Return bytes from an ID3DBlob, then Release it.
    ID3DBlob vtable: IUnknown(0-2), GetBufferPointer=3, GetBufferSize=4."""
    ptr = dx._com(blob, 3, c_void_p, [])  # GetBufferPointer
    size = dx._com(blob, 4, c_size_t, [])  # GetBufferSize
    data = bytes((c_ubyte * size).from_address(ptr))
    dx._release(blob)
    return data


def _compile_shader(src: str, entry: str, target: str) -> bytes:
    src_b = src.encode("utf-8")
    code_blob = c_void_p(None)
    err_blob = c_void_p(None)
    hr = _D3DCompile(
        src_b, len(src_b), None, None, None,
        entry.encode(), target.encode(),
        0, 0,
        byref(code_blob), byref(err_blob),
    )
    if hr < 0:
        msg = ""
        if err_blob.value:
            msg = _blob_data(err_blob.value).decode(errors="replace")
        raise RuntimeError(f"Shader compile failed ({target}/{entry}): {msg}")
    if err_blob.value:
        dx._release(err_blob.value)
    return _blob_data(code_blob.value)


# ---------------------------------------------------------------------------
# Shaders -- colour-only geometry pipeline
# ---------------------------------------------------------------------------

_GEOM_VS_SRC = r"""
cbuffer CB : register(b0) {
    float2 invScreenSize;   // 1/w, 1/h
};
struct VSIn  { float2 pos : POSITION; float4 col : COLOR; };
struct VSOut { float4 pos : SV_POSITION; float4 col : COLOR; };
VSOut main(VSIn v) {
    VSOut o;
    // Map pixel coords [0..w, 0..h] -> NDC [-1..1, 1..-1]
    o.pos = float4(v.pos.x * invScreenSize.x * 2.0 - 1.0,
                   1.0 - v.pos.y * invScreenSize.y * 2.0,
                   0.0, 1.0);
    o.col = v.col;
    return o;
}
"""

_GEOM_PS_SRC = r"""
struct PSIn { float4 pos : SV_POSITION; float4 col : COLOR; };
float4 main(PSIn p) : SV_TARGET {
    // Premultiply alpha before writing to the DComp premultiplied-alpha swap chain.
    return float4(p.col.rgb * p.col.a, p.col.a);
}
"""


# ---------------------------------------------------------------------------
# D3D11 structures needed beyond dx11_bridge
# ---------------------------------------------------------------------------


class _InputElement(Structure):
    _fields_ = [
        ("SemanticName", c_char_p),
        ("SemanticIndex", c_uint),
        ("Format", c_uint),
        ("InputSlot", c_uint),
        ("AlignedByteOffset", c_uint),
        ("InputSlotClass", c_uint),
        ("InstanceDataStepRate", c_uint),
    ]


# DXGI_FORMAT constants
_FMT_R32G32_FLOAT = 16
_FMT_R32G32B32A32_FLOAT = 2

# D3D11_INPUT_CLASSIFICATION
_INPUT_PER_VERTEX = 0

# D3D11_BIND
_BIND_VERTEX_BUFFER = 0x1
_BIND_CONSTANT_BUFFER = 0x4

# D3D11_USAGE
_USAGE_DYNAMIC = 2

# D3D11_CPU_ACCESS
_CPU_WRITE = 0x10000

# D3D11_MAP
_MAP_WRITE_DISCARD = 4

# D3D11_PRIMITIVE_TOPOLOGY
_PRIM_TRIANGLELIST = 4

# D3D11_BLEND / D3D11_BLEND_OP
_BLEND_ONE = 2
_BLEND_INV_SRC_ALPHA = 6
_BLEND_OP_ADD = 1


class _BlendDesc(Structure):
    """Simplified D3D11_BLEND_DESC -- one render target, rest zero."""

    class _RT(Structure):
        _fields_ = [
            ("BlendEnable", c_int),
            ("SrcBlend", c_uint),
            ("DestBlend", c_uint),
            ("BlendOp", c_uint),
            ("SrcBlendAlpha", c_uint),
            ("DestBlendAlpha", c_uint),
            ("BlendOpAlpha", c_uint),
            ("RenderTargetWriteMask", c_ubyte),
        ]

    _fields_ = [
        ("AlphaToCoverageEnable", c_int),
        ("IndependentBlendEnable", c_int),
        ("RenderTarget", _RT * 8),
    ]


class _RastDesc(Structure):
    _fields_ = [
        ("FillMode", c_uint),
        ("CullMode", c_uint),
        ("FrontCounterClockwise", c_int),
        ("DepthBias", c_int),
        ("DepthBiasClamp", c_float),
        ("SlopeScaledDepthBias", c_float),
        ("DepthClipEnable", c_int),
        ("ScissorEnable", c_int),
        ("MultisampleEnable", c_int),
        ("AntialiasedLineEnable", c_int),
    ]


class _BufDesc(Structure):
    _fields_ = [
        ("ByteWidth", c_uint),
        ("Usage", c_uint),
        ("BindFlags", c_uint),
        ("CPUAccessFlags", c_uint),
        ("MiscFlags", c_uint),
        ("StructureByteStride", c_uint),
    ]


class _MappedSubresource(Structure):
    _fields_ = [
        ("pData", c_void_p),
        ("RowPitch", c_uint),
        ("DepthPitch", c_uint),
    ]


# ---------------------------------------------------------------------------
# Vertex struct: float2 pos + float4 col = 24 bytes
# ---------------------------------------------------------------------------

_GEOM_STRIDE = 24
_MAX_VERTS = 8192  # crosshair-only geometry is tiny; generous headroom regardless


def _pack_geom_verts(*verts) -> bytes:
    """verts: sequence of (x, y, r, g, b, a) tuples."""
    return b"".join(struct.pack("6f", *v) for v in verts)


# ---------------------------------------------------------------------------
# Device/context vtable slot indices (subset needed here)
# ---------------------------------------------------------------------------

_CTX_SLOTS = {
    "Draw": 13,
    "Map": 14,
    "Unmap": 15,
    "IASetInputLayout": 17,
    "IASetVertexBuffers": 18,
    "IASetPrimitiveTopology": 24,
    "VSSetShader": 11,
    "PSSetShader": 9,
    "OMSetBlendState": 35,
    "RSSetState": 43,
    "VSSetConstantBuffers": 7,
}

_DEV_SLOTS = {
    "CreateBuffer": 3,
    "CreateInputLayout": 11,
    "CreateVertexShader": 12,
    "CreatePixelShader": 15,
    "CreateBlendState": 20,
    "CreateRasterizerState": 22,
}


def _ctx(ctx: int, name: str, res_type, arg_types: list, *args):
    return dx._com(ctx, _CTX_SLOTS[name], res_type, arg_types, *args)


def _dev(dev: int, name: str, res_type, arg_types: list, *args):
    return dx._com(dev, _DEV_SLOTS[name], res_type, arg_types, *args)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class Renderer:
    """DX11 2D line/circle renderer for the crosshair.

    Thread-safety: call all draw_* methods from the same thread that created
    it (the HUD overlay's own background render thread -- see hud_overlay.py).
    """

    def __init__(self, device: int, context: int, width: int, height: int):
        self._dev = device
        self._ctx = context
        self._w = width
        self._h = height

        self._geom_vs = 0
        self._geom_ps = 0
        self._geom_il = 0
        self._geom_vb = 0
        self._cb0 = 0  # invScreenSize constant buffer
        self._blend = 0
        self._rast = 0

        self._pending_geom: list[bytes] = []

        self._build_shaders()
        self._build_states()
        self._build_buffers()

    # ------------------------------------------------------------------
    # Internal build helpers
    # ------------------------------------------------------------------

    def _build_shaders(self):
        dev = self._dev

        gvs_bc = _compile_shader(_GEOM_VS_SRC, "main", "vs_4_0")
        gps_bc = _compile_shader(_GEOM_PS_SRC, "main", "ps_4_0")

        vs = c_void_p(None)
        hr = _dev(dev, "CreateVertexShader", c_int,
                  [c_void_p, c_size_t, c_void_p, POINTER(c_void_p)],
                  gvs_bc, len(gvs_bc), None, byref(vs))
        dx._check(hr, "CreateVertexShader")
        self._geom_vs = vs.value

        ps = c_void_p(None)
        hr = _dev(dev, "CreatePixelShader", c_int,
                  [c_void_p, c_size_t, c_void_p, POINTER(c_void_p)],
                  gps_bc, len(gps_bc), None, byref(ps))
        dx._check(hr, "CreatePixelShader")
        self._geom_ps = ps.value

        # Input layout: POSITION float2 + COLOR float4
        geom_elems = (_InputElement * 2)(
            _InputElement(b"POSITION", 0, _FMT_R32G32_FLOAT, 0, 0, _INPUT_PER_VERTEX, 0),
            _InputElement(b"COLOR", 0, _FMT_R32G32B32A32_FLOAT, 0, 8, _INPUT_PER_VERTEX, 0),
        )
        il = c_void_p(None)
        hr = _dev(dev, "CreateInputLayout", c_int,
                  [POINTER(_InputElement * 2), c_uint, c_void_p, c_size_t, POINTER(c_void_p)],
                  geom_elems, 2, gvs_bc, len(gvs_bc), byref(il))
        dx._check(hr, "CreateInputLayout (geom)")
        self._geom_il = il.value

    def _build_states(self):
        dev = self._dev

        # Blend: premultiplied alpha -- the geometry PS already premultiplies.
        # src=ONE preserves the premultiplied value; dst=INV_SRC_ALPHA composites correctly.
        bd = _BlendDesc()
        bd.AlphaToCoverageEnable = 0
        bd.IndependentBlendEnable = 0
        rt = bd.RenderTarget[0]
        rt.BlendEnable = 1
        rt.SrcBlend = _BLEND_ONE
        rt.DestBlend = _BLEND_INV_SRC_ALPHA
        rt.BlendOp = _BLEND_OP_ADD
        rt.SrcBlendAlpha = _BLEND_ONE
        rt.DestBlendAlpha = _BLEND_INV_SRC_ALPHA
        rt.BlendOpAlpha = _BLEND_OP_ADD
        rt.RenderTargetWriteMask = 0x0F
        bs = c_void_p(None)
        hr = _dev(dev, "CreateBlendState", c_int,
                  [POINTER(_BlendDesc), POINTER(c_void_p)],
                  byref(bd), byref(bs))
        dx._check(hr, "CreateBlendState")
        self._blend = bs.value

        # Rasterizer: solid, no cull, no depth clip
        rd = _RastDesc()
        rd.FillMode = 3  # D3D11_FILL_SOLID
        rd.CullMode = 1  # D3D11_CULL_NONE
        rd.FrontCounterClockwise = 0
        rd.DepthBias = 0
        rd.DepthBiasClamp = 0.0
        rd.SlopeScaledDepthBias = 0.0
        rd.DepthClipEnable = 0
        rd.ScissorEnable = 0
        rd.MultisampleEnable = 0
        rd.AntialiasedLineEnable = 0
        rs = c_void_p(None)
        hr = _dev(dev, "CreateRasterizerState", c_int,
                  [POINTER(_RastDesc), POINTER(c_void_p)],
                  byref(rd), byref(rs))
        dx._check(hr, "CreateRasterizerState")
        self._rast = rs.value

    def _build_buffers(self):
        dev = self._dev

        bd = _BufDesc()
        bd.ByteWidth = _GEOM_STRIDE * _MAX_VERTS
        bd.Usage = _USAGE_DYNAMIC
        bd.BindFlags = _BIND_VERTEX_BUFFER
        bd.CPUAccessFlags = _CPU_WRITE
        vb = c_void_p(None)
        hr = _dev(dev, "CreateBuffer", c_int,
                  [POINTER(_BufDesc), c_void_p, POINTER(c_void_p)],
                  byref(bd), None, byref(vb))
        dx._check(hr, "CreateBuffer (VB)")
        self._geom_vb = vb.value

        # Constant buffer 0: float2 invScreenSize (padded to 16 bytes)
        cbd = _BufDesc()
        cbd.ByteWidth = 16  # min 16-byte alignment
        cbd.Usage = _USAGE_DYNAMIC
        cbd.BindFlags = _BIND_CONSTANT_BUFFER
        cbd.CPUAccessFlags = _CPU_WRITE
        cb0 = c_void_p(None)
        hr = _dev(dev, "CreateBuffer", c_int,
                  [POINTER(_BufDesc), c_void_p, POINTER(c_void_p)],
                  byref(cbd), None, byref(cb0))
        dx._check(hr, "CreateBuffer (CB0)")
        self._cb0 = cb0.value

        self._update_cb0()

    def _update_cb0(self):
        data = struct.pack("4f", 1.0 / self._w, 1.0 / self._h, 0.0, 0.0)
        self._map_write(self._cb0, data)

    def _map_write(self, buf: int, data: bytes):
        ctx = self._ctx
        mapped = _MappedSubresource()
        hr = _ctx(ctx, "Map", c_int,
                  [c_void_p, c_uint, c_uint, c_uint, POINTER(_MappedSubresource)],
                  c_void_p(buf), 0, _MAP_WRITE_DISCARD, 0, byref(mapped))
        dx._check(hr, "Map")
        ctypes.memmove(mapped.pData, data, len(data))
        _ctx(ctx, "Unmap", None, [c_void_p, c_uint], c_void_p(buf), 0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resize(self, width: int, height: int):
        self._w = width
        self._h = height
        self._update_cb0()

    def begin(self):
        """Set up render state. Call once per frame before draw_* methods."""
        ctx = self._ctx
        _ctx(ctx, "OMSetBlendState", None,
             [c_void_p, c_void_p, c_uint],
             c_void_p(self._blend), None, 0xFFFFFFFF)
        _ctx(ctx, "RSSetState", None, [c_void_p], c_void_p(self._rast))
        cb_arr = (c_void_p * 1)(self._cb0)
        _ctx(ctx, "VSSetConstantBuffers", None,
             [c_uint, c_uint, POINTER(c_void_p * 1)],
             0, 1, cb_arr)
        self._pending_geom = []

    def end(self):
        """Flush all batched geometry to the GPU."""
        self._flush_geom()

    # ------------------------------------------------------------------
    # Geometry draw calls (batched)
    # ------------------------------------------------------------------

    def draw_line(self, x0: float, y0: float, x1: float, y1: float, thickness: float, col: tuple):
        """Draw a line segment as a rectangle (two triangles)."""
        r, g, b, a = col
        dx_ = x1 - x0
        dy_ = y1 - y0
        length = math.hypot(dx_, dy_)
        if length < 0.001:
            return
        nx = -dy_ / length * (thickness * 0.5)
        ny = dx_ / length * (thickness * 0.5)
        ax, ay = x0 + nx, y0 + ny
        bx, by = x0 - nx, y0 - ny
        cx_, cy = x1 + nx, y1 + ny
        dx2, dy2 = x1 - nx, y1 - ny
        verts = _pack_geom_verts(
            (ax, ay, r, g, b, a), (cx_, cy, r, g, b, a), (bx, by, r, g, b, a),
            (bx, by, r, g, b, a), (cx_, cy, r, g, b, a), (dx2, dy2, r, g, b, a),
        )
        self._pending_geom.append(verts)

    def draw_circle(self, cx: float, cy: float, radius: float,
                     col: tuple, thickness: float = 1.5, segments: int = 48):
        """Hollow circle."""
        step = 2.0 * math.pi / segments
        for i in range(segments):
            a0 = i * step
            a1 = (i + 1) * step
            x0 = cx + math.cos(a0) * radius
            y0 = cy + math.sin(a0) * radius
            x1 = cx + math.cos(a1) * radius
            y1 = cy + math.sin(a1) * radius
            self.draw_line(x0, y0, x1, y1, thickness, col)

    def draw_circle_filled(self, cx: float, cy: float, radius: float, col: tuple, segments: int = 48):
        """Filled circle as a triangle fan."""
        r, g, b, a = col
        step = 2.0 * math.pi / segments
        verts = []
        for i in range(segments):
            a0 = i * step
            a1 = (i + 1) * step
            verts.append((cx, cy, r, g, b, a))
            verts.append((cx + math.cos(a0) * radius, cy + math.sin(a0) * radius, r, g, b, a))
            verts.append((cx + math.cos(a1) * radius, cy + math.sin(a1) * radius, r, g, b, a))
        self._pending_geom.append(_pack_geom_verts(*verts))

    # ------------------------------------------------------------------
    # Flush helper (called by end())
    # ------------------------------------------------------------------

    def _flush_geom(self):
        if not self._pending_geom:
            return
        ctx = self._ctx
        data = b"".join(self._pending_geom)
        n_verts = len(data) // _GEOM_STRIDE
        if n_verts == 0:
            return

        self._map_write(self._geom_vb, data[: _GEOM_STRIDE * min(n_verts, _MAX_VERTS)])

        _ctx(ctx, "IASetInputLayout", None, [c_void_p], c_void_p(self._geom_il))
        stride = c_uint(_GEOM_STRIDE)
        offset = c_uint(0)
        vb_arr = (c_void_p * 1)(self._geom_vb)
        _ctx(ctx, "IASetVertexBuffers", None,
             [c_uint, c_uint, POINTER(c_void_p * 1), POINTER(c_uint), POINTER(c_uint)],
             0, 1, vb_arr, byref(stride), byref(offset))
        _ctx(ctx, "IASetPrimitiveTopology", None, [c_uint], _PRIM_TRIANGLELIST)
        _ctx(ctx, "VSSetShader", None, [c_void_p, c_void_p, c_uint],
             c_void_p(self._geom_vs), None, 0)
        _ctx(ctx, "PSSetShader", None, [c_void_p, c_void_p, c_uint],
             c_void_p(self._geom_ps), None, 0)
        _ctx(ctx, "Draw", None, [c_uint, c_uint], min(n_verts, _MAX_VERTS), 0)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def release(self):
        for attr in ("_geom_vs", "_geom_ps", "_geom_il", "_geom_vb", "_cb0", "_blend", "_rast"):
            v = getattr(self, attr, 0)
            if v:
                dx._release(v)
                setattr(self, attr, 0)
