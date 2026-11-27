@echo off
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYTHONPATH=%ROOT%"
set "PYTHONIOENCODING=utf-8"
cd /d "%ROOT%"
"%ROOT%\.venv\Scripts\python.exe" -m jiuwenclaw.channel.acp_channel %* 2>NUL
