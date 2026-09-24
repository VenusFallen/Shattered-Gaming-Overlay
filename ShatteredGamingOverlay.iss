; Inno Setup script for Shattered Gaming Overlay. Installs to Program Files with a Start Menu
; shortcut; matches updater.py's launch_installer_and_quit() silent-install flag assumptions.
;
; No driver install step -- no kernel driver is bundled. LibreHardwareMonitor DLLs ship as plain
; data files via [Files] below (stats_poller.py runs with IsRing0Enabled = False).
;
; Installer and app both manifest as admin (PrivilegesRequired=admin below,
; ShatteredGamingOverlay.spec's uac_admin=True) -- PresentMon's FPS tracking needs elevation.
;
; Silent self-update no longer auto-relaunches the app (see NotifyManualRelaunchNeeded below) --
; just tells the user to restart it themselves.
;
; No AppMutex set -- CloseApplications=yes still helps via Inno's Restart Manager detecting the
; locked exe, as defense-in-depth on top of updater.py's Wait-Process watcher.

; Passed in by build.bat via /DMyAppVersion so this can't drift from version.py.
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

[InstallDelete]
; Cleans up orphaned per-version settings ini files from old releases; current ShatteredGamingOverlay.ini doesn't match this pattern, so live settings are never touched.
Type: files; Name: "{app}\Shattered_Gaming_Overlay__v*.ini"

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
