@echo off
REM JiuwenSwarm build-exe script
REM Usage: scripts\build-exe.bat  or double-click to run

REM Project root = parent of this bat's own dir. Path-relative, survives relocation.
cd /d "%~dp0\.."

echo === JiuwenSwarm build-exe ===
echo.

echo [1/3] Installing Python deps (uv sync --extra dev)...
call uv sync --extra dev
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
start "" /wait "%cd%\dist\jiuwenswarm\jiuwenswarm.exe" "%cd%\scripts\verify_a2ui_bundle.py"
if errorlevel 1 exit /b 1

echo.
echo === Build complete ===
echo Desktop dir: %cd%\dist\jiuwenswarm
echo Main exe:    %cd%\dist\jiuwenswarm\jiuwenswarm.exe
pause
exit /b 0

:failed_frontend
popd
exit /b 1
