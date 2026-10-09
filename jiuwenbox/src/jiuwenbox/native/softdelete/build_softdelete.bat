@echo off
setlocal
rem Build jiuwen_softdelete.dll (x64) next to this script's parent directory.
set "HERE=%~dp0"
set "OUT=%HERE%..\jiuwen_softdelete.dll"
where cl >nul 2>&1
if errorlevel 1 (
  echo cl.exe is not on PATH. Open an x64 Native Tools prompt, or run VsDevCmd.bat -arch=x64 first.
  exit /b 1
)
cl /nologo /utf-8 /std:c17 /LD /O2 /W3 /DUNICODE /D_UNICODE /D_WIN32_WINNT=0x0A00 ^
  /Fo"%HERE%softdelete.obj" "%HERE%softdelete.c" ^
  /link /OUT:"%OUT%" shell32.lib advapi32.lib user32.lib kernel32.lib
if errorlevel 1 exit /b 1
del /q "%HERE%softdelete.obj" "%HERE%..\jiuwen_softdelete.exp" "%HERE%..\jiuwen_softdelete.lib" 2>nul
echo built %OUT%
exit /b 0
