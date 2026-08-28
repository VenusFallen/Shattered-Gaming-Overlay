"""overlay_renderer.py -- DX11 2D geometry + text renderer for the HUD
overlay.

The first HUD overlay slice (the accessibility crosshair) was geometry-only
by design -- this module's own docstring used to say so, and pointed at
R9Tools' dx11_renderer.py as the place to port the GDI-backed text pipeline
back in from once the Stats HUD / module status indicators were built. That
text pipeline (GDI renders a string to an in-memory bitmap -> R8_UNORM D3D11
texture -> SRV -> textured quad) is now ported in below, adapted only where
this file's existing geometry pipeline needed matching additions (rounded
rect fill/outline for the Stats box, used nowhere in R9Tools). Everything
else in the GDI/text section (`_gdi_text_to_srv`, `_gdi_measure_text`, the
text VS/PS shaders, `draw_text`/`measure_text`) is a straight port, not a
redesign -- R9Tools already solved the GDI/DX11 interop.

Builds on dx11_bridge.py. Provides:
  * Renderer -- manages shaders, blend state, constant buffers, vertex buffers
  * draw_line                -- single line segment (as two triangles)
  * draw_rect / draw_rect_filled -- axis-aligned rectangle, hollow/filled
  * draw_rounded_rect / draw_rounded_rect_filled -- rounded rectangle (Stats box)
  * draw_circle / draw_circle_filled -- circle, hollow/filled (crosshair, badges)
  * draw_text / measure_text -- GDI-backed text rendering (Stats box, badges)

All colors are (r, g, b, a) floats 0-1, NOT premultiplied by the caller --
the pixel shaders below premultiply before writing to the DirectComposition
premultiplied-alpha swap chain (see dcomp_bridge.py).

Usage:
    r = Renderer(device, context, width, height)
    # each frame:
    r.begin()
    r.draw_line(x0, y0, x1, y1, thickness, col)
    r.draw_circle(cx, cy, radius, col, segments=64)
    r.draw_text("42", x, y, col, font_size=18)
    r.end()
    # on resize:
    r.resize(new_w, new_h)
    # on shutdown:
    r.release()
"""

from __future__ import annotations

import ctypes
from ctypes import (
    c_int, c_uint, c_float, c_void_p, c_char_p, c_wchar_p,
    POINTER, byref, Structure, c_ubyte, c_ulong, c_ushort, c_size_t,
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
# Shaders -- colour-only geometry pipeline + textured (text) pipeline
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

_TEXT_VS_SRC = r"""
cbuffer CB : register(b0) {
    float2 invScreenSize;
};
struct VSIn  { float2 pos : POSITION; float2 uv : TEXCOORD; };
struct VSOut { float4 pos : SV_POSITION; float2 uv : TEXCOORD; };
VSOut main(VSIn v) {
    VSOut o;
    o.pos = float4(v.pos.x * invScreenSize.x * 2.0 - 1.0,
                   1.0 - v.pos.y * invScreenSize.y * 2.0,
                   0.0, 1.0);
    o.uv  = v.uv;
    return o;
}
"""

_TEXT_PS_SRC = r"""
Texture2D    tex : register(t0);
SamplerState sam : register(s0);
cbuffer CB2 : register(b1) {
    float4 textColor;
};
struct PSIn { float4 pos : SV_POSITION; float2 uv : TEXCOORD; };
float4 main(PSIn p) : SV_TARGET {
    float alpha = tex.Sample(sam, p.uv).r;   // grayscale glyph
    float4 c = textColor * alpha;             // premultiply
    return c;
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
_FMT_R8_UNORM = 61

# D3D11_INPUT_CLASSIFICATION
_INPUT_PER_VERTEX = 0

# D3D11_BIND
_BIND_VERTEX_BUFFER = 0x1
_BIND_CONSTANT_BUFFER = 0x4
_BIND_SHADER_RESOURCE = 0x8

# D3D11_USAGE
_USAGE_DEFAULT = 0
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


class _SubresData(Structure):
    _fields_ = [
        ("pSysMem", c_void_p),
        ("SysMemPitch", c_uint),
        ("SysMemSlicePitch", c_uint),
    ]


class _Tex2DDesc(Structure):
    _fields_ = [
        ("Width", c_uint),
        ("Height", c_uint),
        ("MipLevels", c_uint),
        ("ArraySize", c_uint),
        ("Format", c_uint),
        ("SampleDesc", dx._SampleDesc),
        ("Usage", c_uint),
        ("BindFlags", c_uint),
        ("CPUAccessFlags", c_uint),
        ("MiscFlags", c_uint),
    ]


class _SamplerDesc(Structure):
    _fields_ = [
        ("Filter", c_uint),  # D3D11_FILTER_MIN_MAG_MIP_LINEAR = 21
        ("AddressU", c_uint),  # WRAP=1 CLAMP=3
        ("AddressV", c_uint),
        ("AddressW", c_uint),
        ("MipLODBias", c_float),
        ("MaxAnisotropy", c_uint),
        ("ComparisonFunc", c_uint),
        ("BorderColor", c_float * 4),
        ("MinLOD", c_float),
        ("MaxLOD", c_float),
    ]


class _MappedSubresource(Structure):
    _fields_ = [
        ("pData", c_void_p),
        ("RowPitch", c_uint),
        ("DepthPitch", c_uint),
    ]


# ---------------------------------------------------------------------------
# Vertex structs: geom = float2 pos + float4 col = 24 bytes
#                 text = float2 pos + float2 uv  = 16 bytes
# ---------------------------------------------------------------------------

_GEOM_STRIDE = 24
_TEXT_STRIDE = 16
_MAX_VERTS = 16384  # generous headroom for the crosshair + Stats box + badges


def _pack_geom_verts(*verts) -> bytes:
    """verts: sequence of (x, y, r, g, b, a) tuples."""
    return b"".join(struct.pack("6f", *v) for v in verts)


def _pack_text_verts(*verts) -> bytes:
    """verts: sequence of (x, y, u, v) tuples."""
    return b"".join(struct.pack("4f", *v) for v in verts)


# ---------------------------------------------------------------------------
# Device/context vtable slot indices (subset needed here)
# ---------------------------------------------------------------------------

_CTX_SLOTS = {
    "VSSetConstantBuffers": 7,
    "PSSetShaderResources": 8,
    "PSSetShader": 9,
    "PSSetSamplers": 10,
    "VSSetShader": 11,
    "Draw": 13,
    "Map": 14,
    "Unmap": 15,
    "PSSetConstantBuffers": 16,
    "IASetInputLayout": 17,
    "IASetVertexBuffers": 18,
    "IASetPrimitiveTopology": 24,
    "OMSetBlendState": 35,
    "RSSetState": 43,
}

_DEV_SLOTS = {
    "CreateBuffer": 3,
    "CreateTexture2D": 5,
    "CreateShaderResourceView": 7,
    "CreateInputLayout": 11,
    "CreateVertexShader": 12,
    "CreatePixelShader": 15,
    "CreateBlendState": 20,
    "CreateRasterizerState": 22,
    "CreateSamplerState": 23,
}


def _ctx(ctx: int, name: str, res_type, arg_types: list, *args):
    return dx._com(ctx, _CTX_SLOTS[name], res_type, arg_types, *args)


def _dev(dev: int, name: str, res_type, arg_types: list, *args):
    return dx._com(dev, _DEV_SLOTS[name], res_type, arg_types, *args)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class Renderer:
    """DX11 2D geometry + text renderer for the HUD overlay (crosshair, Stats
    box, module status badges).

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
        self._text_vs = 0
        self._text_ps = 0
        self._text_il = 0
        self._text_vb = 0
        self._cb0 = 0  # invScreenSize constant buffer (shared VS)
        self._cb1 = 0  # textColor constant buffer (text PS)
        self._blend = 0
        self._rast = 0
        self._sampler = 0

        self._pending_geom: list[bytes] = []
        self._pending_text: list = []  # (vb_bytes, srv, col4)

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
        tvs_bc = _compile_shader(_TEXT_VS_SRC, "main", "vs_4_0")
        tps_bc = _compile_shader(_TEXT_PS_SRC, "main", "ps_4_0")

        def make_vs(bc):
            vs = c_void_p(None)
            hr = _dev(dev, "CreateVertexShader", c_int,
                      [c_void_p, c_size_t, c_void_p, POINTER(c_void_p)],
                      bc, len(bc), None, byref(vs))
            dx._check(hr, "CreateVertexShader")
            return vs.value

        def make_ps(bc):
            ps = c_void_p(None)
            hr = _dev(dev, "CreatePixelShader", c_int,
                      [c_void_p, c_size_t, c_void_p, POINTER(c_void_p)],
                      bc, len(bc), None, byref(ps))
            dx._check(hr, "CreatePixelShader")
            return ps.value

        self._geom_vs = make_vs(gvs_bc)
        self._geom_ps = make_ps(gps_bc)
        self._text_vs = make_vs(tvs_bc)
        self._text_ps = make_ps(tps_bc)

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

        # Input layout: POSITION float2 + TEXCOORD float2
        text_elems = (_InputElement * 2)(
            _InputElement(b"POSITION", 0, _FMT_R32G32_FLOAT, 0, 0, _INPUT_PER_VERTEX, 0),
            _InputElement(b"TEXCOORD", 0, _FMT_R32G32_FLOAT, 0, 8, _INPUT_PER_VERTEX, 0),
        )
        il2 = c_void_p(None)
        hr = _dev(dev, "CreateInputLayout", c_int,
                  [POINTER(_InputElement * 2), c_uint, c_void_p, c_size_t, POINTER(c_void_p)],
                  text_elems, 2, tvs_bc, len(tvs_bc), byref(il2))
        dx._check(hr, "CreateInputLayout (text)")
        self._text_il = il2.value

    def _build_states(self):
        dev = self._dev

        # Blend: premultiplied alpha -- both PS variants already premultiply.
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

        # Sampler: linear, clamp -- used to sample glyph textures
        sd = _SamplerDesc()
        sd.Filter = 21  # D3D11_FILTER_MIN_MAG_MIP_LINEAR
        sd.AddressU = 3  # CLAMP
        sd.AddressV = 3
        sd.AddressW = 3
        sd.MipLODBias = 0.0
        sd.MaxAnisotropy = 1
        sd.ComparisonFunc = 1  # NEVER
        sd.MinLOD = -3.402823466e+38
        sd.MaxLOD = 3.402823466e+38
        smp = c_void_p(None)
        hr = _dev(dev, "CreateSamplerState", c_int,
                  [POINTER(_SamplerDesc), POINTER(c_void_p)],
                  byref(sd), byref(smp))
        dx._check(hr, "CreateSamplerState")
        self._sampler = smp.value

    def _build_buffers(self):
        dev = self._dev

        def make_vb(stride, max_verts):
            bd = _BufDesc()
            bd.ByteWidth = stride * max_verts
            bd.Usage = _USAGE_DYNAMIC
            bd.BindFlags = _BIND_VERTEX_BUFFER
            bd.CPUAccessFlags = _CPU_WRITE
            vb = c_void_p(None)
            hr = _dev(dev, "CreateBuffer", c_int,
                      [POINTER(_BufDesc), c_void_p, POINTER(c_void_p)],
                      byref(bd), None, byref(vb))
            dx._check(hr, "CreateBuffer (VB)")
            return vb.value

        self._geom_vb = make_vb(_GEOM_STRIDE, _MAX_VERTS)
        self._text_vb = make_vb(_TEXT_STRIDE, 6)  # one glyph quad at a time

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

        # Constant buffer 1: float4 textColor
        cb1 = c_void_p(None)
        hr = _dev(dev, "CreateBuffer", c_int,
                  [POINTER(_BufDesc), c_void_p, POINTER(c_void_p)],
                  byref(cbd), None, byref(cb1))
        dx._check(hr, "CreateBuffer (CB1)")
        self._cb1 = cb1.value

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
        self._pending_text = []

    def end(self):
        """Flush all batched geometry + text to the GPU."""
        self._flush_geom()
        self._flush_text()

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

    def draw_rect(self, x: float, y: float, w: float, h: float, thickness: float, col: tuple):
        """Hollow axis-aligned rectangle."""
        self.draw_line(x, y, x + w, y, thickness, col)
        self.draw_line(x + w, y, x + w, y + h, thickness, col)
        self.draw_line(x + w, y + h, x, y + h, thickness, col)
        self.draw_line(x, y + h, x, y, thickness, col)

    def draw_rect_filled(self, x: float, y: float, w: float, h: float, col: tuple):
        """Filled axis-aligned rectangle (two triangles)."""
        r, g, b, a = col
        verts = _pack_geom_verts(
            (x, y, r, g, b, a), (x + w, y, r, g, b, a), (x, y + h, r, g, b, a),
            (x, y + h, r, g, b, a), (x + w, y, r, g, b, a), (x + w, y + h, r, g, b, a),
        )
        self._pending_geom.append(verts)

    def _rounded_rect_points(self, x: float, y: float, w: float, h: float,
                              radius: float, segments_per_corner: int = 8) -> list:
        """Perimeter points (clockwise) of an axis-aligned rounded rect,
        used by both the filled (triangle-fan) and hollow (line-loop)
        variants below -- no equivalent existed in R9Tools since it never
        needed a rounded rect; this is new, built to match the style of the
        existing draw_circle/draw_circle_filled helpers."""
        r = max(0.0, min(radius, w * 0.5, h * 0.5))
        corners = [
            (x + w - r, y + r, 270.0, 360.0),  # top-right
            (x + w - r, y + h - r, 0.0, 90.0),  # bottom-right
            (x + r, y + h - r, 90.0, 180.0),  # bottom-left
            (x + r, y + r, 180.0, 270.0),  # top-left
        ]
        pts = []
        for ccx, ccy, a0, a1 in corners:
            if r <= 0.001:
                pts.append((ccx, ccy))
                continue
            for i in range(segments_per_corner + 1):
                a = math.radians(a0 + (a1 - a0) * i / segments_per_corner)
                pts.append((ccx + math.cos(a) * r, ccy + math.sin(a) * r))
        return pts

    def draw_rounded_rect_filled(self, x: float, y: float, w: float, h: float,
                                  radius: float, col: tuple, segments_per_corner: int = 8):
        """Filled rounded rectangle -- the Stats box's plain card background.
        Triangle-fanned from the rect's own center, same technique
        draw_circle_filled uses (valid here since a rounded rect is convex
        and its center is always interior for radius <= min(w,h)/2)."""
        pts = self._rounded_rect_points(x, y, w, h, radius, segments_per_corner)
        if len(pts) < 3:
            return
        cx, cy = x + w * 0.5, y + h * 0.5
        r, g, b, a = col
        verts = []
        n = len(pts)
        for i in range(n):
            p0 = pts[i]
            p1 = pts[(i + 1) % n]
            verts.append((cx, cy, r, g, b, a))
            verts.append((p0[0], p0[1], r, g, b, a))
            verts.append((p1[0], p1[1], r, g, b, a))
        self._pending_geom.append(_pack_geom_verts(*verts))

    def draw_rounded_rect(self, x: float, y: float, w: float, h: float, radius: float,
                           col: tuple, thickness: float = 1.5, segments_per_corner: int = 8):
        """Hollow rounded rectangle outline -- the Stats box's subtle border."""
        pts = self._rounded_rect_points(x, y, w, h, radius, segments_per_corner)
        n = len(pts)
        for i in range(n):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % n]
            self.draw_line(x0, y0, x1, y1, thickness, col)

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
    # Text rendering (ported from R9Tools' dx11_renderer.py -- see module
    # docstring)
    # ------------------------------------------------------------------

    def measure_text(self, text: str, font_size: int = 14, font_face: str = "Segoe UI") -> tuple:
        """Measure (width, height) in pixels without drawing -- used to
        center/size the Stats box and badge labels before draw_text()."""
        if not text:
            return 0, 0
        try:
            return _gdi_measure_text(text, font_size, font_face)
        except Exception:
            return 0, 0

    def draw_text(self, text: str, x: float, y: float, col: tuple,
                  font_size: int = 14, font_face: str = "Segoe UI") -> int:
        """Render *text* using GDI into a one-channel texture, then draw it
        as a textured quad. col is (r,g,b,a) straight (non-premultiplied) --
        the text pixel shader premultiplies by the glyph alpha itself.
        Returns the pixel width of the rendered text."""
        if not text:
            return 0
        try:
            srv, tw, th = _gdi_text_to_srv(self._dev, text, font_size, font_face)
        except Exception:
            return 0
        x1, y1 = x + tw, y + th
        verts = _pack_text_verts(
            (x, y, 0.0, 0.0),
            (x1, y, 1.0, 0.0),
            (x, y1, 0.0, 1.0),
            (x, y1, 0.0, 1.0),
            (x1, y, 1.0, 0.0),
            (x1, y1, 1.0, 1.0),
        )
        self._pending_text.append((verts, srv, col))
        return tw

    # ------------------------------------------------------------------
    # Flush helpers (called by end())
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

    def _flush_text(self):
        if not self._pending_text:
            return
        ctx = self._ctx
        _ctx(ctx, "IASetInputLayout", None, [c_void_p], c_void_p(self._text_il))
        stride = c_uint(_TEXT_STRIDE)
        offset = c_uint(0)
        vb_arr = (c_void_p * 1)(self._text_vb)
        _ctx(ctx, "IASetVertexBuffers", None,
             [c_uint, c_uint, POINTER(c_void_p * 1), POINTER(c_uint), POINTER(c_uint)],
             0, 1, vb_arr, byref(stride), byref(offset))
        _ctx(ctx, "IASetPrimitiveTopology", None, [c_uint], _PRIM_TRIANGLELIST)
        _ctx(ctx, "VSSetShader", None, [c_void_p, c_void_p, c_uint],
             c_void_p(self._text_vs), None, 0)
        _ctx(ctx, "PSSetShader", None, [c_void_p, c_void_p, c_uint],
             c_void_p(self._text_ps), None, 0)
        smp_arr = (c_void_p * 1)(self._sampler)
        _ctx(ctx, "PSSetSamplers", None,
             [c_uint, c_uint, POINTER(c_void_p * 1)],
             0, 1, smp_arr)
        cb_arr1 = (c_void_p * 1)(self._cb1)
        _ctx(ctx, "PSSetConstantBuffers", None,
             [c_uint, c_uint, POINTER(c_void_p * 1)],
             1, 1, cb_arr1)

        for verts, srv, col in self._pending_text:
            self._map_write(self._cb1, struct.pack("4f", *col))
            self._map_write(self._text_vb, verts)
            srv_arr = (c_void_p * 1)(srv)
            _ctx(ctx, "PSSetShaderResources", None,
                 [c_uint, c_uint, POINTER(c_void_p * 1)],
                 0, 1, srv_arr)
            _ctx(ctx, "Draw", None, [c_uint, c_uint], 6, 0)
            # Release per-frame SRV (texture was created just for this frame)
            dx._release(srv)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def release(self):
        for attr in ("_geom_vs", "_geom_ps", "_text_vs", "_text_ps",
                     "_geom_il", "_text_il", "_geom_vb", "_text_vb",
                     "_cb0", "_cb1", "_blend", "_rast", "_sampler"):
            v = getattr(self, attr, 0)
            if v:
                dx._release(v)
                setattr(self, attr, 0)


# ---------------------------------------------------------------------------
# GDI text -> D3D11 R8_UNORM texture SRV (ported near-verbatim from R9Tools'
# dx11_renderer.py -- see module docstring)
# ---------------------------------------------------------------------------

_gdi32 = ctypes.windll.gdi32
_user32 = ctypes.windll.user32

_CreateCompatibleDC = _gdi32.CreateCompatibleDC
_CreateCompatibleDC.restype = c_void_p
_CreateCompatibleDC.argtypes = [c_void_p]

_DeleteDC = _gdi32.DeleteDC
_DeleteDC.restype = c_int
_DeleteDC.argtypes = [c_void_p]

_CreateDIBSection = _gdi32.CreateDIBSection
_CreateDIBSection.restype = c_void_p
_CreateDIBSection.argtypes = [c_void_p, c_void_p, c_uint,
                               POINTER(c_void_p), c_void_p, c_uint]

_DeleteObject = _gdi32.DeleteObject
_DeleteObject.restype = c_int
_DeleteObject.argtypes = [c_void_p]

_SelectObject = _gdi32.SelectObject
_SelectObject.restype = c_void_p
_SelectObject.argtypes = [c_void_p, c_void_p]

_SetBkMode = _gdi32.SetBkMode
_SetBkMode.restype = c_int
_SetBkMode.argtypes = [c_void_p, c_int]

_SetTextColor = _gdi32.SetTextColor
_SetTextColor.restype = c_ulong
_SetTextColor.argtypes = [c_void_p, c_ulong]

_TextOutW = _gdi32.TextOutW
_TextOutW.restype = c_int
_TextOutW.argtypes = [c_void_p, c_int, c_int, c_wchar_p, c_int]

_GetTextExtentPoint32W = _gdi32.GetTextExtentPoint32W
_GetTextExtentPoint32W.restype = c_int
_GetTextExtentPoint32W.argtypes = [c_void_p, c_wchar_p, c_int, c_void_p]

_CreateFontW = _gdi32.CreateFontW
_CreateFontW.restype = c_void_p
_CreateFontW.argtypes = [c_int, c_int, c_int, c_int, c_int,
                          c_uint, c_uint, c_uint, c_uint, c_uint,
                          c_uint, c_uint, c_uint, c_wchar_p]


class _SIZE(Structure):
    _fields_ = [("cx", c_int), ("cy", c_int)]


class _BITMAPINFOHEADER(Structure):
    _fields_ = [
        ("biSize", c_uint),
        ("biWidth", c_int),
        ("biHeight", c_int),
        ("biPlanes", c_ushort),
        ("biBitCount", c_ushort),
        ("biCompression", c_uint),
        ("biSizeImage", c_uint),
        ("biXPelsPerMeter", c_int),
        ("biYPelsPerMeter", c_int),
        ("biClrUsed", c_uint),
        ("biClrImportant", c_uint),
    ]


class _BITMAPINFO(Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", c_uint * 3)]


def _gdi_measure_text(text: str, font_size: int, font_face: str) -> tuple:
    """Measure (width, height) in pixels for *text* without rasterizing it."""
    screen_dc = _user32.GetDC(None)
    mem_dc = _CreateCompatibleDC(screen_dc)
    hfont = _CreateFontW(
        -font_size, 0, 0, 0,
        400, 0, 0, 0, 0, 0, 0, 2, 0,
        font_face,
    )
    old_font = _SelectObject(mem_dc, hfont)
    sz = _SIZE()
    _GetTextExtentPoint32W(mem_dc, text, len(text), ctypes.addressof(sz))
    _SelectObject(mem_dc, old_font)
    _DeleteObject(hfont)
    _DeleteDC(mem_dc)
    _user32.ReleaseDC(None, screen_dc)
    return max(sz.cx, 1), max(sz.cy, 1)


def _gdi_text_to_srv(device: int, text: str, font_size: int, font_face: str) -> tuple:
    """Render *text* with GDI into a grayscale bitmap, then upload it to a
    D3D11 R8_UNORM texture and return (SRV ptr, width, height). The caller
    must Release the SRV when done (see Renderer._flush_text)."""
    screen_dc = _user32.GetDC(None)
    mem_dc = _CreateCompatibleDC(screen_dc)

    hfont = _CreateFontW(
        -font_size, 0, 0, 0,
        400,  # weight (normal)
        0, 0, 0,
        0,  # charset (ANSI)
        0, 0, 2,  # OUT_DEFAULT, CLIP_DEFAULT, ANTIALIASED_QUALITY
        0,
        font_face,
    )
    old_font = _SelectObject(mem_dc, hfont)

    sz = _SIZE()
    _GetTextExtentPoint32W(mem_dc, text, len(text), ctypes.addressof(sz))
    tw, th = max(sz.cx, 1), max(sz.cy, 1)

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = tw
    bmi.bmiHeader.biHeight = -th  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    bits_ptr = c_void_p(None)
    hbm = _CreateDIBSection(mem_dc, ctypes.addressof(bmi), 0, byref(bits_ptr), None, 0)
    if not hbm or not bits_ptr.value:
        _DeleteObject(hfont)
        _DeleteDC(mem_dc)
        _user32.ReleaseDC(None, screen_dc)
        raise OSError(f"CreateDIBSection failed for text {text!r} ({tw}x{th})")
    _SelectObject(mem_dc, hbm)

    _SetBkMode(mem_dc, 1)  # TRANSPARENT
    _SetTextColor(mem_dc, 0x00FFFFFF)  # white-on-black -- luminance IS the glyph coverage
    _TextOutW(mem_dc, 0, 0, text, len(text))

    bgra = (c_ubyte * (tw * th * 4)).from_address(bits_ptr.value)
    r8 = (c_ubyte * (tw * th))()
    for i in range(tw * th):
        r8[i] = bgra[i * 4]  # B channel (white-on-black: all channels equal)

    _SelectObject(mem_dc, old_font)
    _DeleteObject(hfont)
    _DeleteObject(hbm)
    _DeleteDC(mem_dc)
    _user32.ReleaseDC(None, screen_dc)

    td = _Tex2DDesc()
    td.Width = tw
    td.Height = th
    td.MipLevels = 1
    td.ArraySize = 1
    td.Format = _FMT_R8_UNORM
    td.SampleDesc.Count = 1
    td.SampleDesc.Quality = 0
    td.Usage = _USAGE_DEFAULT
    td.BindFlags = _BIND_SHADER_RESOURCE
    td.CPUAccessFlags = 0
    td.MiscFlags = 0

    sd = _SubresData()
    sd.pSysMem = ctypes.cast(r8, c_void_p)
    sd.SysMemPitch = tw
    sd.SysMemSlicePitch = 0

    tex = c_void_p(None)
    hr = _dev(device, "CreateTexture2D", c_int,
              [POINTER(_Tex2DDesc), POINTER(_SubresData), POINTER(c_void_p)],
              byref(td), byref(sd), byref(tex))
    dx._check(hr, "CreateTexture2D (text)")

    srv = c_void_p(None)
    hr = _dev(device, "CreateShaderResourceView", c_int,
              [c_void_p, c_void_p, POINTER(c_void_p)],
              c_void_p(tex.value), None, byref(srv))
    dx._check(hr, "CreateShaderResourceView (text)")
    dx._release(tex.value)  # SRV holds a ref; release our ref

    return srv.value, tw, th
