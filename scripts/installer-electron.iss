; JiuwenSwarm Electron Installer Script (Windows)
; 用法: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\installer-electron.iss
; 应用名 / 版本号 / 壳 exe 名 / 后端 exe 名由 build-electron-exe.ps1 经
; /DMyAppName=... /DMyAppVersion=... /DMyAppExeName=... /DBackendExecutableName=...
; 传入（来自 build_config，与 pyproject 单一来源，与 installer.iss 的 WorkSwarm
; 显示名一致）；直接编译且未传 /D 时使用下方默认值。

#ifndef MyAppName
  #define MyAppName "WorkSwarm"
#endif
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyAppExeName
  #define MyAppExeName "WorkSwarm.exe"
#endif
#ifndef BackendExecutableName
  #define BackendExecutableName "workswarm.exe"
#endif
#define MyAppPublisher "openJiuwen"
#define MyAppURL "https://openjiuwen.com"
; 项目根目录 = 脚本所在目录（scripts）的上一级。ISCC 不折叠 "..\"，相对路径会
; 带着 "scripts\..\" 原样参与文件扫描，叠加 Electron 包内 node_modules 的深层
; 路径后可触顶 Windows MAX_PATH(260)，报"系统找不到指定的路径"；因此 dist 与
; 图标统一用基于 SourcePath（脚本所在目录，ISPP 预定义变量）的绝对路径引用。
#define ProjectRoot ExtractFileDir(RemoveBackslashUnlessRoot(SourcePath))

[Setup]
AppId={{B8F3A2D1-7E4C-4A9B-8D6F-1C2E3F4A5B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir={#ProjectRoot}\dist
#ifdef ELECTRON_FRONTEND_ONLY
  OutputBaseFilename={#MyAppName}-frontend-test-{#MyAppVersion}
#else
  #ifdef ELECTRON_TEST_BUILD
    OutputBaseFilename={#MyAppName}-test-{#MyAppVersion}
  #else
    OutputBaseFilename={#MyAppName}-setup-{#MyAppVersion}
  #endif
#endif
SetupIconFile={#ProjectRoot}\jiuwenswarm\channels\web\frontend\public\logo.ico
UninstallDisplayIcon={app}\logo-{#MyAppVersion}.ico
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
; per-user 安装（LOCALAPPDATA\Programs），无需 UAC 提权
PrivilegesRequired=lowest
; 与 installer.iss 一致：冻结后端子进程终身持有这些命名互斥体
; （jiuwenswarm_exe_entry.py），Setup/Uninstall 在应用运行时直接拒绝，
; 而不是靠 CloseApplications 强杀丢掉未保存的界面状态。
AppMutex=JiuwenSwarm.App,Global\JiuwenSwarm.App,WorkSwarm.App,Global\WorkSwarm.App
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
DisableDirPage=no
DisableProgramGroupPage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#ProjectRoot}\dist\{#MyAppName}-Electron\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ProjectRoot}\jiuwenswarm\channels\web\frontend\public\logo.ico"; DestDir: "{app}"; DestName: "logo-{#MyAppVersion}.ico"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{app}\logo-*.ico"

#ifndef ELECTRON_FRONTEND_ONLY
[UninstallRun]
; 卸载前重置外部 CLI 开关（与 installer.iss 一致；Web UI 也能安装外部 CLI，
; 其配置在用户目录，需要后端 exe 显式清理）。FrontendOnly 包无后端，跳过。
Filename: "{app}\resources\backend\{#BackendExecutableName}"; Parameters: "--desktop-reset-external-cli-config"; Flags: runhidden waituntilterminated; RunOnceId: "ResetExternalCliConfig"
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\logo-{#MyAppVersion}.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\logo-{#MyAppVersion}.ico"

[UninstallDelete]
; 清理运行期可能落在安装目录内的文件（后端子进程的工作目录在 resources\backend
; 下），保证卸载后目录可完整移除；用户配置在 ~/.jiuwenswarm，不受影响。
Type: filesandordirs; Name: "{app}\resources"
Type: files; Name: "{app}\{#MyAppExeName}"
Type: dirifempty; Name: "{app}"

[Run]
Filename: "{win}\explorer.exe"; Parameters: """{app}\{#MyAppExeName}"""; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall

[Code]
// ─── 旧版迁移（对齐 installer.iss 的迁移语义并扩展 per-user 旧版）──────────
// 旧 Python(pywebview) 桌面版使用 AppId 6DC96977-…；Electron 壳是全新 AppId，
// Inno 不会自动替换旧版，且两者默认目录同为 {localappdata}\Programs\{#MyAppName}，
// 不卸载旧版会把两套文件混进同一目录。管理员旧版（Program Files + HKLM）本
// per-user 安装器无权删除，经 runas 驱动旧卸载器（仅迁移这一次 UAC）；2026-09
// 起的 per-user 旧版无需提权，直接静默卸载并等待其真正收尾（旧卸载器会自复制
// 到 TEMP 异步执行，注册表项临近结束时才删除）。全新机器零弹窗、零提权。

const
  LegacyUninstallSubkey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6DC96977-C194-44FE-812D-D4F0B576BD905}_is1';
  LegacyUninstallWowSubkey = 'Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{6DC96977-C194-44FE-812D-D4F0B576BD905}_is1';
  LegacyUserUninstallPollIntervalMs = 500;
  LegacyUserUninstallWaitAttempts = 240;

function StripQuotes(const S: String): String;
var
  T: String;
begin
  T := Trim(S);
  if (Length(T) >= 2) and (Copy(T, 1, 1) = '"') and (Copy(T, Length(T), 1) = '"') then
    T := Copy(T, 2, Length(T) - 2);
  Result := Trim(T);
end;

function TryRegUninstaller(const RootKey: Integer; const Subkey: String; var ExePath: String): Boolean;
var
  Raw: String;
begin
  Result := False;
  if RegQueryStringValue(RootKey, Subkey, 'UninstallString', Raw) then
  begin
    ExePath := StripQuotes(Raw);
    Result := (ExePath <> '') and FileExists(ExePath);
  end;
end;

function TryFallbackUninstaller(const ProgramFilesRoot, FolderName: String; var ExePath: String): Boolean;
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
  // 1) 注册表优先：覆盖旧安装器允许的任意自定义安装目录。
  if TryRegUninstaller(HKLM, LegacyUninstallSubkey, ExePath) then
    Result := True
  else if TryRegUninstaller(HKLM, LegacyUninstallWowSubkey, ExePath) then
    Result := True
  else
  begin
    // 2) 兜底：Program Files 常见位置 + 当前/历史产品名。
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

function GetLegacyUserUninstaller(var ExePath: String): Boolean;
begin
  // 仅注册表：per-user 旧版必有 HKCU 卸载项。不做目录兜底——旧版与
  // 本安装器默认同目录，按目录找 unins000.exe 会误伤 Electron 自己。
  Result := TryRegUninstaller(HKCU, LegacyUninstallSubkey, ExePath) or
    TryRegUninstaller(HKCU, LegacyUninstallWowSubkey, ExePath);
end;

function LegacyUserUninstallEntryExists(): Boolean;
var
  Raw: String;
begin
  Result := RegQueryStringValue(HKCU, LegacyUninstallSubkey, 'UninstallString', Raw) or
    RegQueryStringValue(HKCU, LegacyUninstallWowSubkey, 'UninstallString', Raw);
end;

function WaitForLegacyUserUninstall(): Boolean;
var
  Attempt: Integer;
begin
  if not LegacyUserUninstallEntryExists() then
  begin
    Result := True;
    Exit;
  end;
  // 240 × 500ms = 120s 上限；大包删除可能较慢，通常数秒内完成。
  for Attempt := 1 to LegacyUserUninstallWaitAttempts do
  begin
    Sleep(LegacyUserUninstallPollIntervalMs);
    if not LegacyUserUninstallEntryExists() then
    begin
      Result := True;
      Exit;
    end;
  end;
  Result := False;
end;

function RunLegacyUninstaller(const ExePath: String; const AdminLineage: Boolean): Boolean;
var
  ErrorCode: Integer;
  PromptText: String;
begin
  Result := False;

  if AdminLineage then
    PromptText :=
      '检测到旧版本（管理员安装，位于 Program Files）。' + #13#10 +
      '安装新版前将卸载旧版本，期间会弹出权限确认，请点击“是”。'
  else
    PromptText :=
      '检测到旧版本（本机用户安装）。' + #13#10 +
      '安装新版前将卸载旧版本。';

  if MsgBox(PromptText + #13#10 +
            '若应用正在运行，请先关闭后再继续。' + #13#10 + #13#10 +
            '是否现在卸载旧版本？',
            mbConfirmation, MB_YESNO) <> IDYES then
    Exit;

  if AdminLineage then
  begin
    // 旧 unins000.exe 携带管理员清单；runas 让它删除自己的 Program Files
    // 目录与 HKLM 项。/VERYSILENT 保持无界面。
    if not ShellExec('runas', ExePath,
                     '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES', '',
                     SW_HIDE, ewWaitUntilTerminated, ErrorCode) then
    begin
      MsgBox('启动旧版本卸载程序失败（错误码：' + IntToStr(ErrorCode) + '）。' + #13#10 +
             '请手动卸载旧版本后再运行本安装程序。', mbError, MB_OK);
      Exit;
    end;
    // 旧卸载器自复制到 TEMP 异步收尾，此处立即检查完成会误判（注册表项在
    // 临时副本末尾才删除）。管理员旧版在 Program Files，与本安装器的
    // per-user 目录互不重叠，直接继续安装即可。
  end
  else
  begin
    // per-user 旧版无需提权，静默卸载。
    if not Exec(ExePath,
                '/VERYSILENT /NORESTART /SUPPRESSMSGBOXES', '',
                SW_HIDE, ewWaitUntilTerminated, ErrorCode) then
    begin
      MsgBox('启动旧版本卸载程序失败（错误码：' + IntToStr(ErrorCode) + '）。' + #13#10 +
             '请手动卸载旧版本后再运行本安装程序。', mbError, MB_OK);
      Exit;
    end;
    // per-user 旧版目录与本安装器默认目录相同，必须等旧卸载器真正收尾
    //（注册表项消失）后再继续，避免删除旧文件与写入新文件竞争。
    if not WaitForLegacyUserUninstall() then
    begin
      MsgBox('卸载旧版本超时。请等待旧版本完全卸载后，重新运行本安装程序。',
             mbError, MB_OK);
      Exit;
    end;
  end;

  Result := True;
end;

function InitializeSetup: Boolean;
var
  AdminUninstaller: String;
  UserUninstaller: String;
begin
  Result := True;
  // 先迁移管理员旧版（如有），再迁移 per-user 旧版（如有）；全部成功才继续。
  if GetLegacyAdminUninstaller(AdminUninstaller) then
    if not RunLegacyUninstaller(AdminUninstaller, True) then
    begin
      Result := False;
      Exit;
    end;
  if GetLegacyUserUninstaller(UserUninstaller) then
    if not RunLegacyUninstaller(UserUninstaller, False) then
    begin
      Result := False;
      Exit;
    end;
end;
