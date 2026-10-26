; JiuwenSwarm Inno Setup Installer Script
; 用法: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" scripts\installer.iss

#define MyAppName "JiuwenSwarm"
#define MyAppVersion "0.2.3"
#define MyAppPublisher "openJiuwen"
#define MyAppExeName "jiuwenswarm.exe"
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
OutputBaseFilename=JiuwenSwarm-setup-{#MyAppVersion}
SetupIconFile=..\jiuwenswarm\channels\web\frontend\public\logo.ico
UninstallDisplayIcon={app}\jiuwenswarm.exe
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\jiuwenswarm\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; 通过 Explorer 代启动程序，使安装完成后的启动上下文更接近桌面快捷方式启动
; postinstall 在安装向导最后一页显示"运行 JiuwenSwarm"复选框，由用户决定是否启动
Filename: "{win}\explorer.exe"; Parameters: """{app}\{#MyAppExeName}"""; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall
