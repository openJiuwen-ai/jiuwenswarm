@echo off
setlocal EnableExtensions
rem Build jiuwen_softdelete.dll (x64) next to ..\jiuwen_softdelete.dll
set "HERE=%~dp0"
set "OUT=%HERE%..\jiuwen_softdelete.dll"

where cl >nul 2>&1
if errorlevel 1 (
  echo cl.exe is not on PATH. Open "x64 Native Tools Command Prompt for VS", or run vcvars64.bat first.
  exit /b 1
)
where nmake >nul 2>&1
if errorlevel 1 (
  echo nmake.exe is not on PATH. Install Visual Studio C++ build tools with MSBuild/nmake.
  exit /b 1
)

if not defined DETOURS_ROOT set "DETOURS_ROOT=%HERE%..\..\..\..\..\third_party\microsoft-detours"
if not exist "%DETOURS_ROOT%\src\detours.h" (
  echo Detours not found at: %DETOURS_ROOT%
  echo Clone v4.0.1, for example:
  echo   git clone --branch v4.0.1 --depth 1 https://github.com/microsoft/Detours "%DETOURS_ROOT%"
  exit /b 1
)

set "DETOURS_LIB=%DETOURS_ROOT%\lib.X64\detours.lib"
if not exist "%DETOURS_LIB%" (
  echo Building Detours lib in %DETOURS_ROOT%\src ...
  pushd "%DETOURS_ROOT%\src"
  set DETOURS_TARGET_PROCESSOR=X64
  nmake clean 2>nul
  nmake
  if errorlevel 1 (
    popd
    echo nmake failed in Detours src/
    exit /b 1
  )
  popd
)
if not exist "%DETOURS_LIB%" (
  echo Missing %DETOURS_LIB% after nmake.
  exit /b 1
)

set "OBJDIR=%HERE%build_msvc"
if not exist "%OBJDIR%" mkdir "%OBJDIR%"

cl /nologo /utf-8 /std:c17 /O2 /W3 /DUNICODE /D_UNICODE /D_WIN32_WINNT=0x0A00 ^
  /I"%DETOURS_ROOT%\src" /Fo"%OBJDIR%\softdelete.obj" /c "%HERE%softdelete.c"
if errorlevel 1 exit /b 1

link /nologo /DLL /OUT:"%OUT%" /DEF:"%HERE%softdelete.def" ^
  "%OBJDIR%\softdelete.obj" "%DETOURS_LIB%" ^
  shell32.lib advapi32.lib user32.lib kernel32.lib
if errorlevel 1 exit /b 1

del /q "%OBJDIR%\softdelete.obj" 2>nul
del /q "%HERE%..\jiuwen_softdelete.exp" "%HERE%..\jiuwen_softdelete.lib" 2>nul
echo built %OUT%
exit /b 0
