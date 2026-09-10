; Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
; Inno Setup installer script
; 仅由 scripts\build-exe.ps1 调用；wrapper 从 pyproject.toml 读取并传入构建配置。

#ifndef BuildDisplayName
  #error BuildDisplayName is required; run scripts\build-exe.ps1
#endif
#ifndef BuildVersion
  #error BuildVersion is required; run scripts\build-exe.ps1
#endif
#ifndef BuildExecutableNameWindows
  #error BuildExecutableNameWindows is required; run scripts\build-exe.ps1
#endif
#ifndef BuildDistDirName
  #error BuildDistDirName is required; run scripts\build-exe.ps1
#endif
#ifndef BuildSetupBaseName
  #error BuildSetupBaseName is required; run scripts\build-exe.ps1
#endif

#define MyAppName BuildDisplayName
#define MyAppVersion BuildVersion
#define MyAppPublisher "openJiuwen"
#define MyAppExeName BuildExecutableNameWindows
#define MyAppURL "https://openjiuwen.com"

[Setup]
AppId={{6DC96977-C194-44FE-812D-D4F0B576BD905}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
; Per-user install: LocalAppData is user-writable, so the installer no longer
; needs administrator privileges. The previous admin lineage installed into
; {autopf} (Program Files); a [Code] hook migrates those installs away.
DefaultDirName={localappdata}\Programs\{#MyAppName}
; Do not inherit the old admin install directory for the same AppId. If an
; uninstall was interrupted, that directory is still under Program Files and
; cannot be safely written by this per-user installer.
UsePreviousAppDir=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename={#BuildSetupBaseName}
SetupIconFile=..\jiuwenswarm\channels\web\frontend\public\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
; lowest = no UAC for a fresh per-user install. The [Code] section elevates
; (runas) only when an old admin install must be removed during migration.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Keep the legacy and current mutexes stable across installer AppId transitions.
; New frozen processes also create the product-derived mutexes.
AppMutex=JiuwenSwarm.App,Global\JiuwenSwarm.App,{#MyAppName}.App,Global\{#MyAppName}.App
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#BuildDistDirName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--desktop-reset-external-cli-config"; Flags: runhidden waituntilterminated; RunOnceId: "ResetExternalCliConfig"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[UninstallDelete]
; Remove only application-owned paths that may gain runtime-generated files.
; User configuration lives outside {app}; optional Windows runtimes live under {app}\runtime.
; The uninstall command resets external CLI switches before this directory is removed.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\runtime"
Type: files; Name: "{app}\{#MyAppExeName}"
Type: dirifempty; Name: "{app}"

[Run]
; 通过 Explorer 代启动程序，使安装完成后的启动上下文更接近桌面快捷方式启动
; postinstall 在安装向导最后一页显示运行应用复选框，由用户决定是否启动
Filename: "{win}\explorer.exe"; Parameters: """{app}\{#MyAppExeName}"""; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall

[Code]
function HasDescriptionStemInTree(const Directory, Stem: String): Boolean;
var
  FindRec: TFindRec;
  ChildDirectory: String;
begin
  Result := False;
  if not FindFirst(AddBackslash(Directory) + '*', FindRec) then
    exit;

  try
    repeat
      if (FindRec.Name = '.') or (FindRec.Name = '..') then
        continue;

      if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
      begin
        if CompareText(FindRec.Name, 'fragments') <> 0 then
        begin
          ChildDirectory := AddBackslash(Directory) + FindRec.Name;
          if HasDescriptionStemInTree(ChildDirectory, Stem) then
          begin
            Result := True;
            exit;
          end;
        end;
      end
      else if (CompareText(ExtractFileExt(FindRec.Name), '.md') = 0) and
              (CompareText(ChangeFileExt(FindRec.Name, ''), Stem) = 0) then
      begin
        Result := True;
        exit;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

function HasNestedDescriptionReplacement(const LanguageDirectory, Stem: String): Boolean;
var
  FindRec: TFindRec;
  ChildDirectory: String;
begin
  Result := False;
  if not FindFirst(AddBackslash(LanguageDirectory) + '*', FindRec) then
    exit;

  try
    repeat
      if (FindRec.Name = '.') or (FindRec.Name = '..') then
        continue;

      if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0) and
         (CompareText(FindRec.Name, 'fragments') <> 0) then
      begin
        ChildDirectory := AddBackslash(LanguageDirectory) + FindRec.Name;
        if HasDescriptionStemInTree(ChildDirectory, Stem) then
        begin
          Result := True;
          exit;
        end;
      end;
    until not FindNext(FindRec);
  finally
    FindClose(FindRec);
  end;
end;

procedure CleanupStaleOpenJiuwenDescriptions();
var
  DescsDirectory: String;
  LanguageDirectory: String;
  StalePath: String;
  Stem: String;
  LanguageRec: TFindRec;
  FlatRec: TFindRec;
begin
  DescsDirectory := ExpandConstant(
    '{app}\_internal\openjiuwen\agent_teams\tools\locales\descs'
  );
  if not DirExists(DescsDirectory) then
    exit;

  if not FindFirst(AddBackslash(DescsDirectory) + '*', LanguageRec) then
    exit;

  try
    repeat
      if (LanguageRec.Name = '.') or (LanguageRec.Name = '..') or
         ((LanguageRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0) then
        continue;

      LanguageDirectory := AddBackslash(DescsDirectory) + LanguageRec.Name;
      if not FindFirst(AddBackslash(LanguageDirectory) + '*.md', FlatRec) then
        continue;

      try
        repeat
          Stem := ChangeFileExt(FlatRec.Name, '');
          if not HasNestedDescriptionReplacement(LanguageDirectory, Stem) then
            continue;

          StalePath := AddBackslash(LanguageDirectory) + FlatRec.Name;
          if DeleteFile(StalePath) then
            Log('Removed stale OpenJiuwen description: ' + StalePath)
          else
            RaiseException(
              'Unable to remove stale OpenJiuwen description: ' + StalePath
            );
        until not FindNext(FlatRec);
      finally
        FindClose(FlatRec);
      end;
    until not FindNext(LanguageRec);
  finally
    FindClose(LanguageRec);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    exit;

  { The current per-user installation directory is writable by Setup. }
  CleanupStaleOpenJiuwenDescriptions();
end;

const
  WebView2RuntimeId = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2StandaloneUrl = 'https://go.microsoft.com/fwlink/?linkid=2124701';
  WebView2StandaloneFileName = 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe';
  WebView2DownloadPageUrl = 'https://developer.microsoft.com/microsoft-edge/webview2/';
  // Inno writes the uninstall entry as "<AppId>_is1" under HKLM. The old admin
  // install registers in the 64-bit view; WOW6432Node is checked as a fallback.
  LegacyUninstallNativeSubkey = 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{6DC96977-C194-44FE-812D-D4F0B576BD905}_is1';
  LegacyUninstallWowSubkey = 'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{6DC96977-C194-44FE-812D-D4F0B576BD905}_is1';

var
  WebView2DownloadPage: TDownloadWizardPage;

procedure InitializeWizard;
begin
  WebView2DownloadPage := CreateDownloadPage(
    '正在下载 Microsoft Edge WebView2 Runtime',
    '桌面 App 需要此运行环境。下载期间可以取消，WorkSwarm 主体安装仍可继续。',
    nil
  );
  WebView2DownloadPage.ShowBaseNameInsteadOfUrl := True;
  WebView2DownloadPage.AbortButton.Caption := '取消下载';
end;

function GetWebView2RuntimeVersion(var Version: String): Boolean;
var
  Key: String;
begin
  Key := 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2RuntimeId;
  Result := RegQueryStringValue(HKLM32, Key, 'pv', Version) and
    (Trim(Version) <> '') and (CompareText(Trim(Version), '0.0.0.0') <> 0);

  if not Result then
    Result := RegQueryStringValue(HKCU, Key, 'pv', Version) and
      (Trim(Version) <> '') and (CompareText(Trim(Version), '0.0.0.0') <> 0);
end;

procedure ShowWebView2UnavailableMessage(const Reason: String);
begin
  Log(Reason + ' WorkSwarm installation will continue without a usable WebView2 Runtime.');
  if WizardSilent then
    exit;

  MsgBox(
    Reason + #13#10 + #13#10 +
    '{#MyAppName} 将继续安装。安装完成后 Web 端仍可正常使用；' +
    '桌面 App 必须安装 Microsoft Edge WebView2 Runtime 才能正常运行。' + #13#10 + #13#10 +
    '可稍后从微软官方页面下载安装：' + #13#10 + WebView2DownloadPageUrl,
    mbError,
    MB_OK
  );
end;

function DownloadWebView2Installer(
  var RuntimeInstaller: String;
  var FailureReason: String
): Boolean;
var
  DownloadError: String;
begin
  Result := False;
  FailureReason := '';
  RuntimeInstaller := ExpandConstant('{tmp}\') + WebView2StandaloneFileName;
  DeleteFile(RuntimeInstaller);
  WebView2DownloadPage.Clear;
  WebView2DownloadPage.Add(
    WebView2StandaloneUrl,
    WebView2StandaloneFileName,
    ''
  );
  WebView2DownloadPage.Show;
  try
    try
      WebView2DownloadPage.Download;
      Result := FileExists(RuntimeInstaller);
      if not Result then
        Log('WebView2 Runtime download completed but the temporary file is missing.');
    except
      DownloadError := GetExceptionMessage;
      if WebView2DownloadPage.AbortedByUser then
      begin
        FailureReason := '已取消下载 Microsoft Edge WebView2 Runtime。';
        Log('WebView2 Runtime download was cancelled by the user.');
      end
      else
      begin
        FailureReason :=
          '未能下载 Microsoft Edge WebView2 Runtime。请检查网络、代理或防火墙设置。';
        Log('WebView2 Runtime download failed: ' + DownloadError);
      end;
    end;
  finally
    { Keep the completed download page visible while signature verification
      and the Microsoft child installer start. This avoids flashing back to
      the main wizard and then showing a second progress page. }
    if not Result then
      WebView2DownloadPage.Hide;
  end;
end;

function VerifyDownloadedWebView2Installer(const RuntimeInstaller: String): Boolean;
var
  PowerShellPath: String;
  ScriptPath: String;
  ScriptBody: AnsiString;
  ResultCode: Integer;
begin
  Result := False;
  PowerShellPath := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  ScriptPath := ExpandConstant('{tmp}\verify-workswarm-webview2.ps1');
  ScriptBody :=
    '$signature = Get-AuthenticodeSignature -LiteralPath $args[0]' + #13#10 +
    'if (($signature.Status -eq ''Valid'') -and $signature.SignerCertificate -and ' +
    '($signature.SignerCertificate.Subject -match ''(^|,\s*)O=Microsoft Corporation(,|$)'')) { exit 0 }' + #13#10 +
    'exit 1' + #13#10;

  if not SaveStringToFile(ScriptPath, ScriptBody, False) then
  begin
    Log('Could not create the WebView2 signature verification script.');
    exit;
  end;

  if not Exec(
    PowerShellPath,
    '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ScriptPath + '" "' + RuntimeInstaller + '"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  ) then
  begin
    Log('Could not start WebView2 signature verification.');
    exit;
  end;

  Result := ResultCode = 0;
  if not Result then
    Log('Downloaded WebView2 installer failed Microsoft Authenticode verification.');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  RuntimeVersion: String;
  RuntimeInstaller: String;
  ResultCode: Integer;
  Executed: Boolean;
  InstallParameters: String;
  InstallShowCmd: Integer;
  DownloadFailureReason: String;
begin
  Result := '';

  if GetWebView2RuntimeVersion(RuntimeVersion) then
  begin
    Log('Microsoft Edge WebView2 Runtime is already installed: ' + RuntimeVersion);
    exit;
  end;

  Log('Microsoft Edge WebView2 Runtime is missing; starting the online prerequisite flow.');
  if not WizardSilent then
    MsgBox(
      '未检测到可用的 Microsoft Edge WebView2 Runtime。' + #13#10 + #13#10 +
      '安装程序将从微软官方下载约 250 MB 的运行环境，并显示下载进度。' +
      '下载期间可以取消；如果网络不可用，WorkSwarm 主体安装仍会继续。',
      mbInformation,
      MB_OK
    );

  if not DownloadWebView2Installer(RuntimeInstaller, DownloadFailureReason) then
  begin
    if DownloadFailureReason = '' then
      DownloadFailureReason := 'Microsoft Edge WebView2 Runtime 下载文件不可用。';
    ShowWebView2UnavailableMessage(DownloadFailureReason);
    exit;
  end;

  WebView2DownloadPage.AbortButton.Enabled := False;
  WebView2DownloadPage.Caption := '正在验证 Microsoft Edge WebView2 Runtime';
  WebView2DownloadPage.Description := '正在校验微软数字签名，请稍候…';
  try
    if not VerifyDownloadedWebView2Installer(RuntimeInstaller) then
    begin
      DeleteFile(RuntimeInstaller);
      ShowWebView2UnavailableMessage(
        '下载的 Microsoft Edge WebView2 Runtime 未通过微软数字签名验证，已停止执行。'
      );
      exit;
    end;

    WebView2DownloadPage.Caption := '正在打开 Microsoft Edge WebView2 Runtime 安装程序';
    WebView2DownloadPage.Description :=
      '请在即将弹出的微软安装窗口中查看进度或取消安装。';
    ResultCode := -1;
    if WizardSilent then
    begin
      InstallParameters := '/silent /install';
      InstallShowCmd := SW_HIDE;
    end
    else
    begin
      InstallParameters := '/install';
      InstallShowCmd := SW_SHOWNORMAL;
    end;
    Executed := Exec(
      RuntimeInstaller,
      InstallParameters,
      '',
      InstallShowCmd,
      ewWaitUntilTerminated,
      ResultCode
    );
  finally
    WebView2DownloadPage.Hide;
  end;

  { The Evergreen installer can return a non-zero code even when registration
    succeeded. Verify Microsoft's documented pv value instead of trusting only
    the child exit code. }
  if GetWebView2RuntimeVersion(RuntimeVersion) then
  begin
    Log(
      'Microsoft Edge WebView2 Runtime installation verified: ' +
      RuntimeVersion + ' (installer exit code ' + IntToStr(ResultCode) + ')'
    );
    exit;
  end;

  if Executed then
    Log('WebView2 Runtime installer exit code: ' + IntToStr(ResultCode))
  else
    Log('WebView2 Runtime installer could not be started.');

  ShowWebView2UnavailableMessage(
    'Microsoft Edge WebView2 Runtime 未能完成安装或已被取消。'
  );
end;

// Migration: remove a previous admin-lineage install (AppId
// 6DC96977-C194-44FE-812D-D4F0B576BD905) that lives in Program Files + HKLM.
// A per-user (lowest) installer cannot delete those paths itself, so it drives
// the old uninstaller via runas. This triggers one UAC during migration only;
// fresh per-user installs run without any elevation.

function StripQuotes(const S: String): String;
var
  T: String;
begin
  T := Trim(S);
  if (Length(T) >= 2) and (Copy(T, 1, 1) = '"') and (Copy(T, Length(T), 1) = '"') then
    T := Copy(T, 2, Length(T) - 2);
  Result := Trim(T);
end;

function TryRegUninstaller(const Subkey: String; var ExePath: String): Boolean;
var
  Raw: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, Subkey, 'UninstallString', Raw) then
  begin
    ExePath := StripQuotes(Raw);
    Result := (ExePath <> '') and FileExists(ExePath);
  end;
end;

function TryFallbackUninstaller(const ProgramFilesRoot: String; const FolderName: String; var ExePath: String): Boolean;
var
  Candidate: String;
begin
  Candidate := ProgramFilesRoot + '\' + FolderName + '\unins000.exe';
  Result := FileExists(Candidate);
  if Result then
    ExePath := Candidate;
end;

function GetLegacyAdminUninstaller(var ExePath: String): Boolean;
var
  Pf64: String;
  Pf32: String;
begin
  Result := False;
  // 1) Registry first: covers any custom install folder name used by the old setup.
  if TryRegUninstaller(LegacyUninstallNativeSubkey, ExePath) then
    Result := True
  else if TryRegUninstaller(LegacyUninstallWowSubkey, ExePath) then
    Result := True
  else
  begin
    // 2) Fallback: well-known Program Files locations + current/former product names.
    Pf64 := GetEnv('ProgramFiles');
    if Pf64 = '' then Pf64 := 'C:\Program Files';
    Pf32 := GetEnv('ProgramFiles(x86)');
    if Pf32 = '' then Pf32 := GetEnv('ProgramFiles');
    if Pf32 = '' then Pf32 := 'C:\Program Files (x86)';
    if TryFallbackUninstaller(Pf64, '{#MyAppName}', ExePath) then
      Result := True
    else if TryFallbackUninstaller(Pf32, '{#MyAppName}', ExePath) then
      Result := True
    else if TryFallbackUninstaller(Pf64, 'JiuwenSwarm', ExePath) then
      Result := True
    else if TryFallbackUninstaller(Pf32, 'JiuwenSwarm', ExePath) then
      Result := True;
  end;
end;

function InitializeSetup: Boolean;
var
  LegacyUninstaller: String;
  ErrorCode: Integer;
begin
  Result := True;
  // No old admin install: clean per-user install, no UAC at all.
  if not GetLegacyAdminUninstaller(LegacyUninstaller) then
    Exit;
  // Old admin install detected. Removing Program Files + HKLM requires admin,
  // so we drive the old uninstaller with runas (one UAC during migration).
  if MsgBox('检测到旧版本（管理员安装，位于 Program Files）。' + #13#10 +
            '安装新版前将卸载旧版本，期间会弹出权限确认，请点击“是”。' + #13#10 +
            '若应用正在运行，请先关闭后再继续。' + #13#10 + #13#10 +
            '是否现在卸载旧版本？',
            mbConfirmation, MB_YESNO) <> IDYES then
  begin
    Result := False;
    Exit;
  end;
  // The old unins000.exe carries an admin manifest; runas lets it remove its
  // own Program Files tree + HKLM entry. /VERYSILENT keeps it headless.
  if not ShellExec('runas', LegacyUninstaller,
                   '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES', '',
                   SW_HIDE, ewWaitUntilTerminated, ErrorCode) then
  begin
    MsgBox('启动旧版本卸载程序失败（错误码：' + IntToStr(ErrorCode) + '）。' + #13#10 +
           '请手动卸载旧版本后再运行本安装程序。', mbError, MB_OK);
    Result := False;
    Exit;
  end;
  // Inno's uninstaller copies itself to TEMP and finishes asynchronously, so a
  // completion check here would false-positive: the registry entry is removed
  // near the end of the temp copy, after this call returns. The new per-user
  // install targets {localappdata}\Programs and is independent of the old
  // Program Files tree, so installation proceeds regardless. If the app was
  // still running the new setup's own AppMutex guard will have blocked startup.
end;
