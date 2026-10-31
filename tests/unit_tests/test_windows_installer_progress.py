"""Exercise the installer process boundary without changing the installed app."""

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _run_edge_installer(tmp_path, monkeypatch, body, *, visible=False):
    compiler = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe"
    if not compiler.is_file():
        pytest.skip("Inno Setup is not installed")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    shutil.copyfile(ROOT / "scripts/windows-cli-preservation.iss", tmp_path / "windows-cli-preservation.iss")
    source = tmp_path / "edge.iss"
    source.write_text(r'''
[Setup]
WizardStyle=modern
AppName=Installer edge regression
AppVersion=0.0.0
DefaultDirName={sd}\unused-edge-test
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
OutputDir=.
OutputBaseFilename=edge
[Code]
#include "windows-cli-preservation.iss"
function TestVisible(Window: LongWord): Boolean;
  external 'IsWindowVisible@user32.dll stdcall';
function TestSendMessage(Window, Msg, WParam, LParam: LongWord): LongWord;
  external 'SendMessageW@user32.dll stdcall';
procedure PreserveFixture;
var Code: Integer;
begin
  ShowWindowsCliStatus('Preserving CLI environment...');
  if not RunCliProcess(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -Command "Start-Sleep -Milliseconds 100"',
    '', 0, 300, Code) then RaiseException('Preserve fixture failed');
end;
''' + body, encoding="utf-8")
    compiled = subprocess.run([str(compiler), "/Q", str(source)], capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    log = tmp_path / "setup.log"
    run = subprocess.run(
        [str(tmp_path / "edge.exe"), "/SILENT" if visible else "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}", f"/DIR={tmp_path / 'install'}"],
        capture_output=True, timeout=30,
    )
    return run, log.read_text(encoding="utf-8-sig")


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows and Inno Setup 6")
def test_preparation_failure_after_preservation_restores_error_page_and_exit(tmp_path, monkeypatch):
    run, text = _run_edge_installer(tmp_path, monkeypatch, r'''
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Cancel, Confirm: Boolean;
begin
  PreserveFixture;
  Result := 'Simulated administrator migration failure';
  AbortCliProgress;
  Cancel := True;
  Confirm := False;
  CliCancelButtonClick(WizardForm.CurPageID, Cancel, Confirm);
  Log('[failure] visible=' + IntToStr(Ord(TestVisible(CliProgressContainer.Handle))) +
    '; stage=' + IntToStr(CliStage) + '; cancel=' + IntToStr(Ord(Cancel)));
end;
''')
    assert run.returncode != 0, text
    assert "[failure] visible=0; stage=0; cancel=1" in text
    # Both real production entry points must clear a later migration failure,
    # not only failures inside the preserve subprocess.
    for name, message in (("installer-electron.iss", "The previous version was not uninstalled."),
                          ("installer.iss", "Legacy administrator installation migration failed.")):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        failure = source[source.index(message):].split("end;", 1)[0]
        assert "AbortCliProgress;" in failure


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows and Inno Setup 6")
@pytest.mark.parametrize("cancel_download", [False, True])
def test_prerequisite_download_keeps_common_gauge_and_cancel_button_visible(tmp_path, monkeypatch, cancel_download):
    payload = b"isolated prerequisite download\n" * 20000

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                for offset in range(0, len(payload), 8192):
                    self.wfile.write(payload[offset:offset + 8192])
                    self.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = r'''
type TTestRect = record Left, Top, Right, Bottom: Longint; end;
function TestRect(Window: LongWord; var Bounds: TTestRect): Boolean;
  external 'GetWindowRect@user32.dll stdcall';
var Page: TDownloadWizardPage; Gauge: LongWord;
function DownloadProgress(const Url, FileName: String; const Progress, Max: Int64): Boolean;
var PanelBounds, AbortBounds: TTestRect;
begin
  UpdateCliPrerequisiteProgress(Progress, Max);
  TestRect(CliProgressContainer.Handle, PanelBounds);
  TestRect(Page.AbortButton.Handle, AbortBounds);
  Log('[prerequisite] visible=' + IntToStr(Ord(TestVisible(CliProgressGauge.Handle))) +
    '; same=' + IntToStr(Ord(CliProgressGauge.Handle = Gauge)) +
    '; abort=' + IntToStr(Ord(TestVisible(Page.AbortButton.Handle))) +
    '; uncovered=' + IntToStr(Ord(AbortBounds.Top >= PanelBounds.Bottom)));
  if @CANCEL@ and (Progress > 0) then
    TestSendMessage(Page.AbortButton.Handle, $00F5, 0, 0);
  Result := True;
end;
procedure InitializeWizard;
begin
  Page := CreateDownloadPage('Downloading prerequisites', 'You can cancel the download.', @DownloadProgress);
end;
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  PreserveFixture;
  Gauge := CliProgressGauge.Handle;
  Page.Add('@URL@', 'fixture.bin', '');
  Page.Show;
  ShowCliPrerequisiteStatus('Downloading prerequisites...', 'You can cancel the download below.');
  try
    try
      Page.Download;
    except
      if not Page.AbortedByUser then RaiseException(GetExceptionMessage);
    end;
    Log('[prerequisite] aborted=' + IntToStr(Ord(Page.AbortedByUser)));
  finally
    Page.Hide;
  end;
  Result := '';
end;
procedure CurStepChanged(Step: TSetupStep);
begin
  if Step = ssPostInstall then begin
    Log('[after-prerequisite] start=' + IntToStr(CliExtractionStart) +
      '; same=' + IntToStr(Ord(CliProgressGauge.Handle = Gauge)));
    FinishCliProgress;
  end;
end;
'''.replace("@URL@", f"http://127.0.0.1:{server.server_port}/fixture.bin").replace("@CANCEL@", str(cancel_download))
    try:
        run, text = _run_edge_installer(tmp_path, monkeypatch, body, visible=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert run.returncode == 0, text
    assert "[prerequisite] visible=1; same=1; abort=1; uncovered=1" in text
    assert not re.search(r"\[prerequisite\].*(?:visible|same|abort|uncovered)=0", text)
    assert f"[prerequisite] aborted={int(cancel_download)}" in text
    assert "[after-prerequisite] start=400; same=1" in text


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows and Inno Setup 6")
@pytest.mark.parametrize("outcome", ["success", "exit-error", "missing-executable"])
def test_installer_wait_tracks_process_exit_and_preserves_total_progress(tmp_path, monkeypatch, outcome):
    compiler = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe"
    if not compiler.is_file():
        pytest.skip("Inno Setup is not installed")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    shutil.copyfile(ROOT / "scripts/windows-cli-preservation.iss", tmp_path / "windows-cli-preservation.iss")
    (tmp_path / "helper.ps1").write_text(
        'param([string]$ProgressPath)\n'
        '[IO.File]::WriteAllText($ProgressPath,"40`nVerifying CLI files...`n4 / 10 files")\n'
        'Start-Sleep -Milliseconds 300\n'
        '[IO.File]::WriteAllText($ProgressPath,"20`nScanning next environment...`nDiscovering files")\n'
        'Start-Sleep -Milliseconds 300\n'
        f'exit {7 if outcome == "exit-error" else 0}\n', encoding="utf-8",
    )
    executable = "{sys}\\WindowsPowerShell\\v1.0\\powershell.exe"
    if outcome == "missing-executable":
        executable = "{tmp}\\missing.exe"
    script = r'''
[Setup]
WizardStyle=modern
AppName=Installer progress regression
AppVersion=0.0.0
DefaultDirName={sd}\unused-progress-test
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
OutputDir=.
OutputBaseFilename=regression
[Files]
Source: "helper.ps1"; Flags: dontcopy
[Code]
#include "windows-cli-preservation.iss"
function PrepareToInstall(var NeedsRestart: Boolean): String;
var Code: Integer; Ok: Boolean;
begin
  ExtractTemporaryFile('helper.ps1');
  ShowWindowsCliStatus('Preserving CLI environment...');
  Ok := RunCliProcess(ExpandConstant('@EXECUTABLE@'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\helper.ps1') + '" -ProgressPath "' +
    ExpandConstant('{tmp}\progress.txt') + '"',
    ExpandConstant('{tmp}\progress.txt'), 0, 300, Code);
  Log('[test] exit=' + IntToStr(Code));
  Log('[test] progress=' + IntToStr(CliTotalProgress));
  if Ok then Result := '' else Result := 'Expected regression helper failure';
end;
procedure CurStepChanged(Step: TSetupStep);
begin
  if Step = ssPostInstall then begin
    FinishCliProgress;
    Log('[test] completed');
  end;
end;
'''.replace("@EXECUTABLE@", executable)
    source = tmp_path / "regression.iss"
    source.write_text(script, encoding="utf-8")
    compile_result = subprocess.run([str(compiler), "/Q", str(source)], capture_output=True, text=True, timeout=60)
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    log = tmp_path / "setup.log"
    result = subprocess.run(
        [str(tmp_path / "regression.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}", f"/DIR={tmp_path / 'install'}"],
        capture_output=True, timeout=30,
    )
    text = log.read_text(encoding="utf-8-sig")
    values = [int(value) for value in re.findall(r"\[CLI\] Overall progress: (\d+)/1000", text)]
    assert values == sorted(values), text
    if outcome != "missing-executable":
        # The helper's 40% must be consumed before its later 20% report. Endpoint
        # checks alone would also pass if the progress reader stopped working.
        assert 120 in values, text
        assert 60 not in values, text
    if outcome == "success":
        assert result.returncode == 0 and "[test] completed" in text, text
        assert "[test] exit=0" in text and "[test] progress=300" in text
        assert values[-1] == 1000
    else:
        assert result.returncode != 0 and "[test] completed" not in text, text
        assert "[test] exit=7" in text if outcome == "exit-error" else "[test] exit=-1" in text
        assert not values or values[-1] < 300


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows and Inno Setup 6")
def test_common_progress_window_survives_native_stage_transitions(tmp_path, monkeypatch):
    compiler = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe"
    if not compiler.is_file():
        pytest.skip("Inno Setup is not installed")
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    # Observe actual native window handles/visibility/position, rather than
    # treating the helper's monotonic counter as evidence of continuous UI.
    helper = (ROOT / "scripts/windows-cli-preservation.iss").read_text(encoding="utf-8")
    helper = helper.replace("while CliPeekMessage(Msg, 0, 0, 0, 1) do", "RecordPresentation;\n  while CliPeekMessage(Msg, 0, 0, 0, 1) do")
    (tmp_path / "windows-cli-preservation.iss").write_text(helper, encoding="utf-8")
    (tmp_path / "helper.ps1").write_text(
        'param([string]$ProgressPath)\n'
        'foreach($value in @(40,20,80,100)) {\n'
        ' [IO.File]::WriteAllText($ProgressPath,([string]$value+"`nVerifying CLI files...`nChecking recovery copies"))\n'
        ' Start-Sleep -Milliseconds 150\n'
        '}\nexit 0\n', encoding="utf-8",
    )
    (tmp_path / "payload.bin").write_bytes(b"isolated extraction fixture\n" * 20000)
    source = tmp_path / "transitions.iss"
    source.write_text(r'''
[Setup]
WizardStyle=modern
AppName=Installer transition regression
AppVersion=0.0.0
DefaultDirName={sd}\unused-transition-test
Uninstallable=no
CreateUninstallRegKey=no
PrivilegesRequired=lowest
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
OutputDir=.
OutputBaseFilename=transitions
[Files]
Source: "helper.ps1"; Flags: dontcopy
Source: "payload.bin"; DestDir: "{app}"
[Code]
procedure RecordPresentation; forward;
#include "windows-cli-preservation.iss"
function TestVisible(Window: LongWord): Boolean;
  external 'IsWindowVisible@user32.dll stdcall';
function TestGetParent(Window: LongWord): LongWord;
  external 'GetParent@user32.dll stdcall';
function TestSendMessage(Window, Msg, WParam, LParam: LongWord): LongWord;
  external 'SendMessageW@user32.dll stdcall';
function TestPostMessage(Window, Msg, WParam, LParam: LongWord): Boolean;
  external 'PostMessageW@user32.dll stdcall';
function TestGetDC(Window: LongWord): LongWord;
  external 'GetDC@user32.dll stdcall';
function TestReleaseDC(Window, DC: LongWord): Integer;
  external 'ReleaseDC@user32.dll stdcall';
function TestGetPixel(DC: LongWord; X, Y: Integer): LongWord;
  external 'GetPixel@gdi32.dll stdcall';
var
  CloseSent: Boolean;
procedure RecordPresentation;
var DC, PageDC, PageHandle, LabelDC: LongWord;
begin
  if (CliStage = 0) or (WizardForm.CurPageID = wpFinished) then Exit;
  Log('[presentation] ' + IntToStr(CliStage) + ',' +
    IntToStr(CliProgressGauge.Handle) + ',' +
    IntToStr(TestGetParent(CliProgressGauge.Handle)) + ',' +
    IntToStr(TestGetParent(CliProgressContainer.Handle)) + ',' +
    IntToStr(WizardForm.Handle) + ',' +
    IntToStr(Ord(TestVisible(CliProgressGauge.Handle))) + ',' +
    IntToStr(TestSendMessage(CliProgressGauge.Handle, $0408, 0, 0)) + ',' +
    IntToStr(CliProgressGauge.Left) + ',' + IntToStr(CliProgressGauge.Top) + ',' +
    IntToStr(CliProgressContainer.Left) + ',' + IntToStr(CliProgressContainer.Top) + ',' +
    IntToStr(TestSendMessage(CliProgressGauge.Handle, $0407, 0, 0)));
  CliProgressContainer.Update;
  DC := TestGetDC(CliProgressContainer.Handle);
  if WizardForm.CurPageID = wpPreparing then
    PageHandle := WizardForm.PreparingPage.Handle
  else PageHandle := WizardForm.InstallingPage.Handle;
  PageDC := TestGetDC(PageHandle);
  LabelDC := TestGetDC(CliStatusLabel.Handle);
  try
    Log('[background] ' + IntToStr(TestGetPixel(DC,
      CliProgressGauge.Left + 20, CliProgressGauge.Top - 3)) + ',' +
      IntToStr(TestGetPixel(PageDC, CliProgressGauge.Left + 20,
        CliProgressGauge.Top + CliProgressGauge.Height + 8)));
    Log('[background] ' + IntToStr(TestGetPixel(LabelDC,
      CliStatusLabel.Width - 3, 2)) + ',' +
      IntToStr(TestGetPixel(PageDC, CliProgressGauge.Left + 20,
        CliProgressGauge.Top + CliProgressGauge.Height + 8)));
  finally
    TestReleaseDC(CliProgressContainer.Handle, DC);
    TestReleaseDC(PageHandle, PageDC);
    TestReleaseDC(CliStatusLabel.Handle, LabelDC);
  end;
  Log('[status] ' + CliStatusLabel.Caption);
  if CliHelperRunning and not CloseSent then
  begin
    CloseSent := True;
    TestPostMessage(WizardForm.Handle, $0010, 0, 0);
  end;
end;
procedure HoldTransition(const Name: String);
var I: Integer;
begin
  Log('[transition] ' + Name);
  for I := 1 to 10 do begin
    PumpCliMessages;
    Sleep(20);
  end;
end;
procedure RunPhase(const StartValue, EndValue: Integer);
var Code: Integer;
begin
  if not RunCliProcess(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{tmp}\helper.ps1') + '" -ProgressPath "' +
    ExpandConstant('{tmp}\progress.txt') + '"',
    ExpandConstant('{tmp}\progress.txt'), StartValue, EndValue, Code) then
    RaiseException('Regression helper failed');
end;
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  ExtractTemporaryFile('helper.ps1');
  ShowWindowsCliStatus('Preserving CLI environment...');
  RunPhase(0, 300);
  HoldTransition('preserve-to-extraction');
  Result := '';
end;
<event('CurInstallProgressChanged')>
procedure TestExtract(CurProgress, MaxProgress: Integer);
begin
  RecordPresentation;
end;
procedure CurStepChanged(Step: TSetupStep);
var Code: Integer;
begin
  if Step = ssInstall then HoldTransition('native-install-start');
  if Step = ssPostInstall then begin
    HoldTransition('extraction-to-initialize');
    ShowWindowsCliStatus('Initializing CLI tools...');
    DeleteFile(ExpandConstant('{tmp}\progress.txt'));
    RunPhase(850, 980);
    HoldTransition('initialize-to-restore');
    ShowWindowsCliStatus('Restoring upgrade settings. Please wait...');
    if not RunCliProcess(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoProfile -NonInteractive -Command "Start-Sleep -Milliseconds 200"',
      '', 980, 990, Code) then RaiseException('Restore regression helper failed');
    FinishCliProgress;
    HoldTransition('restore-to-finish');
  end;
end;
''', encoding="utf-8")
    compiled = subprocess.run([str(compiler), "/Q", str(source)], capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    log = tmp_path / "setup.log"
    run = subprocess.run(
        [str(tmp_path / "transitions.exe"), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}", f"/DIR={tmp_path / 'install'}"],
        capture_output=True, timeout=30,
    )
    text = log.read_text(encoding="utf-8-sig")
    assert run.returncode == 0, text
    samples = [tuple(map(int, row.split(","))) for row in re.findall(r"\[presentation\] ([\d,]+)", text)]
    assert {row[0] for row in samples} == {1, 2, 3, 4}, text
    assert len({row[1] for row in samples}) == 1, "Native gauge was recreated"
    assert len({row[2] for row in samples}) == 1, "Native gauge was reparented"
    assert all(row[3] == row[4] for row in samples), "Container must stay on the common wizard"
    assert all(row[5] == 1 for row in samples), "Native gauge disappeared before completion"
    positions = [row[6] for row in samples]
    # TNewProgressBar maps its logical range to the native control's range.
    assert positions == sorted(positions) and positions[-1] == samples[-1][11], text
    assert samples[-1][11] > 0
    assert len({row[7:11] for row in samples}) == 1, "Gauge bounds moved across stages"
    backgrounds = re.findall(r"\[background\] (\d+),(\d+)", text)
    assert backgrounds and all(actual == expected for actual, expected in backgrounds), backgrounds
    statuses = re.findall(r"\[status\] (.*)", text)
    assert statuses and all(status and status.isascii() for status in statuses)
    for transition in ("preserve-to-extraction", "extraction-to-initialize", "initialize-to-restore", "restore-to-finish"):
        assert f"[transition] {transition}" in text
    assert (tmp_path / "install/payload.bin").read_bytes() == (tmp_path / "payload.bin").read_bytes()
