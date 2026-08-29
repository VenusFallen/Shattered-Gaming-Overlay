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
;   - The shellexec elevation workaround R9Tools.iss needed on its
;     post-install [Run] entry. Both this installer and the app it installs
;     now run at the SAME privilege level (both admin -- see PrivilegesRequired
;     below and ShatteredGamingOverlay.spec's uac_admin=True), so there's no
;     privilege mismatch to bridge with ShellExec's runas verb; a plain
;     launch (Exec/[Run] Filename=) already runs at the right level.
;     (uac_admin / PrivilegesRequired=admin themselves ARE carried over now,
;     unlike the original build of this file -- see the note below.)
;
; PrivilegesRequired=admin (set below) is a deliberate CHANGE from this
; project's original no-elevation design -- everything else in the app
; (input hooks, LibreHardwareMonitor with IsRing0Enabled=False) still needs
; no elevation at all, but stats_poller.py's FPS tracking (PresentMon, a
; real-time ETW trace session) does: confirmed by direct repro, PresentMon
; fails immediately with "access denied" unless elevated. Rather than
; self-elevate only the PresentMon subprocess (which would mean a UAC prompt
; only the first time FPS tracking starts), the whole app now requests admin
; at launch, so the installer needs to match that privilege level too.
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

; See the header comment above -- the app itself now requires admin
; (ShatteredGamingOverlay.spec's uac_admin=True, for PresentMon/FPS
; tracking), so the installer matches that privilege level explicitly
; rather than relying on whatever Inno Setup's own default happens to be.
PrivilegesRequired=admin

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
; No shellexec needed here -- installer and app are both admin now (see the
; header comment / PrivilegesRequired above), so a plain postinstall launch
; already runs at the right privilege level.
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
  // Launched exactly ONCE. An earlier version of this procedure called
  // Exec() again on every retry iteration -- when the first launch was
  // merely slow to register with tasklist (the AV real-time-scanning race
  // described above) rather than actually failed, that spawned a second,
  // and sometimes third, fully separate running instance instead of just
  // catching up to the one already on its way. A real bug, reported after
  // shipping with the old loop. The retries below only re-check whether
  // that single launch came up, with a growing wait between checks --
  // never launching a second process.
  Sleep(2000);
  Exec(ExpandConstant('{app}\ShatteredGamingOverlay.exe'), '', '',
    SW_SHOWNORMAL, ewNoWait, ResultCode);

  SettleDelayMs := 1500;
  for Attempt := 1 to 3 do
  begin
    Sleep(SettleDelayMs);

    if IsAppProcessRunning() then
    begin
      Log('RelaunchAppAfterSilentUpdate: ShatteredGamingOverlay.exe confirmed ' +
        'running (check ' + IntToStr(Attempt) + ', last wait was ' +
        IntToStr(SettleDelayMs) + 'ms).');
      Exit;
    end;

    Log('RelaunchAppAfterSilentUpdate: ShatteredGamingOverlay.exe not found ' +
      'running on check ' + IntToStr(Attempt) + ' (wait was ' +
      IntToStr(SettleDelayMs) + 'ms) -- checking again before giving up, ' +
      'not launching another instance.');
    SettleDelayMs := SettleDelayMs + 1500;
  end;

  Log('RelaunchAppAfterSilentUpdate: gave up after 3 checks -- the silent ' +
    'update completed but ShatteredGamingOverlay.exe did not appear to stay ' +
    'running. The user will need to launch it manually.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardSilent() then
    RelaunchAppAfterSilentUpdate();
end;
