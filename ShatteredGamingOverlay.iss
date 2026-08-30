; Inno Setup script for Shattered Gaming Overlay. Installs to Program Files,
; Start Menu shortcut, silent self-update relaunch handling -- matches
; updater.py's launch_installer_and_quit() assumption that the installer is
; Inno-Setup-based and accepts /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
; /LOG=<path>.
;
; No driver install/uninstall step: this project has no kernel driver (see
; .claude\agents\engine-agent.md's hard rule). No LibreHardwareMonitor driver
; step either -- stats_poller.py runs with IsRing0Enabled = False, so LHM
; needs only its DLLs, shipped as plain data files via [Files] below.
;
; Installer and app both run admin (PrivilegesRequired=admin below,
; ShatteredGamingOverlay.spec's uac_admin=True), so the post-install [Run]
; launch needs no ShellExec runas workaround -- same privilege level, plain
; Exec/[Run] Filename= already runs at the right level. Admin is required
; because stats_poller.py's FPS tracking (PresentMon, a real-time ETW trace
; session) fails with "access denied" unless elevated; the whole app
; requests admin at launch rather than self-elevating just PresentMon.
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
; Setup's own post-close auto-relaunch is disabled in favor of
; RelaunchAppAfterSilentUpdate() below (ssPostInstall), which retries with a
; settle delay and verifies via tasklist that the relaunch actually stuck.
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
; No shellexec needed -- installer and app both run admin (see header note).
Filename: "{app}\ShatteredGamingOverlay.exe"; Description: "Launch Shattered Gaming Overlay"; \
    Flags: nowait postinstall skipifsilent

[Code]
// True if ShatteredGamingOverlay.exe is currently running (via tasklist).
// Used to verify a silent relaunch actually stuck.
function IsAppProcessRunning(): Boolean;
var
  ResultCode: Integer;
  Output: TExecOutput;
  I: Integer;
  CombinedOutput: String;
  MatchStr: String;
begin
  Result := False;
  if not ExecAndCaptureOutput('tasklist.exe',
    '/FI "IMAGENAME eq ShatteredGamingOverlay.exe" /NH', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode, Output) then
  begin
    // Distinct from a genuine "no matching process" result below -- a live
    // 2026-08-30 test showed this check reporting false negatives against a
    // process independently confirmed still running, and the two failure
    // modes need to be told apart to find out which one this actually is.
    Log('IsAppProcessRunning: ExecAndCaptureOutput failed to launch tasklist.exe -- result unknown, not a confirmed absence.');
    Exit;
  end;

  CombinedOutput := '';
  for I := 0 to GetArrayLength(Output.StdOut) - 1 do
    CombinedOutput := CombinedOutput + Output.StdOut[I] + #13#10;

  Result := Pos('SHATTEREDGAMINGOVERLAY.EXE', Uppercase(CombinedOutput)) > 0;

  if Result then
    MatchStr := 'yes'
  else
    MatchStr := 'no';
  Log('IsAppProcessRunning: tasklist.exe ResultCode=' + IntToStr(ResultCode) +
    ' StdOutLines=' + IntToStr(GetArrayLength(Output.StdOut)) +
    ' match=' + MatchStr + ' raw=[' + CombinedOutput + ']');
end;

// The "Launch Shattered Gaming Overlay" [Run] entry uses skipifsilent, so a
// silent self-update (updater.py runs this installer with /VERYSILENT)
// relaunches the app here instead. Retried with an increasing settle delay
// and verified via IsAppProcessRunning(), since the freshly extracted
// PyInstaller bootloader can occasionally be slow to come up after a
// silent install's file-copy step (AV real-time scanning race).
procedure RelaunchAppAfterSilentUpdate();
var
  ResultCode: Integer;
  Attempt: Integer;
  LaunchAttempt: Integer;
  SettleDelayMs: Integer;
begin
  // Up to two real launches. The second only fires after every check on the
  // first has come back "not running" -- avoids spawning a duplicate
  // instance if the first launch is just slow to register with tasklist
  // (AV scan race), while still recovering from a genuinely failed launch
  // (e.g. bootloader "Failed to load Python DLL").
  for LaunchAttempt := 1 to 2 do
  begin
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
          'running (launch attempt ' + IntToStr(LaunchAttempt) + ', check ' +
          IntToStr(Attempt) + ', last wait was ' + IntToStr(SettleDelayMs) + 'ms).');
        Exit;
      end;

      Log('RelaunchAppAfterSilentUpdate: ShatteredGamingOverlay.exe not found ' +
        'running (launch attempt ' + IntToStr(LaunchAttempt) + ', check ' +
        IntToStr(Attempt) + ', wait was ' + IntToStr(SettleDelayMs) + 'ms).');
      SettleDelayMs := SettleDelayMs + 1500;
    end;

    Log('RelaunchAppAfterSilentUpdate: launch attempt ' + IntToStr(LaunchAttempt) +
      ' never came up after 3 checks.');
  end;

  Log('RelaunchAppAfterSilentUpdate: gave up after 2 launch attempts -- the silent ' +
    'update completed but ShatteredGamingOverlay.exe did not stay running. ' +
    'The user will need to launch it manually.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardSilent() then
    RelaunchAppAfterSilentUpdate();
end;
