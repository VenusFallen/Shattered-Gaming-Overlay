# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Shattered Gaming Overlay. Onefile build
# (EXE(pyz, a.scripts, a.binaries, a.datas, ...)); lib/*.dll and
# presentmon/*.exe are bundled as data files, not compiled in --
# stats_poller.py loads lib/*.dll via Assembly.LoadFrom() and runs
# presentmon/PresentMon.exe as a subprocess, both resolved via
# sys._MEIPASS when frozen (see _lib_dir()/_presentmon_path()).
#
# Notes:
#   - No PySide6 plugin/data bundling (no Qt in this project).
#   - imgui_bundle is bundled via collect_data_files() + collect_dynamic_libs()
#     only, not collect_all()/collect_submodules(). imgui_bundle ships a real
#     assets/ tree (fonts incl. FontAwesome 4/6 -- see main.py's
#     default_icon_font) that Hello ImGui's C++ side loads by relative path,
#     not through anything main.py controls, so it needs explicit data
#     bundling. No PyInstaller-contributed hook exists for imgui_bundle.
#     collect_all() also implies collect_submodules(), which pulls in
#     imgui_bundle's optional demos_python/ and python_backends/ subpackages
#     -- these statically import PyQt6, pygame, and pyglet (backends this
#     project doesn't use), dragging in unrelated bloat (all of PyQt6's Qt6
#     binaries) and a bogus "Hidden import 'clr._extra' not found" error.
#     This project's own imgui_bundle submodules (hello_imgui, imgui, immapp,
#     icons_fontawesome_4, imgui_toggle) are plain, statically-visible
#     imports, so PyInstaller's normal Analysis pass already discovers and
#     bundles them (plus native _imgui_bundle*.pyd/glfw3.dll) with no
#     hiddenimports needed.
#   - uac_admin=True is set on the EXE() below because stats_poller.py's FPS
#     tracking (PresentMon, a real-time ETW trace session) fails with
#     "access denied" unless elevated. Input capture/injection itself needs
#     no elevation, but the app requests admin whole-app rather than
#     self-elevating just the PresentMon subprocess.
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

datas = []
binaries = []
hiddenimports = []

# assets/ (icon.ico, icon.png) -- the `icon=` EXE param below only bakes
# icon.ico into the exe's resource section (Explorer/taskbar icon); it does
# NOT put the file on disk for the app to read at runtime. main.py's
# _set_window_icon() and tray_icon.py's _add_icon() both load
# 'assets/icon.ico' relative to their frozen __file__ (under sys._MEIPASS),
# silently no-op-ing if it's not bundled as data -- this is why the tray
# icon needs it explicitly listed here.
datas += [('assets', 'assets')]

# imgui_bundle -- this project's UI toolkit (Dear ImGui via Hello ImGui).
# Data files (fonts/icon assets loaded by relative path) + native binaries
# (_imgui_bundle*.pyd, glfw3.dll) only -- see the header note above for why
# collect_all()/collect_submodules() isn't used. subdir='assets' scopes
# collect_data_files() to imgui_bundle's runtime assets/ folder; without it,
# collect_data_files() also pulls in demos_cpp/, demos_python/,
# demos_assets/, and .pyi stubs that nothing here touches at runtime.
datas += collect_data_files('imgui_bundle', include_py_files=False, subdir='assets')
binaries += collect_dynamic_libs('imgui_bundle')

# pythonnet (clr) -- required for the Stats HUD via LibreHardwareMonitor.
# 'clr._extra' is deliberately not hand-listed as a hiddenimport: this
# pythonnet version has no such submodule (clr exposes a `_extras`
# *attribute*, not a `clr._extra` *submodule*), and listing it produces a
# harmless but bogus "Hidden import 'clr._extra' not found" line.
tmp_ret = collect_all('pythonnet')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
hiddenimports += ['clr']

# Bundle all DLLs from lib/ if present (LHM + its polyfill dependencies).
# Build succeeds without them -- stats simply won't appear (see
# stats_poller.py's module docstring).
import glob
for _dll in glob.glob('lib/*.dll'):
    datas.append((_dll, 'lib'))

# Bundle PresentMon.exe from presentmon/ if present (standalone console app,
# powers the FPS-tracking feature). Build succeeds without it -- FPS stats
# simply won't appear.
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
    # TEMPORARY debug instrumentation (2026-08-30) -- debug=True turns on the
    # bootloader's own verbose step-by-step tracing (DLL search, extraction,
    # each load attempt) instead of just the generic failure MessageBox;
    # needs console=True too so there's an actual console for it to print
    # into. The crash shows a blocking error dialog, so the process (and its
    # console) stays alive long enough to read/screenshot before it closes.
    # Revert both to False once the post-update relaunch crash is
    # root-caused.
    debug=True,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    # TEMPORARY debug instrumentation (2026-08-30) -- fixed, non-random,
    # never-auto-deleted extraction dir instead of the default random
    # %TEMP%\_MEI<n> so a crashed relaunch's actual extracted contents can
    # be inspected afterward. The default random temp folder is gone
    # within moments of a crash, which has blocked diagnosing the
    # post-update "Failed to load Python DLL" bug directly. Revert to
    # runtime_tmpdir=None once that's root-caused.
    runtime_tmpdir=r'C:\Users\VenusFallen\SGO_debug_extract',
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\icon.ico'],
    # See header note above -- PresentMon needs elevation for FPS tracking.
    uac_admin=True,
)
