# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Shattered Gaming Overlay. Ported from R9Tools'
# R9Tools.spec (D:\Projects\Python\Testing\R9Tools\R9Tools.spec) -- same
# onefile EXE(pyz, a.scripts, a.binaries, a.datas, ...) pattern, same
# lib/*.dll and presentmon/*.exe glob-bundling approach (stats_poller.py
# loads lib/*.dll via Assembly.LoadFrom() at runtime and presentmon/
# PresentMon.exe as a subprocess, both resolved via sys._MEIPASS when
# frozen -- see stats_poller.py's _lib_dir()/_presentmon_path() -- so both
# must ship as data files, never compiled in).
#
# What's different from R9Tools.spec:
#   - No PySide6 plugin/data bundling at all (this project has no Qt).
#   - imgui_bundle is bundled via collect_data_files() + collect_dynamic_libs()
#     ONLY (data + native binaries), deliberately NOT collect_all()/
#     collect_submodules(). imgui_bundle ships its own real assets/ tree
#     (fonts for Hello ImGui's default font + FontAwesome 4/6 icon fonts --
#     see main.py's `default_icon_font = hi.DefaultIconFont.font_awesome4`)
#     that Hello ImGui's C++ side locates relative to the installed package
#     at runtime, not via any Python-level path main.py controls -- that
#     part genuinely needs explicit data bundling, same reasoning as
#     R9Tools' collect_all('PySide6'). There is no PyInstaller-contributed
#     hook for imgui_bundle (confirmed by checking both imgui_bundle's own
#     package tree for a bundled `__pyinstaller` hook and the installed
#     pyinstaller-hooks-contrib package's stdhooks for an imgui entry --
#     neither exists). BUT collect_all() also implies collect_submodules(),
#     which pulls in imgui_bundle's own optional demos_python/ and
#     python_backends/ subpackages as hidden imports -- those statically
#     import PyQt6, pygame, and pyglet (imgui_bundle's OTHER supported
#     backends, none of which this project uses; main.py only ever imports
#     hello_imgui/imgui/immapp), which pulled in a huge amount of unrelated
#     bloat (all of PyQt6's Qt6 binaries included) and produced a real
#     `Hidden import 'clr._extra' not found` ERROR-level line in a first
#     build attempt. This project's own imgui_bundle submodules (hello_imgui,
#     imgui, immapp, icons_fontawesome_4, imgui_toggle) are all plain,
#     statically-visible `from imgui_bundle import X` imports across
#     main.py/shell.py/titlebar.py/widgets.py/panels/*.py, so PyInstaller's
#     normal Analysis pass already discovers and bundles them (and the
#     native _imgui_bundle*.pyd/glfw3.dll they load) without any
#     hiddenimports help at all.
#   - uac_admin is NOT set (defaults to False/unset) -- unlike R9Tools, this
#     app has no Interception driver and no requireAdministrator manifest
#     anywhere in the source (confirmed via grep across the repo); it uses
#     plain SetWindowsHookEx-based hooks (input_hooks.py) which don't need
#     elevation for a same-session target window.
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

datas = []
binaries = []
hiddenimports = []

# assets/ (icon.ico, icon.png) -- the `icon=` EXE parameter below only bakes
# icon.ico into the built exe's own resource section (what Explorer/the
# taskbar show for the exe file itself); it does NOT put the file on disk
# for the app's own code to read at runtime. main.py's _set_window_icon()
# and tray_icon.py's _add_icon() both load 'assets/icon.ico' from a path
# relative to their own frozen __file__ (PyInstaller resolves that under
# sys._MEIPASS), which silently no-ops if the file isn't actually bundled
# as data -- confirmed as the real cause of a shipped-and-reported bug
# where the system tray icon rendered blank (the main window's own titlebar
# icon had the identical silent failure, just masked by Windows falling
# back to the exe's baked-in resource icon there, which Shell_NotifyIcon
# has no equivalent fallback for).
datas += [('assets', 'assets')]

# imgui_bundle -- this project's entire UI toolkit (Dear ImGui via Hello
# ImGui). Data files only (fonts/icon assets Hello ImGui's C++ side loads by
# relative path) + native binaries (_imgui_bundle*.pyd, glfw3.dll) -- see
# the module-docstring note above for why collect_all()/collect_submodules()
# is deliberately NOT used here. subdir='assets' scopes collect_data_files()
# to imgui_bundle's actual runtime assets/ folder (fonts, icon.png, .plist)
# -- without it, collect_data_files() also pulls in the package's
# demos_cpp/ (raw .cpp source), demos_python/ (notebooks, readmes),
# demos_assets/ (demo-only images), and .pyi type stubs, none of which this
# project's own code ever touches at runtime, confirmed by direct
# inspection of a first-attempt build's archive contents.
datas += collect_data_files('imgui_bundle', include_py_files=False, subdir='assets')
binaries += collect_dynamic_libs('imgui_bundle')

# pythonnet (clr) -- required for the Stats HUD via LibreHardwareMonitor,
# same as R9Tools. R9Tools' own spec additionally hand-lists 'clr._extra' as
# a hiddenimport; this project's spec omits it. Confirmed by direct
# introspection against this project's installed pythonnet that no such
# submodule exists in this version (`clr` exposes a `_extras` *attribute*,
# not a `clr._extra` *submodule* -- `import clr._extra` fails with "'clr' is
# not a package" here), and a first build attempt that included it produced
# a real "Hidden import 'clr._extra' not found" ERROR-level line (harmless
# -- doesn't fail the build -- but a real, confirmed-bogus entry, so dropped
# here rather than carried forward uninvestigated).
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
)
