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
AppId={{B8F3A2D1-7E4C-4A9B-8D6F-1C2E3F4A5B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename={#BuildSetupBaseName}
SetupIconFile=..\jiuwenswarm\channels\web\frontend\public\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Keep the legacy mutexes as a stable upgrade protocol for the existing AppId.
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

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[UninstallDelete]
; Remove only application-owned paths that may gain runtime-generated files.
; User data lives outside {app}, under ~/.jiuwenswarm, and is intentionally kept.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\runtime"
Type: files; Name: "{app}\{#MyAppExeName}"
Type: dirifempty; Name: "{app}"

[Run]
; 通过 Explorer 代启动程序，使安装完成后的启动上下文更接近桌面快捷方式启动
; postinstall 在安装向导最后一页显示运行应用复选框，由用户决定是否启动
Filename: "{win}\explorer.exe"; Parameters: """{app}\{#MyAppExeName}"""; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall
