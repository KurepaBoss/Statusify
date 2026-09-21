; Statusify installer — Inno Setup 6.
;
; Build:  iscc /DAppVersion=1.3.0 installer\Statusify.iss
; Needs:  dist\Statusify.exe (from `pyinstaller Statusify.spec`) to exist first.
; Output: dist\Statusify-Setup-<version>.exe
;
; Per-user install, deliberately. Spicetify refuses to run elevated and patches
; the per-user Spotify in %APPDATA%, and Statusify writes its config/history
; next to its own exe — both break under Program Files + admin.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E0B5C3A-8F2D-4C8B-9A61-5F3E2D7C1B40}
AppName=Statusify
AppVersion={#AppVersion}
AppPublisher=KurepaBoss
AppPublisherURL=https://github.com/KurepaBoss/Statusify
AppSupportURL=https://github.com/KurepaBoss/Statusify/issues
DefaultDirName={localappdata}\Programs\Statusify
DefaultGroupName=Statusify
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Statusify-Setup-{#AppVersion}
SetupIconFile=..\statusify.ico
UninstallDisplayIcon={app}\Statusify.exe
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
; Statusify holds this mutex while running; Setup asks the user to close it
; instead of failing to overwrite a locked exe.
AppMutex=Global\Statusify_SingleInstance_v1
CloseApplications=yes
LicenseFile=..\LICENSE

[Tasks]
Name: "spicetify"; Description: "Install Spicetify and the lyrics bridge (needed for lyrics; restarts Spotify)"
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start Statusify with Windows"; Flags: unchecked

[Files]
Source: "..\dist\Statusify.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\lyrics-bridge.js"; DestDir: "{app}\setup"; Flags: ignoreversion
Source: "setup-spicetify.ps1"; DestDir: "{app}\setup"; Flags: ignoreversion

[Icons]
Name: "{group}\Statusify"; Filename: "{app}\Statusify.exe"
Name: "{group}\Repair Spicetify bridge"; Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\setup-spicetify.ps1"" -Bridge ""{app}\setup\lyrics-bridge.js"""; \
  Comment: "Run this if lyrics stop working after a Spotify update"
Name: "{group}\Uninstall Statusify"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Statusify"; Filename: "{app}\Statusify.exe"; Tasks: desktopicon
; Same path and name the app's own "Launch with Windows" toggle uses, so the
; two stay in agreement.
Name: "{userstartup}\Statusify"; Filename: "{app}\Statusify.exe"; Tasks: startup

[Run]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\setup-spicetify.ps1"" -Bridge ""{app}\setup\lyrics-bridge.js"""; \
  StatusMsg: "Setting up Spicetify (a console window will show progress)..."; \
  Flags: waituntilterminated; Tasks: spicetify; Check: not WizardSilent
; In-app updates run Setup /SILENT: refresh the bridge without a console that
; waits for a keypress, then bring Statusify back up (it quit to let us in).
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\setup-spicetify.ps1"" -Bridge ""{app}\setup\lyrics-bridge.js"" -NoPause"; \
  Flags: runhidden waituntilterminated; Tasks: spicetify; Check: WizardSilent
Filename: "{app}\Statusify.exe"; Description: "Launch Statusify"; Flags: nowait postinstall skipifsilent
Filename: "{app}\Statusify.exe"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\setup\setup-spicetify.ps1"" -Uninstall"; \
  Flags: runhidden waituntilterminated; RunOnceId: "RemoveBridge"

[UninstallDelete]
; Runtime files Statusify creates beside its exe. Removed on uninstall so the
; folder doesn't linger; the user is warned about history in the prompt below.
Type: files; Name: "{app}\statusify.cfg"
Type: files; Name: "{app}\history.json"
Type: files; Name: "{app}\statusify.log*"
Type: files; Name: "{app}\crash.log"
Type: files; Name: "{app}\.env"
Type: filesandordirs; Name: "{app}\.artcache"

[Code]
var
  AppIdPage: TInputQueryWizardPage;

function EnvFile(): String;
begin
  Result := ExpandConstant('{app}\.env');
end;

// Pre-fill from an existing install so upgrades don't ask twice.
function ExistingAppId(): String;
var
  Lines: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if LoadStringsFromFile(ExpandConstant('{localappdata}\Programs\Statusify\.env'), Lines) then
    for I := 0 to GetArrayLength(Lines) - 1 do
      if Pos('DISCORD_APP_ID=', Lines[I]) = 1 then
        Result := Trim(Copy(Lines[I], 16, MaxInt));
end;

function IsDigits(S: String): Boolean;
var
  I: Integer;
begin
  Result := Length(S) > 0;
  for I := 1 to Length(S) do
    if (S[I] < '0') or (S[I] > '9') then begin
      Result := False;
      Exit;
    end;
end;

procedure InitializeWizard();
begin
  AppIdPage := CreateInputQueryPage(wpSelectTasks,
    'Discord Application ID',
    'Statusify shows your music through your own Discord application.',
    'Create one at https://discord.com/developers/applications (New Application ' +
    '→ give it a name, e.g. "Spotify" — that name is what your status will show). ' +
    'Copy its Application ID from the General Information page and paste it below.' + #13#10#13#10 +
    'You can leave this empty and Statusify will ask on first launch.');
  AppIdPage.Add('Application ID:', False);
  AppIdPage.Values[0] := ExistingAppId();
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  V: String;
begin
  Result := True;
  if CurPageID = AppIdPage.ID then begin
    V := Trim(AppIdPage.Values[0]);
    AppIdPage.Values[0] := V;
    if (V <> '') and ((not IsDigits(V)) or (Length(V) < 17) or (Length(V) > 20)) then begin
      MsgBox('That doesn''t look like an Application ID — it should be a 17-20 digit number.',
             mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  V: String;
begin
  if CurStep = ssPostInstall then begin
    V := Trim(AppIdPage.Values[0]);
    if V <> '' then
      SaveStringToFile(EnvFile(), 'DISCORD_APP_ID=' + V + #13#10, False);
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := MsgBox('Uninstalling removes Statusify, its settings and your listening history. ' +
                   'Spicetify itself stays installed. Continue?',
                   mbConfirmation, MB_YESNO) = IDYES;
end;
