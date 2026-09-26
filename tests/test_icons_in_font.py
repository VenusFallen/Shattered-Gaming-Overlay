"""Every icon the UI draws must exist in the FA4 font main.py loads, or it renders as a box."""

import os
import re
import struct
from pathlib import Path

import imgui_bundle
from imgui_bundle import icons_fontawesome_4 as fa

import icons

ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = Path(os.path.dirname(imgui_bundle.__file__)) / "assets" / "fonts" / "fontawesome-webfont.ttf"
ICON_NAME = re.compile(r"\bfa\.(ICON_FA_[A-Z0-9_]+)")


def _font_codepoints() -> set:
    data = FONT_PATH.read_bytes()
    num_tables = struct.unpack(">H", data[4:6])[0]
    cmap = next(
        struct.unpack(">I", data[20 + 16 * i:24 + 16 * i])[0]
        for i in range(num_tables)
        if data[12 + 16 * i:16 + 16 * i] == b"cmap"
    )
    codepoints = set()
    for i in range(struct.unpack(">H", data[cmap + 2:cmap + 4])[0]):
        sub = cmap + struct.unpack(">I", data[cmap + 8 + 8 * i:cmap + 12 + 8 * i])[0]
        if struct.unpack(">H", data[sub:sub + 2])[0] != 4:
            continue
        seg = struct.unpack(">H", data[sub + 6:sub + 8])[0] // 2
        ends = struct.unpack(f">{seg}H", data[sub + 14:sub + 14 + 2 * seg])
        starts = struct.unpack(f">{seg}H", data[sub + 16 + 2 * seg:sub + 16 + 4 * seg])
        for start, end in zip(starts, ends):
            if end != 0xFFFF:
                codepoints.update(range(start, end + 1))
    return codepoints


def _source_files():
    return [p for p in list(ROOT.glob("*.py")) + list((ROOT / "panels").glob("*.py"))]


def test_every_fa_constant_used_in_source_exists_in_loaded_font():
    in_font = _font_codepoints()
    missing = []
    for path in _source_files():
        for name in ICON_NAME.findall(path.read_text(encoding="utf-8")):
            glyph = getattr(fa, name, None)
            if glyph is None or ord(glyph) not in in_font:
                missing.append(f"{path.name}: {name}")
    assert not missing, f"icons missing from the loaded font: {sorted(set(missing))}"


def test_custom_icon_constants_exist_in_loaded_font():
    in_font = _font_codepoints()
    for name in ("TACHOMETER", "EXTERNAL_LINK"):
        assert ord(getattr(icons, name)) in in_font, name
