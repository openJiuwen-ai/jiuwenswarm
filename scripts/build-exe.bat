@echo off
setlocal
set "PROCESSOR_ARCHITECTURE=AMD64"
REM Windows build-exe script
REM Usage: scripts\build-exe.bat  or double-click to run

REM Project root = parent of this bat's own dir. Path-relative, survives relocation.
if not defined WORKSWARM_PACKAGING_ROOT for %%I in ("%~dp0..") do set "WORKSWARM_PACKAGING_ROOT=%%~fI"
if not defined WORKSWARM_SOURCE_ROOT set "WORKSWARM_SOURCE_ROOT=%WORKSWARM_PACKAGING_ROOT%"
if not defined WORKSWARM_BUILD_ROOT set "WORKSWARM_BUILD_ROOT=%WORKSWARM_SOURCE_ROOT%"
if not exist "%WORKSWARM_BUILD_ROOT%" mkdir "%WORKSWARM_BUILD_ROOT%"
cd /d "%WORKSWARM_SOURCE_ROOT%"

echo === Windows build-exe ===
echo.

set "BUILD_DISPLAY_NAME="
for /f "usebackq delims=" %%L in (`uv run --no-project --python 3.11 python "%WORKSWARM_PACKAGING_ROOT%\scripts\build_config.py" --root "%WORKSWARM_SOURCE_ROOT%" --sync --emit-batch`) do %%L
if not defined BUILD_DISPLAY_NAME exit /b 1
echo Build identity: %BUILD_DISPLAY_NAME% %BUILD_VERSION%
echo.

echo [1/3] Installing Python deps (uv sync --extra dev --extra cua)...
call uv sync --extra dev --extra cua
if errorlevel 1 exit /b 1

echo.
echo [2/3] Building frontend...
pushd jiuwenswarm\channels\web\frontend
if errorlevel 1 goto :failed_frontend
if not exist node_modules (
    echo [build] node_modules missing, running npm install...
    call npm install
    if errorlevel 1 goto :failed_frontend
) else (
    echo [build] node_modules exists, skip npm install
)
call npm run build
if errorlevel 1 goto :failed_frontend
popd

echo.
echo [3/3] Running PyInstaller...
call uv run pyinstaller "%WORKSWARM_PACKAGING_ROOT%\scripts\jiuwenswarm.spec" --noconfirm --distpath "%WORKSWARM_BUILD_ROOT%\dist" --workpath "%WORKSWARM_BUILD_ROOT%\build"
if errorlevel 1 exit /b 1

echo.
echo Verifying frozen A2UI v0.8 bundle...
start "" /wait "%WORKSWARM_BUILD_ROOT%\dist\%BUILD_DIST_DIR_NAME%\%BUILD_EXECUTABLE_NAME_WINDOWS%" "%WORKSWARM_PACKAGING_ROOT%\scripts\verify_a2ui_bundle.py"
if errorlevel 1 exit /b 1

echo.
echo Verifying frozen RSI Harness baseline bundle...
start "" /wait "%WORKSWARM_BUILD_ROOT%\dist\%BUILD_DIST_DIR_NAME%\%BUILD_EXECUTABLE_NAME_WINDOWS%" "%WORKSWARM_PACKAGING_ROOT%\scripts\verify_rsi_bundle.py"
if errorlevel 1 exit /b 1

echo.
echo Verifying frozen Computer Use runtime without build tools...
start "" /wait "%WORKSWARM_BUILD_ROOT%\dist\%BUILD_DIST_DIR_NAME%\%BUILD_EXECUTABLE_NAME_WINDOWS%" "%WORKSWARM_PACKAGING_ROOT%\scripts\verify_computer_use_bundle.py"
if errorlevel 1 exit /b 1

echo.
echo Verifying frozen GitCode CLI bundle...
start "" /wait "%WORKSWARM_BUILD_ROOT%\dist\%BUILD_DIST_DIR_NAME%\%BUILD_EXECUTABLE_NAME_WINDOWS%" "%WORKSWARM_PACKAGING_ROOT%\scripts\verify_gitcode_cli_bundle.py"
if errorlevel 1 exit /b 1

echo.
echo === Build complete ===
echo Desktop dir: %WORKSWARM_BUILD_ROOT%\dist\%BUILD_DIST_DIR_NAME%
echo Main exe:    %WORKSWARM_BUILD_ROOT%\dist\%BUILD_DIST_DIR_NAME%\%BUILD_EXECUTABLE_NAME_WINDOWS%
pause
exit /b 0

:failed_frontend
popd
exit /b 1
