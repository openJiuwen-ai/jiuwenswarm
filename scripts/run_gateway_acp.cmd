@echo off
setlocal
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "PYTHONPATH=%ROOT%"
set "PYTHONIOENCODING=utf-8"
cd /d "%ROOT%"
"%ROOT%\.venv\Scripts\python.exe" -m jiuwenswarm.gateway.channel_manager.protocol.acp.acp_connect %* 2>NUL
