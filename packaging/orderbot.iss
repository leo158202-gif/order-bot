#ifndef StageRoot
  #error StageRoot is required
#endif
#ifndef ReleaseVersion
  #error ReleaseVersion is required
#endif
[Setup]
#ifdef ValidationBuild
AppId=OrderBot.IsolatedValidation.{#ValidationBuild}
AppName=OrderBot Isolated Validation
#else
AppId={{E173CC1D-AB47-4D22-BB44-04D3E7C9D4F8}
AppName=OrderBot
#endif
AppVersion={#ReleaseVersion}
DefaultDirName={localappdata}\Programs\OrderBot
DefaultGroupName=OrderBot
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputPath}
OutputBaseFilename=OrderBot-{#ReleaseVersion}-setup
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\OrderBot.exe
SetupLogging=yes
[Files]
Source: "{#StageRoot}\*"; DestDir: "{app}"; Excludes: "current.json"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
#ifndef ValidationBuild
Name: "{group}\OrderBot"; Filename: "{app}\OrderBot.exe"
Name: "{autodesktop}\OrderBot"; Filename: "{app}\OrderBot.exe"
#endif
[Run]
Filename: "{app}\OrderBot.exe"; Description: "打开 OrderBot（首次设置；发送默认关闭）"; Flags: postinstall nowait skipifsilent unchecked
[Code]
function MoveFileEx(ExistingFile, NewFile: String; Flags: Cardinal): Boolean;
  external 'MoveFileExW@kernel32.dll stdcall';
function OpenRunningFile(FileName: String; DesiredAccess, ShareMode, SecurityAttributes,
  CreationDisposition, FlagsAndAttributes, TemplateFile: Cardinal): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseRunningFile(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function TargetRunning: Boolean;
var Handle: THandle; Marker: String;
begin
  Result := False;
  Marker := ExpandConstant('{app}\launcher.running');
  if not FileExists(Marker) then Exit;
  Handle := OpenRunningFile(Marker, $80000000, 0, 0, 3, 0, 0);
  if Handle = THandle(-1) then Result := True
  else CloseRunningFile(Handle);
end;
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if TargetRunning then Result := '此安装目录的OrderBot仍在运行，请正常关闭窗口后再安装；其它目录程序不受影响。';
end;
function InitializeUninstall: Boolean;
begin
  Result := not TargetRunning;
  if not Result then MsgBox('请先正常关闭此目录的OrderBot，再卸载；客户data会保留。', mbError, MB_OK);
end;
function NeedsVC: Boolean;
var Installed: Cardinal;
begin
  Result := not RegQueryDWordValue(HKLM64, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) or (Installed <> 1);
end;
function NeedsWebView: Boolean;
var Version: String;
begin
  Result := True;
  if RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) then
    if (Version <> '') and (Version <> '0.0.0.0') then Result := False;
  if Result and RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) then
    if (Version <> '') and (Version <> '0.0.0.0') then Result := False;
end;
procedure CurStepChanged(CurStep: TSetupStep);
var PointerFile: String; ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    if NeedsVC then begin
      if not Exec(ExpandConstant('{app}\prerequisites\vc_redist.x64.exe'), '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
        RaiseException('Visual C++组件未安装，请由账号所有人完成安装后重试。');
      if NeedsVC then RaiseException('Visual C++组件尚未就绪，未切换程序版本。');
    end;
    if NeedsWebView then begin
      if not Exec(ExpandConstant('{app}\prerequisites\MicrosoftEdgeWebview2Setup.exe'), '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
        RaiseException('WebView2组件未安装，请确认网络或由账号所有人安装后重试。');
      if NeedsWebView then RaiseException('WebView2组件尚未就绪，请连接网络安装官方运行组件后重试；未切换程序版本。');
    end;
    PointerFile := ExpandConstant('{app}\current.json');
    if FileExists(PointerFile) then CopyFile(PointerFile, ExpandConstant('{app}\previous.json'), False);
    SaveStringToFile(PointerFile + '.new', '{"current":"{#ReleaseVersion}"}', False);
    if not MoveFileEx(PointerFile + '.new', PointerFile, 9) then RaiseException('无法切换程序版本；客户data未改动。');
  end;
end;
