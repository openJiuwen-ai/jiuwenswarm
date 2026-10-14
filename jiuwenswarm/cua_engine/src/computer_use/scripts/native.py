from computer_use._json import loads as jsonLoads
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from ..paths import nativeDriver, repositoryRoot
from ..protocol.session import nativeProtocolVersion


def run(command, args, cwd=None, capture=False):
    result = subprocess.run([str(command), *map(str, args)], cwd=cwd, text=True, capture_output=capture)
    if result.returncode:
        raise RuntimeError(f"{Path(command).name} failed ({result.returncode})."+(f"\n{result.stderr}" if capture else ""))
    return result.stdout.strip() if capture else result


def buildMacOS(root=None):
    if sys.platform != "darwin": raise RuntimeError("Only the macOS backend is implemented.")
    from .pointer import generatePointerAssets
    root = Path(root) if root else repositoryRoot()
    generatePointerAssets(root=root)
    native = root/"backends/macos"
    run("swift", ["build", "-c", "debug"], native)
    bin = Path(run("swift", ["build", "-c", "debug", "--show-bin-path"], native, True))
    bundle = native/".build/ComputerUseDriver.app"
    executable = bundle/"Contents/MacOS/computer-use-driver"
    executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bin/"computer-use-driver", executable)
    (bundle/"Contents/Info.plist").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.computer-use.driver</string>
<key>CFBundleName</key><string>Computer Use Driver</string>
<key>CFBundleExecutable</key><string>computer-use-driver</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>CFBundleShortVersionString</key><string>0.0.1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
</dict></plist>
''', encoding="utf-8")
    run("codesign", ["--force", "--sign", "-", "--identifier", "local.computer-use.driver", bundle], native)
    print(executable)
    return executable


def _protocolVersion(version):
    if type(version) is not int or version not in (3, 5):
        raise ValueError('Protocol version must be 3 or 5.')
    return version


def buildNative(root=None, *, protocol_version=nativeProtocolVersion):
    _protocolVersion(protocol_version)
    if sys.platform == "darwin":
        if protocol_version != 5: raise ValueError('The macOS backend requires protocol 5; upgrade the client.')
        return buildMacOS(root)
    if sys.platform == "win32":
        if protocol_version != 5: raise ValueError('The Windows backend requires protocol 5; upgrade the client.')
        return buildWindows(root)
    raise RuntimeError(f"platform {sys.platform} is not implemented")


def buildWindows(root=None):
    """Use the system Framework compiler; no Node, NuGet or shell command."""
    if sys.platform != "win32": raise RuntimeError("Build the Windows backend on Windows x64 with .NET Framework 4.8.")
    native = (Path(root) if root else repositoryRoot()) / "backends/windows"
    framework = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319"
    compiler = framework / "csc.exe"
    if not compiler.is_file(): raise RuntimeError("Windows x64 with .NET Framework 4.8 and its C# compiler is required.")
    output = native / ".build/computer-use-driver.exe"
    output.parent.mkdir(parents=True, exist_ok=True)
    references = ("System.dll", "System.Core.dll", "System.Web.Extensions.dll", "System.Drawing.dll", "System.Windows.Forms.dll",
                  "WPF/WindowsBase.dll", "WPF/UIAutomationClient.dll", "WPF/UIAutomationTypes.dll")
    sources = sorted(native.glob("*.cs"))
    if not sources: raise RuntimeError("Windows backend sources are missing; reinstall the toolkit.")
    run(compiler, ["/nologo", "/target:exe", "/platform:x64", "/optimize+", "/warnaserror+", f"/out:{output}",
        *(f"/reference:{framework / name}" for name in references), *sources], native)
    # Compile the WinRT pixel helper against metadata shipped with Windows.
    # UIA objects and the public driver do not depend on WinRT projections.
    metadata = framework.parents[2] / 'System32/WinMetadata'
    capture = output.with_name('computer-use-capture.exe')
    capture_refs = ('System.dll', 'System.Core.dll', 'System.Web.Extensions.dll', 'System.Drawing.dll',
                    'System.Runtime.WindowsRuntime.dll', 'System.Runtime.dll')
    winmd = ('Windows.Foundation.winmd', 'Windows.Graphics.winmd', 'Windows.Storage.winmd')
    required = [*(framework / name for name in capture_refs), *(metadata / name for name in winmd)]
    if any(not path.is_file() for path in required):
        raise RuntimeError('Windows 10 1903+ WinRT metadata and .NET Framework 4.8 are required for window capture.')
    run(compiler, ['/nologo', '/target:exe', '/platform:x64', '/optimize+', '/warnaserror+', f'/out:{capture}',
        *(f'/reference:{path}' for path in required), native / 'Wire.cs', native / 'NativeCaptureGeometry.cs',
        *sorted((native / 'CaptureWorker').glob('*.cs'))], native)
    print(output)
    return output


def readPermissions(request=False, root=None, timeout=None, *, protocol_version=nativeProtocolVersion):
    _protocolVersion(protocol_version)
    if sys.platform in ('darwin', 'win32') and protocol_version != 5:
        platform = {'darwin': 'macOS', 'win32': 'Windows'}[sys.platform]
        raise ValueError(f'The {platform} backend requires protocol 5; upgrade the client.')
    native = nativeDriver(root=root)
    try:
        result = subprocess.run([native["command"], *native["args"], "--request-permissions" if request else "--check-permissions"], stdout=subprocess.PIPE, text=True, timeout=timeout)
    except FileNotFoundError: raise RuntimeError("本机后端尚未构建，请先运行 jiuwenswarm-computer-use --prepare 或 computer-use-build。") from None
    if result.returncode: raise RuntimeError(f"权限检查未完成（{result.returncode}）。")
    status = jsonLoads(result.stdout)
    if not isinstance(status, dict): raise ValueError("Invalid permission response")
    return status


def permissionState(status, platform=None):
    platform = platform or sys.platform
    missing = [name for name in ("accessibility", "screenCapture", "input") if status.get(name) is not True]
    return not missing, missing


def permissions(argv=None):
    parser = argparse.ArgumentParser(description="检查目标桌面权限；--request 在 macOS 发起系统授权提示。请使用与 MCP 相同的终端或 SSH 会话。")
    parser.add_argument("--request", action="store_true")
    parser.add_argument('--protocol-version', type=int, choices=(3, 5), default=nativeProtocolVersion,
                        help='Inspect native protocol 5 resources by default; 3 is for legacy diagnostics.')
    args = parser.parse_args(argv)
    status = readPermissions(args.request, protocol_version=args.protocol_version)
    print(json.dumps(status, ensure_ascii=False, separators=(",", ":")))
    ready, missing = permissionState(status)
    if not ready:
        message = "已请求缺失权限；请在系统提示或「系统设置 → 隐私与安全」中授权，必要时重启启动终端，再运行 computer-use-permissions。" if args.request else "可运行 computer-use-permissions --request 发起系统授权提示。"
        print(f"未就绪：{', '.join(missing)}。{message}\n权限归属以系统显示的启动程序为准（驱动、终端或 SSH 服务，例如 sshd-keygen-wrapper）；请从与 MCP 相同的终端或 SSH 连接复查。请求命令不会代替你批准授权。", file=sys.stderr)
        return 1
    return 0


def _cli(action):
    try: return action()
    except Exception as error: print(str(error), file=sys.stderr); raise SystemExit(1) from None


def buildCli(argv=None):
    parser = argparse.ArgumentParser(description='Install/build native driver dependencies; this does not verify desktop access.')
    parser.add_argument('--protocol-version', type=int, choices=(3, 5), default=nativeProtocolVersion,
                        help='Prepare native protocol 5 resources by default; 3 is for legacy diagnostics. This does not change the SDK protocol.')
    args = parser.parse_args(argv)
    _cli(lambda: buildNative(protocol_version=args.protocol_version))
def permissionsCli(): raise SystemExit(_cli(permissions))
