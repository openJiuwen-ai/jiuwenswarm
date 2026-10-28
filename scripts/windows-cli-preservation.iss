// Shared pre-delete preservation. The helper is never elevated by this hook.
function PreserveWindowsCli(const InstallDirectory, Mode: String): Boolean;
var
  ScriptPath: String;
  ReportPath: String;
  ResultCode: Integer;
begin
  Result := False;
  ScriptPath := ExpandConstant('{tmp}\preserve-windows-cli.ps1');
  if not FileExists(ScriptPath) then
    ExtractTemporaryFile('preserve-windows-cli.ps1');
  ReportPath := ExpandConstant('{tmp}\workswarm-cli-preservation.log');
  if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ScriptPath + '" -InstallDir "' + RemoveBackslashUnlessRoot(InstallDirectory) +
      '" -Mode ' + Mode + ' -ReportPath "' + ReportPath + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode = 0;
  if not Result then
    if Mode = 'Preserve' then
      MsgBox('External CLI preservation failed. Installation will stop before cleanup.' +
        Chr(13) + Chr(10) + 'Details: ' + ReportPath, mbError, MB_OK)
    else
      MsgBox('External CLI initialization failed. Recovery copies remain in the user data directory.' +
        Chr(13) + Chr(10) + 'Details: ' + ReportPath, mbError, MB_OK);
end;

function RestoreWindowsCliConfiguration(const BackendPath: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(BackendPath, '--desktop-restore-upgrade-config', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := ResultCode = 0;
  if not Result then
    MsgBox('Legacy agent configuration recovery failed. The encrypted snapshot remains in the user data directory.', mbError, MB_OK);
end;
