// The install presentation stays on the wizard, outside all notebook pages.
type
  // Inno Setup 6 runs its script/DLL calls in a 32-bit process, including x64 installs.
  TCliStartupInfo = record
    Size: LongWord;
    Reserved, Desktop, Title: LongWord;
    X, Y, XSize, YSize, XChars, YChars, Fill, Flags: LongWord;
    ShowWindow, ReservedSize: Word;
    ReservedData, Input, Output, Error: LongWord;
  end;
  TCliProcessInfo = record
    Process, Thread: LongWord;
    ProcessId, ThreadId: LongWord;
  end;
  TCliMessage = record
    Window, Message, WParam, LParam, Time: LongWord;
    X, Y: Longint;
  end;

function CliCreateProcess(ApplicationName, CommandLine: String;
  ProcessAttributes, ThreadAttributes: LongWord; InheritHandles: Boolean;
  CreationFlags: LongWord; Environment, CurrentDirectory: LongWord;
  var StartupInfo: TCliStartupInfo; var ProcessInfo: TCliProcessInfo): Boolean;
  external 'CreateProcessW@kernel32.dll stdcall';
function CliWaitForSingleObject(Handle: LongWord; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CliGetExitCodeProcess(Handle: LongWord; var Code: LongWord): Boolean;
  external 'GetExitCodeProcess@kernel32.dll stdcall';
function CliCloseHandle(Handle: LongWord): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function CliPeekMessage(var Msg: TCliMessage; Window, Min, Max, Remove: LongWord): Boolean;
  external 'PeekMessageW@user32.dll stdcall';
function CliTranslateMessage(var Msg: TCliMessage): Boolean;
  external 'TranslateMessage@user32.dll stdcall';
function CliDispatchMessage(var Msg: TCliMessage): Longint;
  external 'DispatchMessageW@user32.dll stdcall';
function CliIsDialogMessage(Window: LongWord; var Msg: TCliMessage): Boolean;
  external 'IsDialogMessageW@user32.dll stdcall';
procedure CliPostQuitMessage(Code: Integer);
  external 'PostQuitMessage@user32.dll stdcall';

var
  CliProgressContainer: TPanel;
  CliStatusLabel, CliDetailLabel: TNewStaticText;
  CliProgressGauge: TNewProgressBar;
  CliTotalProgress: Integer;
  CliExtractionStart: Integer;
  CliStage: Integer; // 0: inactive, 1: preserve, 2: extract, 3: initialize, 4: done, 5: prerequisite
  CliHelperRunning: Boolean;

procedure EnsureCliProgress;
begin
  if CliProgressContainer <> nil then Exit;
  CliProgressContainer := TPanel.Create(WizardForm);
  CliProgressContainer.Visible := False;
  CliProgressContainer.Parent := WizardForm;
  CliProgressContainer.BevelOuter := bvNone;
  CliProgressContainer.ParentBackground := False;
  // Notebook pages paint the window background through the theme, while their
  // Color property can still be clBtnFace. Copying that property leaves a gray
  // strip between the filename label and the progress bar on the common panel.
  CliProgressContainer.Color := clWindow;
  CliProgressContainer.SetBounds(
    WizardForm.OuterNotebook.Left + WizardForm.InnerPage.Left + WizardForm.InnerNotebook.Left,
    WizardForm.OuterNotebook.Top + WizardForm.InnerPage.Top + WizardForm.InnerNotebook.Top,
    WizardForm.InnerNotebook.Width,
    WizardForm.ProgressGauge.Top + WizardForm.ProgressGauge.Height);
  CliStatusLabel := TNewStaticText.Create(WizardForm);
  CliStatusLabel.Parent := CliProgressContainer;
  CliStatusLabel.AutoSize := False;
  CliStatusLabel.ShowAccelChar := False;
  CliStatusLabel.WordWrap := True;
  CliStatusLabel.Font.Assign(WizardForm.StatusLabel.Font);
  CliStatusLabel.SetBounds(WizardForm.StatusLabel.Left, WizardForm.StatusLabel.Top,
    WizardForm.StatusLabel.Width, WizardForm.StatusLabel.Height);
  CliDetailLabel := TNewStaticText.Create(WizardForm);
  CliDetailLabel.Parent := CliProgressContainer;
  CliDetailLabel.AutoSize := False;
  CliDetailLabel.ShowAccelChar := False;
  CliDetailLabel.Font.Assign(WizardForm.FilenameLabel.Font);
  CliDetailLabel.SetBounds(WizardForm.FilenameLabel.Left, WizardForm.FilenameLabel.Top,
    WizardForm.FilenameLabel.Width, WizardForm.FilenameLabel.Height);
  CliProgressGauge := TNewProgressBar.Create(WizardForm);
  CliProgressGauge.Parent := CliProgressContainer;
  CliProgressGauge.SetBounds(WizardForm.ProgressGauge.Left, WizardForm.ProgressGauge.Top,
    WizardForm.ProgressGauge.Width, WizardForm.ProgressGauge.Height);
  // Set Max once. Reparenting/recreating or resetting the range at transitions
  // can restart the native Windows progress animation from an empty bar.
  CliProgressGauge.Max := 1000;
  CliExtractionStart := 300;
end;

procedure AbortCliProgress;
begin
  if CliProgressContainer <> nil then
  begin
    CliProgressContainer.Visible := False;
    CliProgressGauge.Position := 0;
  end;
  CliStage := 0;
  CliTotalProgress := 0;
  CliExtractionStart := 300;
end;

procedure PumpCliMessages;
var
  Msg: TCliMessage;
begin
  while CliPeekMessage(Msg, 0, 0, 0, 1) do
  begin
    if Msg.Message = $0012 then
    begin
      // Preserve a pending shutdown request for the outer installer loop.
      CliPostQuitMessage(Msg.WParam);
      Break;
    end;
    if not CliIsDialogMessage(WizardForm.Handle, Msg) then
    begin
      CliTranslateMessage(Msg);
      CliDispatchMessage(Msg);
    end;
  end;
end;

procedure SetCliProgress(const Value: Integer);
begin
  EnsureCliProgress;
  if Value > CliTotalProgress then
  begin
    CliTotalProgress := Value;
    CliProgressGauge.Position := CliTotalProgress;
    Log('[CLI] Overall progress: ' + IntToStr(Value) + '/1000');
  end;
end;

<event('CurInstallProgressChanged')>
procedure CliInstallProgressChanged(CurProgress, MaxProgress: Integer);
begin
  EnsureCliProgress;
  if (CliStage = 2) and (MaxProgress > 0) then
  begin
    if WizardForm.StatusLabel.Caption <> '' then
      CliStatusLabel.Caption := WizardForm.StatusLabel.Caption;
    CliDetailLabel.Caption := WizardForm.FilenameLabel.Caption;
    SetCliProgress(CliExtractionStart + Round((850.0 - CliExtractionStart) * CurProgress / MaxProgress));
  end;
end;

<event('CurPageChanged')>
procedure CliPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpInstalling then
  begin
    EnsureCliProgress;
    CliStage := 2;
    CliProgressContainer.Visible := True;
    CliProgressContainer.BringToFront;
    CliStatusLabel.Caption := 'Installing application files...';
    CliDetailLabel.Caption := '';
    SetCliProgress(CliExtractionStart);
  end
  else if (CurPageID = wpFinished) and (CliProgressContainer <> nil) then
    // Only retire the common presentation after installation has completed.
    CliProgressContainer.Visible := False
  else if (CurPageID < wpPreparing) and (CliStage <> 0) and not CliHelperRunning then
    AbortCliProgress;
end;

procedure ShowCliPrerequisiteStatus(const Status, Detail: String);
begin
  EnsureCliProgress;
  CliStage := 5;
  CliExtractionStart := 400;
  CliStatusLabel.Caption := Status;
  CliDetailLabel.Caption := Detail;
  WizardForm.Enabled := True;
  CliProgressContainer.Visible := True;
  CliProgressContainer.BringToFront;
end;

procedure UpdateCliPrerequisiteProgress(const Progress, MaxProgress: Int64);
begin
  if CliStage <> 5 then Exit;
  if MaxProgress > 0 then
    SetCliProgress(300 + Round(100.0 * Progress / MaxProgress));
end;

<event('CancelButtonClick')>
procedure CliCancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  // Pumping messages must not allow close/cancel to interrupt a recovery copy.
  if CliHelperRunning or (CliStage = 1) then begin Cancel := False; Confirm := False; end;
end;

function RunCliProcess(const Filename, Parameters, ProgressPath: String;
  const StartProgress, EndProgress: Integer; var ResultCode: Integer): Boolean;
var
  Startup: TCliStartupInfo;
  Child: TCliProcessInfo;
  ExitCode, WaitResult: LongWord;
  Lines: TArrayOfString;
  Percent: Integer;
  NextEnabled, BackEnabled, CancelEnabled: Boolean;
begin
  Result := False;
  ResultCode := -1;
  EnsureCliProgress;
  NextEnabled := WizardForm.NextButton.Enabled;
  BackEnabled := WizardForm.BackButton.Enabled;
  CancelEnabled := WizardForm.CancelButton.Enabled;
  if StartProgress >= 850 then CliStage := 3 else CliStage := 1;
  CliHelperRunning := True;
  try
    // PrepareToInstall normally disables the whole wizard. Only navigation is
    // locked here; the title bar stays available while the helper runs.
    WizardForm.Enabled := True;
    WizardForm.NextButton.Enabled := False;
    WizardForm.BackButton.Enabled := False;
    WizardForm.CancelButton.Enabled := False;
    CliProgressContainer.Visible := True;
    CliProgressContainer.BringToFront;
    SetCliProgress(StartProgress);
    PumpCliMessages;
    Startup.Size := SizeOf(Startup);
    Startup.Reserved := 0;
    Startup.Desktop := 0;
    Startup.Title := 0;
    Startup.X := 0;
    Startup.Y := 0;
    Startup.XSize := 0;
    Startup.YSize := 0;
    Startup.XChars := 0;
    Startup.YChars := 0;
    Startup.Fill := 0;
    Startup.Flags := 1; // STARTF_USESHOWWINDOW
    Startup.ShowWindow := SW_HIDE;
    Startup.ReservedSize := 0;
    Startup.ReservedData := 0;
    Startup.Input := 0;
    Startup.Output := 0;
    Startup.Error := 0;
    if not CliCreateProcess(Filename, '"' + Filename + '" ' + Parameters,
      0, 0, False, $08000000, 0, 0, Startup, Child) then Exit;
    CliCloseHandle(Child.Thread);
    try
      repeat
        WaitResult := CliWaitForSingleObject(Child.Process, 0);
        if WaitResult = $FFFFFFFF then Exit;
        if (ProgressPath <> '') and LoadStringsFromFile(ProgressPath, Lines) then
          if GetArrayLength(Lines) >= 3 then
          begin
            Percent := StrToIntDef(Lines[0], -1);
            if (Percent >= 0) and (Percent <= 100) then
            begin
              CliStatusLabel.Caption := Lines[1];
              CliDetailLabel.Caption := Lines[2];
              SetCliProgress(StartProgress + (EndProgress - StartProgress) * Percent div 100);
            end;
          end;
        // Keep the common controls alive and process title-bar input even when
        // no new progress report is available. Navigation remains locked.
        PumpCliMessages;
        if WaitResult <> 0 then Sleep(50);
      until WaitResult = 0;
      if CliGetExitCodeProcess(Child.Process, ExitCode) then
      begin
        ResultCode := ExitCode;
        Result := ExitCode = 0;
      end;
      if Result then SetCliProgress(EndProgress);
    finally
      CliCloseHandle(Child.Process);
    end;
  finally
    WizardForm.NextButton.Enabled := NextEnabled;
    WizardForm.BackButton.Enabled := BackEnabled;
    WizardForm.CancelButton.Enabled := CancelEnabled;
    CliHelperRunning := False;
    if not Result then
      AbortCliProgress;
  end;
end;

procedure ShowWindowsCliStatus(const Text: String);
begin
  EnsureCliProgress;
  CliStatusLabel.Caption := Text;
  CliDetailLabel.Caption := 'Please wait. Your CLI environment is being kept safe.';
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
  ProgressPath: String;
  ResultCode: Integer;
  StartProgress, EndProgress: Integer;
begin
  Result := False;
  ResultCode := -1;
  ScriptPath := ExpandConstant('{tmp}\preserve-windows-cli.ps1');
  if not FileExists(ScriptPath) then
    ExtractTemporaryFile('preserve-windows-cli.ps1');
  ReportPath := ExpandConstant('{tmp}\workswarm-cli-preservation.log');
  ProgressPath := ExpandConstant('{tmp}\workswarm-cli-progress.txt');
  DeleteFile(ProgressPath);
  StartProgress := 0;
  EndProgress := 300;
  if Mode = 'Initialize' then
  begin
    StartProgress := 850;
    EndProgress := 980;
    ShowWindowsCliStatus('Initializing CLI tools...');
  end
  else if Mode = 'PreserveConfig' then
    ShowWindowsCliStatus('Preserving upgrade settings...')
  else
    ShowWindowsCliStatus('Preserving CLI environment...');
  Log('[CLI] Begin ' + Mode);
  Result := RunCliProcess(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      ScriptPath + '" -InstallDir "' + RemoveBackslashUnlessRoot(InstallDirectory) +
      '" -Mode ' + Mode + ' -ReportPath "' + ReportPath +
      '" -ProgressPath "' + ProgressPath + '"',
      ProgressPath, StartProgress, EndProgress, ResultCode);
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
  Result := RunCliProcess(BackendPath, '--desktop-restore-upgrade-config', '',
    980, 990, ResultCode);
  Log('[CLI] End RestoreConfiguration; success=' + IntToStr(Ord(Result)) +
    '; result code=' + IntToStr(ResultCode));
  if not Result then
    MsgBox('Legacy agent configuration recovery failed. The encrypted snapshot remains in the user data directory.', mbError, MB_OK);
end;

procedure FinishCliProgress;
begin
  EnsureCliProgress;
  CliStage := 4;
  SetCliProgress(1000);
  CliProgressGauge.Repaint;
end;
