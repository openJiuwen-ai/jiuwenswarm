; JiuwenSwarm Electron Installer Script (Windows)
; 用法: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\installer-electron.iss
; 版本号 / 后端 exe 名由 build-electron-exe.ps1 经 /DMyAppVersion=... /
; /DBackendExecutableName=... 传入（来自 build_config，与 pyproject 单一来源）；
; 直接编译且未传 /D 时使用下方默认值。

#define MyAppName "JiuwenSwarm"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef BackendExecutableName
  #define BackendExecutableName "workswarm.exe"
#endif
#define MyAppPublisher "openJiuwen"
#define MyAppExeName "JiuwenSwarm.exe"
#define MyAppURL "https://openjiuwen.com"

[Setup]
AppId={{B8F3A2D1-7E4C-4A9B-8D6F-1C2E3F4A5B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=..\dist
#ifdef ELECTRON_FRONTEND_ONLY
  OutputBaseFilename=JiuwenSwarm-frontend-test-{#MyAppVersion}
#else
  #ifdef ELECTRON_TEST_BUILD
    OutputBaseFilename=JiuwenSwarm-test-{#MyAppVersion}
  #else
    OutputBaseFilename=JiuwenSwarm-setup-{#MyAppVersion}
  #endif
#endif
SetupIconFile=..\jiuwenswarm\channels\web\frontend\public\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
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
Source: "..\dist\JiuwenSwarm-Electron\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

#ifndef ELECTRON_FRONTEND_ONLY
[UninstallRun]
; 卸载前重置外部 CLI 开关（与 installer.iss 一致；Web UI 也能安装外部 CLI，
; 其配置在用户目录，需要后端 exe 显式清理）。FrontendOnly 包无后端，跳过。
Filename: "{app}\resources\backend\{#BackendExecutableName}"; Parameters: "--desktop-reset-external-cli-config"; Flags: runhidden waituntilterminated; RunOnceId: "ResetExternalCliConfig"
#endif

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\resources\app\logo.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; IconFilename: "{app}\resources\app\logo.ico"

[UninstallDelete]
; 清理运行期可能落在安装目录内的文件（后端子进程的工作目录在 resources\backend
; 下），保证卸载后目录可完整移除；用户配置在 ~/.jiuwenswarm，不受影响。
Type: filesandordirs; Name: "{app}\resources"
Type: files; Name: "{app}\{#MyAppExeName}"
Type: dirifempty; Name: "{app}"

[Run]
Filename: "{win}\explorer.exe"; Parameters: """{app}\{#MyAppExeName}"""; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall
