# Third-party native dependencies

## Microsoft Detours

[Microsoft Detours](https://github.com/microsoft/Detours) v4.0.1 (MIT, Copyright Microsoft Corporation) is linked into `jiuwen_softdelete.dll`. It is **not** copied into `jiuwenbox/`. Clone it locally; `third_party/microsoft-detours/` is gitignored.

```powershell
git clone --branch v4.0.1 --depth 1 https://github.com/microsoft/Detours third_party/microsoft-detours
```

Build steps are in `jiuwenbox/src/jiuwenbox/native/softdelete/BUILD.md`. Use Visual Studio 2022 x64 Native Tools and `build_softdelete.bat`. That script builds `lib.X64\detours.lib` with upstream `nmake`, then links only `softdelete.c` plus that library.

`DetourCopyInstruction` comes from upstream `disasm.cpp` inside `detours.lib`. This repo does not ship a Detours fork or an OpenDesk shim.
