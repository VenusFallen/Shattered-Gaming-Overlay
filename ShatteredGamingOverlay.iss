; Inno Setup script for Shattered Gaming Overlay. Ported from R9Tools'
; R9Tools.iss (D:\Projects\Python\Testing\R9Tools\R9Tools.iss) -- same
; overall structure (installs to Program Files, Start Menu shortcut, silent
; self-update relaunch handling), matching updater.py's
; launch_installer_and_quit() assumption that the eventual installer is
; Inno-Setup-based and accepts /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; /LOG=<path>.
;
; Deliberately NOT carried over from R9Tools.iss:
;   - The entire Interception driver install/uninstall step (
;     InstallInterceptionDriver, IsInterceptionServiceRunning,
;     IsInterceptionDriverActive, NeedRestart, the [UninstallRun] driver
;     removal, installer_assets\install-interception.exe). This project has
;     no driver at all -- see .claude\agents\engine-agent.md's hard rule --
;     that's the whole point of its architecture. There is nothing to
;     install, nothing that needs a reboot, and nothing to uninstall here.
;   - Any LibreHardwareMonitor driver install step. stats_poller.py already
;     runs with IsRing0Enabled = False (confirmed in stats_poller.py), so
;     LHM needs no separate driver install either -- only its DLLs, which
;     ship as plain data files via the [Files] section below.
;   - uac_admin / PrivilegesRequired=admin / the shellexec elevation
;     workaround on the post-install [Run] entry. This project's exe has no
;     requireAdministrator manifest (confirmed via grep across the repo --
;     no driver means no elevation requirement), so PrivilegesRequired is
;     left at Inno Setup's default (lowest) and the [Run]/relaunch entries
;     use plain launches, not ShellExec.
;   - AppMutex=R9Tools_AppMutex. R9Tools' main.py creates and holds a named
;     mutex for its whole lifetime specifically so CloseApplications=yes can
;     target it; main.py in this project does not create an equivalent
;     mutex yet (that would be an application-logic change, out of
;     build-agent's scope -- see updater.py's own module docstring, which
;     already documents this as a known, deliberate gap). CloseApplications
;     =yes is still set below without an AppMutex -- Inno Setup's Restart
;     Manager integration also detects processes holding a lock on the
;     actual file being replaced (ShatteredGamingOverlay.exe) even with no
;     AppMutex configured, so this still provides real defense-in-depth
;     (per updater.py's own "belt-and-suspenders safety net" framing) on
;     top of updater.py's Wait-Process watcher, which remains the primary
;     mechanism closing the race.

; MyAppVersion is passed in by build.bat via /DMyAppVersion=<version.py's
; VERSION> at compile time, so AppVersion below can never drift out of sync
; with the actual app again (found and fixed as a real bug during the pre-
; 1.0 QA pass: this file's AppVersion was hardcoded and had gone stale
; against version.py). The fallback here only matters if ISCC is ever run
; directly instead of through build.bat.
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

; See the deliberately-not-carried-over note above re: no AppMutex yet.
CloseApplications=yes
; Setup's own post-close auto-relaunch is disabled in favor of this script's
; RelaunchAppAfterSilentUpdate() below (ssPostInstall), which retries with a
; settle delay and verifies via tasklist that the relaunch actually stuck --
; mirrors R9Tools.iss's own reasoning for the same override.
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
; No shellexec needed here (unlike R9Tools) -- this app's exe has no
; requireAdministrator manifest, so a plain postinstall launch works fine.
Filename: "{app}\ShatteredGamingOverlay.exe"; Description: "Launch Shattered Gaming Overlay"; \
    Flags: nowait postinstall skipifsilent

[Code]
// True if ShatteredGamingOverlay.exe is currently running (via tasklist).
// Used to verify a silent-relaunch actually stuck, mirroring R9Tools.iss's
// IsAppProcessRunning().
function IsAppProcessRunning(): Boolean;
var
  ResultCode: Integer;
  Output: TExecOutput;
  I: Integer;
  CombinedOutput: String;
begin
  Result := False;
  if not ExecAndCaptureOutput('tasklist.exe',
    '/FI "IMAGENAME eq ShatteredGamingOverlay.exe" /NH', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode, Output) then
    Exit;

  CombinedOutput := '';
  for I := 0 to GetArrayLength(Output.StdOut) - 1 do
    CombinedOutput := CombinedOutput + Output.StdOut[I] + #13#10;

  Result := Pos('SHATTEREDGAMINGOVERLAY.EXE', Uppercase(CombinedOutput)) > 0;
end;

// The "Launch Shattered Gaming Overlay" [Run] entry uses skipifsilent, so a
// silent self-update (updater.py runs this installer with /VERYSILENT)
// needs to relaunch the app itself here instead. Mirrors R9Tools.iss's
// RelaunchAppAfterSilentUpdate(): retried with an increasing settle delay
// (2s/4s/6s) and verified via IsAppProcessRunning(), since the freshly
// extracted PyInstaller bootloader can occasionally fail to come up right
// after a silent install's file-copy step (AV real-time scanning race).
procedure RelaunchAppAfterSilentUpdate();
var
  ResultCode: Integer;
  Attempt: Integer;
  SettleDelayMs: Integer;
begin
  SettleDelayMs := 2000;
  for Attempt := 1 to 3 do
  begin
    Sleep(SettleDelayMs);
    Exec(ExpandConstant('{app}\ShatteredGamingOverlay.exe'), '', '',
      SW_SHOWNORMAL, ewNoWait, ResultCode);

    // Give the bootloader a moment to either come up or crash outright
    // before checking whether it's actually still alive.
    Sleep(1500);

    if IsAppProcessRunning() then
    begin
      Log('RelaunchAppAfterSilentUpdate: ShatteredGamingOverlay.exe confirmed ' +
        'running after attempt ' + IntToStr(Attempt) + ' (settle delay was ' +
        IntToStr(SettleDelayMs) + 'ms).');
      Exit;
    end;

    Log('RelaunchAppAfterSilentUpdate: ShatteredGamingOverlay.exe not found ' +
      'running after attempt ' + IntToStr(Attempt) + ' (settle delay was ' +
      IntToStr(SettleDelayMs) + 'ms) -- likely the bootloader race described ' +
      'above; retrying with a longer settle delay.');
    SettleDelayMs := SettleDelayMs + 2000;
  end;

  Log('RelaunchAppAfterSilentUpdate: gave up after 3 attempts -- the silent ' +
    'update completed but ShatteredGamingOverlay.exe did not stay running. ' +
    'The user will need to launch it manually.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardSilent() then
    RelaunchAppAfterSilentUpdate();
end;
