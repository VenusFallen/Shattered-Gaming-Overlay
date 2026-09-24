@echo off
setlocal

rem Build script for Shattered Gaming Overlay: PyInstaller build, Inno Setup compile, release zip.
rem Requires: pip install pyinstaller (see requirements.txt) and Inno Setup 6 (ISCC.exe).
rem Run from the project root.

rem Read VERSION once, up front -- keeps the ISCC /D define and release zip name in sync with version.py.
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
