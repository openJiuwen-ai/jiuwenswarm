#define MyAppName "JiuwenClaw"
#define MyAppVersion "0.1.7"
#define MyAppPublisher "JiuwenClaw"
#define MyAppURL "https://github.com/"
#define MyAppExeName "jiuwenclaw.exe"
#define MyDistDir "..\dist\jiuwenclaw"

[Setup]
AppId={{6DDF1C96-B2CE-4A2F-A7E7-A2E8627AE0A2}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
SetupLogging=yes
OutputDir=..\dist\installer
OutputBaseFilename=jiuwenclaw-setup-{#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}

; 如果后续补了 ico，可以取消下面两行注释并指向同一图标文件
; SetupIconFile=..\assets\jiuwenclaw.ico
; WizardSmallImageFile=..\assets\jiuwenclaw.bmp

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "{#MyDistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 默认不删除用户数据目录，避免误删配置和日志。
; 如果你需要在卸载时清理缓存，可按需增加删除规则。

[Code]
function UserWorkspaceDir(): string;
begin
  Result := ExpandConstant('{userappdata}') + '\..\.jiuwenclaw';
end;
