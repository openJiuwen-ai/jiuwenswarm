// Shared pre-delete preservation. The helper is never elevated by this hook.
procedure ShowWindowsCliStatus(const Text: String);
begin
  // Match the English-only wizard used by both installers.
  if WizardForm.CurPageID = wpInstalling then
  begin
    WizardForm.StatusLabel.Caption := Text;
    WizardForm.FilenameLabel.Caption := '';
    WizardForm.StatusLabel.Visible := True;
    WizardForm.FilenameLabel.Visible := True;
    WizardForm.StatusLabel.Repaint;
    WizardForm.FilenameLabel.Repaint;
  end;
end;

function PreserveWindowsCli(const InstallDirectory, Mode: String): Boolean;
var
  ScriptPath: String;
  ReportPath: String;
  ResultCode: Integer;
begin
  Result := False;
  ResultCode := -1;
  ScriptPath := ExpandConstant('{tmp}\preserve-windows-cli.ps1');
  if not FileExists(ScriptPath) then
    ExtractTemporaryFile('preserve-windows-cli.ps1');
  ReportPath := ExpandConstant('{tmp}\workswarm-cli-preservation.log');
  if Mode = 'Initialize' then
    ShowWindowsCliStatus('Initializing CLI tools. First-time setup may take a few minutes...');
  Log('[CLI] Begin ' + Mode);
  if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ScriptPath + '" -InstallDir "' + RemoveBackslashUnlessRoot(InstallDirectory) +
      '" -Mode ' + Mode + ' -ReportPath "' + ReportPath + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode = 0;
  Log('[CLI] End ' + Mode + '; success=' + IntToStr(Ord(Result)) +
    '; result code=' + IntToStr(ResultCode));
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
  ResultCode := -1;
  ShowWindowsCliStatus('Restoring upgrade settings. Please wait...');
  Log('[CLI] Begin RestoreConfiguration');
  Result := Exec(BackendPath, '--desktop-restore-upgrade-config', '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := ResultCode = 0;
  Log('[CLI] End RestoreConfiguration; success=' + IntToStr(Ord(Result)) +
    '; result code=' + IntToStr(ResultCode));
  if not Result then
    MsgBox('Legacy agent configuration recovery failed. The encrypted snapshot remains in the user data directory.', mbError, MB_OK);
end;
