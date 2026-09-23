# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Shattered Gaming Overlay. Onefile build; lib/*.dll and presentmon/*.exe
# are bundled as data files (loaded via Assembly.LoadFrom()/subprocess, not compiled in).
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

datas = []
binaries = []
hiddenimports = []

# assets/ -- icon= EXE param only bakes icon.ico into the exe resource section, doesn't put
# files on disk; main.py/tray_icon.py both load 'assets/icon.ico' at runtime so it needs bundling.
datas += [('assets', 'assets')]

# imgui_bundle -- data files + native binaries only, not collect_all()/collect_submodules()
# (that would drag in unused demo subpackages that statically import PyQt6/pygame/pyglet).
datas += collect_data_files('imgui_bundle', include_py_files=False, subdir='assets')
binaries += collect_dynamic_libs('imgui_bundle')

# pythonnet (clr) -- required for the Stats HUD via LibreHardwareMonitor.
# clr._extra is not a real submodule (clr exposes a _extras attribute) so it's not hiddenimported.
tmp_ret = collect_all('pythonnet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
hiddenimports += ['clr']

# Bundle lib/*.dll if present (LHM); build succeeds without them, stats just won't appear.
import glob
for _dll in glob.glob('lib/*.dll'):
    datas.append((_dll, 'lib'))

# Bundle PresentMon.exe if present; build succeeds without it, FPS stats just won't appear.
for _pm in glob.glob('presentmon/*.exe'):
    datas.append((_pm, 'presentmon'))


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ShatteredGamingOverlay',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
    # PresentMon's FPS tracking needs elevation, so the whole app requests admin.
    uac_admin=True,
)
