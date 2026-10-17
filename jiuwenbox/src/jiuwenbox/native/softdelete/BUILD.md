# Building `jiuwen_softdelete.dll`

The DLL is the only runtime artifact. Microsoft Detours is compiled into it from an external checkout; Detours sources are not part of this repository.

## Prerequisites

1. Visual Studio 2022 **C++ Build Tools** (the full Visual Studio IDE is not required) with the **Windows 10/11 SDK**. `cl.exe` alone is not enough: Detours includes `windows.h`. If they are missing:

```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools -e --accept-package-agreements --accept-source-agreements --override "--quiet --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

Then open **x64 Native Tools Command Prompt for VS 2022**, or run `vcvars64.bat` in the current shell before the build script.

2. [Microsoft Detours](https://github.com/microsoft/Detours) **v4.0.1** (MIT). Clone once; the directory is gitignored:

```powershell
git clone --branch v4.0.1 --depth 1 https://github.com/microsoft/Detours C:\path\to\jiuwenclaw\third_party\microsoft-detours
```

`DETOURS_ROOT` overrides that default path.

## Build

From this directory, in the x64 Native Tools prompt:

```powershell
.\build_softdelete.bat
```

The script runs Detours `nmake` when `lib.X64\detours.lib` is missing, then links `..\jiuwen_softdelete.dll`. Official `detours.lib` already exports `DetourCopyInstruction` and `DetourFinishHelperProcess`. Do not add a separate Detours source tree under `jiuwenbox/`.

Quit any process that has already loaded `jiuwen_softdelete.dll` before rebuilding. Otherwise `link` fails with `LNK1104` because the output file is in use.

Output: `jiuwenbox/src/jiuwenbox/native/jiuwen_softdelete.dll`.
