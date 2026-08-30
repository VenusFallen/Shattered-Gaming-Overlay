; Inno Setup script for Shattered Gaming Overlay. Installs to Program Files,
; Start Menu shortcut. Matches updater.py's launch_installer_and_quit()
; assumption that the installer is Inno-Setup-based and accepts /VERYSILENT
; /SUPPRESSMSGBOXES /NORESTART /LOG=<path>.
;
; No driver install/uninstall step: this project has no kernel driver (see
; .claude\agents\engine-agent.md's hard rule). No LibreHardwareMonitor driver
; step either -- stats_poller.py runs with IsRing0Enabled = False, so LHM
; needs only its DLLs, shipped as plain data files via [Files] below.
;
; Both the installer and the app manifest as admin (PrivilegesRequired=admin
; below, ShatteredGamingOverlay.spec's uac_admin=True). Admin is required
; because stats_poller.py's FPS tracking (PresentMon, a real-time ETW trace
; session) fails with "access denied" unless elevated; the whole app
; requests admin at launch rather than self-elevating just PresentMon.
;
; A silent self-update used to auto-relaunch the app here (ssPostInstall).
; Every attempt hit a "Failed to load Python DLL" bootloader crash across
; many rounds of live testing (2026-08-30) that survived every fix tried --
; relaunch-check accuracy, ShellExec vs Exec, Defender exclusions including
; Block-At-First-Sight, stripping a stale _MEIPASS2, a fixed inspectable
; runtime_tmpdir. Tabled rather than sunk further time into it: a silent
; update now just tells the user to restart the app themselves instead of
; fighting the crash.
;
; No AppMutex set: main.py doesn't hold a named mutex for CloseApplications
; to target (that would be an application-logic change, out of scope here --
; see updater.py's module docstring). CloseApplications=yes is still set
; below without one -- Inno's Restart Manager also detects processes locking
; the file being replaced (ShatteredGamingOverlay.exe), so this remains a
; useful defense-in-depth layer on top of updater.py's Wait-Process watcher,
; which is the primary mechanism closing the race.

; Passed in by build.bat via /DMyAppVersion so this can't drift from
; version.py. Fallback only matters if ISCC runs outside build.bat.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
AppName=Shattered Gaming Overlay
AppVersion={#MyAppVersion}
AppPublisher=VenusFallen
AppSupportURL=https://github.com/VenusFallen/Shattered-Gaming-Overlay
DefaultDirName={autopf}\Shattered Gaming Overlay
DefaultGroupName=Shattered Gaming Overlay
OutputBaseFilename=ShatteredGamingOverlay_Setup
OutputDir=installer
Compression=lzma2
SolidCompression=yes
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\ShatteredGamingOverlay.exe
; Require Windows 10 or later
MinVersion=10.0

; Matches ShatteredGamingOverlay.spec's uac_admin=True (see header note).
PrivilegesRequired=admin

; See header note above re: no AppMutex yet.
CloseApplications=yes
RestartApplications=no

[Files]
; Main application
Source: "dist\ShatteredGamingOverlay.exe"; DestDir: "{app}"; Flags: ignoreversion

; Documentation
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

; Third-party license -- LibreHardwareMonitor DLLs bundled in lib/ (MPL 2.0)
Source: "lib\LICENSE-LibreHardwareMonitor.txt"; DestDir: "{app}"; Flags: ignoreversion

; Third-party license -- PresentMon.exe bundled in presentmon/ (MIT)
Source: "presentmon\LICENSE-PresentMon.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Shattered Gaming Overlay"; Filename: "{app}\ShatteredGamingOverlay.exe"
Name: "{group}\README"; Filename: "{app}\README.md"
Name: "{commondesktop}\Shattered Gaming Overlay"; Filename: "{app}\ShatteredGamingOverlay.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Run]
; Launch Shattered Gaming Overlay after install (optional, user can uncheck).
; skipifsilent -- silent self-updates don't auto-launch, see NotifyManualRelaunchNeeded() below.
Filename: "{app}\ShatteredGamingOverlay.exe"; Description: "Launch Shattered Gaming Overlay"; \
    Flags: nowait postinstall skipifsilent shellexec

[Code]
// See header note above -- a silent self-update no longer tries to relaunch
// the app itself, just tells the user to do it.
procedure NotifyManualRelaunchNeeded();
begin
  MsgBox('Shattered Gaming Overlay has been updated. Please start it again to continue.',
    mbInformation, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardSilent() then
    NotifyManualRelaunchNeeded();
end;
