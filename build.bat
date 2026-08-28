@echo off
setlocal

rem Build script for Shattered Gaming Overlay. Mirrors R9Tools' own build
rem automation (D:\Projects\Python\Testing\R9Tools\build.bat runs just the
rem PyInstaller step; this project's pipeline additionally scripts the Inno
rem Setup compile + release-zip steps that R9Tools does by hand, per this
rem task's explicit ask) rather than reinventing the orchestration.
rem
rem Build requirements: pip install pyinstaller (see requirements.txt for
rem the rest -- psutil, pywin32, imgui-bundle, requests, pythonnet).
rem Also requires Inno Setup 6 (ISCC.exe) for step 2.
rem
rem Run this from the project root (the directory containing this file).

rem Read VERSION once, up front -- used both for the ISCC /D define (so
rem ShatteredGamingOverlay.iss's AppVersion can never drift out of sync with
rem version.py again, a real bug found and fixed during the pre-1.0 QA pass)
rem and for the release zip name in Step 3.
for /f "usebackq tokens=*" %%v in (`python -c "import version; print(version.VERSION)"`) do set SGO_VERSION=%%v
if "%SGO_VERSION%"=="" (
    echo ERROR: could not read VERSION from version.py
    goto :error
)

echo === Step 1/3: PyInstaller ===
pyinstaller ShatteredGamingOverlay.spec
if errorlevel 1 goto :error

echo.
echo === Step 2/3: Inno Setup compile ===
where iscc >nul 2>nul
if %errorlevel%==0 (
    set "ISCC=iscc"
) else if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
) else (
    echo ERROR: ISCC.exe ^(Inno Setup 6 compiler^) not found on PATH or in the default install locations.
    goto :error
)
"%ISCC%" "/DMyAppVersion=%SGO_VERSION%" ShatteredGamingOverlay.iss
if errorlevel 1 goto :error

echo.
echo === Step 3/3: Release zip ===
set ZIP_NAME=ShatteredGamingOverlay_v%SGO_VERSION%.zip
powershell -NoProfile -Command "Compress-Archive -Path 'installer\ShatteredGamingOverlay_Setup.exe' -DestinationPath 'installer\%ZIP_NAME%' -Force"
if errorlevel 1 goto :error

echo.
echo Build complete: installer\%ZIP_NAME%  (contains ShatteredGamingOverlay_Setup.exe,
echo the exact name updater.py's _INSTALLER_EXE_NAME expects to find inside it)
goto :eof

:error
echo.
echo Build FAILED.
exit /b 1
