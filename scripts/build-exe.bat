@echo off
REM Windows build-exe script
REM Usage: scripts\build-exe.bat  or double-click to run

REM Project root = parent of this bat's own dir. Path-relative, survives relocation.
cd /d "%~dp0\.."

echo === Windows build-exe ===
echo.

set "BUILD_DISPLAY_NAME="
for /f "usebackq delims=" %%L in (`uv run --no-project --python 3.11 python scripts\build_config.py --sync --emit-batch`) do %%L
if not defined BUILD_DISPLAY_NAME exit /b 1
echo Build identity: %BUILD_DISPLAY_NAME% %BUILD_VERSION%
echo.

echo [1/3] Installing Python deps (uv sync --extra dev --extra claude --extra codex)...
call uv sync --extra dev --extra claude --extra codex
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
call uv run pyinstaller scripts\jiuwenswarm.spec --noconfirm
if errorlevel 1 exit /b 1

echo.
echo Verifying frozen A2UI v0.8 bundle...
start "" /wait "%cd%\dist\%BUILD_DIST_DIR_NAME%\%BUILD_EXECUTABLE_NAME_WINDOWS%" "%cd%\scripts\verify_a2ui_bundle.py"
if errorlevel 1 exit /b 1

echo.
echo === Build complete ===
echo Desktop dir: %cd%\dist\%BUILD_DIST_DIR_NAME%
echo Main exe:    %cd%\dist\%BUILD_DIST_DIR_NAME%\%BUILD_EXECUTABLE_NAME_WINDOWS%
pause
exit /b 0

:failed_frontend
popd
exit /b 1
