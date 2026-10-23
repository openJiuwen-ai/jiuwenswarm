import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from computer_use._json import loads as jsonLoads

from ..paths import native_driver, repository_root

LOGGER = logging.getLogger(__name__)


def run(command, args, cwd=None, capture=False):
    result = subprocess.run(
        [str(command), *map(str, args)], cwd=cwd, text=True, capture_output=capture,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if result.returncode:
        raise RuntimeError(f"{Path(command).name} failed ({result.returncode})." +
                           (f"\n{result.stderr}" if capture else ""))
    return result.stdout.strip() if capture else result


def build_mac_os(root=None, *, configuration="debug"):
    if sys.platform != "darwin":
        raise RuntimeError("Only the macOS backend is implemented.")
    swift = shutil.which("swift")
    if swift is None:
        raise RuntimeError("The macOS desktop driver requires a Swift 6 toolchain.")
    if Path(swift).resolve() == Path("/usr/bin/swift"):
        # The system swift shim may open the developer-tools installation dialog.
        selected = subprocess.run(
            ["/usr/bin/xcode-select", "--print-path"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        if selected.returncode:
            raise RuntimeError("No active macOS developer tools; native desktop preparation skipped.")
    from .pointer import generate_pointer_assets
    root = Path(root) if root else repository_root()
    generate_pointer_assets(root=root)
    native = root / "backends/macos"
    run("swift", ["build", "-c", configuration], native)
    bin_path = Path(run("swift", ["build", "-c", configuration, "--show-bin-path"], native, True))
    bundle = native / ".build/ComputerUseDriver.app"
    executable = bundle / "Contents/MacOS/computer-use-driver"
    executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bin_path / "computer-use-driver", executable)
    (bundle / "Contents/Info.plist").write_text(
        '''<?xml version="1.0" encoding="UTF-8"?>
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
    LOGGER.info("%s", executable)
    return executable


def build_native(root=None, *, configuration="debug"):
    if sys.platform == "darwin":
        return build_mac_os(root, configuration=configuration)
    if sys.platform == "win32":
        return build_windows(root)
    raise RuntimeError(f"platform {sys.platform} is not implemented")


def build_windows(root=None):
    """Use the system Framework compiler; no Node, NuGet or shell command."""
    if sys.platform != "win32":
        raise RuntimeError("Build the Windows backend on Windows x64 with .NET Framework 4.8.")
    native = (Path(root) if root else repository_root()) / "backends/windows"
    framework = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319"
    compiler = framework / "csc.exe"
    if not compiler.is_file():
        raise RuntimeError("Windows x64 with .NET Framework 4.8 and its C# compiler is required.")
    output = native / ".build/computer-use-driver.exe"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Derive COM declarations from the host Windows type library. Keep system
    # property identifiers/enums, without using managed provider traversal.
    importer = output.with_name("import-uia.exe")
    interop = output.with_name("Interop.NativeUIA.dll")
    run(compiler, [
        "/nologo", "/target:exe", "/platform:x64", f"/out:{importer}", native / "Build/ImportUia.cs"
    ], native)
    run(importer, [output.parent], native)
    references = ("System.dll", "System.Core.dll", "System.Web.Extensions.dll", "System.Drawing.dll",
                  "System.Windows.Forms.dll", "WPF/WindowsBase.dll", "WPF/UIAutomationClient.dll",
                  "WPF/UIAutomationTypes.dll")
    sources = sorted(native.glob("*.cs"))
    if not sources:
        raise RuntimeError("Windows backend sources are missing; reinstall the toolkit.")
    run(compiler, [
        "/nologo", "/target:exe", "/platform:x64", "/optimize+", "/warnaserror+", f"/out:{output}",
        *(f"/reference:{framework / name}" for name in references), f"/reference:{interop}", *sources
    ], native)
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
    run(compiler, [
        '/nologo', '/target:exe', '/platform:x64', '/optimize+', '/warnaserror+', f'/out:{capture}',
        *(f'/reference:{path}' for path in required), native / 'Wire.cs', native / 'NativeCaptureGeometry.cs', *sorted(
            (native / 'CaptureWorker').glob('*.cs'))
    ], native)
    LOGGER.info("%s", output)
    return output


def read_permissions(request=False, root=None, timeout=None):
    native = native_driver(root=root)
    try:
        result = subprocess.run(
            [native["command"], *native["args"], "--request-permissions" if request else "--check-permissions"],
            stdout=subprocess.PIPE, text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError("本机后端尚未构建，请先运行 jiuwenswarm-computer-use --prepare 或 computer-use-build。") from None
    if result.returncode:
        raise RuntimeError(f"权限检查未完成（{result.returncode}）。")
    status = jsonLoads(result.stdout)
    if not isinstance(status, dict):
        raise ValueError("Invalid permission response")
    return status


def permission_state(status, platform=None):
    platform = platform or sys.platform
    missing = [name for name in ("accessibility", "screenCapture", "input") if status.get(name) is not True]
    return not missing, missing


def permissions(argv=None):
    parser = argparse.ArgumentParser(description="检查目标桌面权限；--request 在 macOS 发起系统授权提示。请使用与 MCP 相同的终端或 SSH 会话。")
    parser.add_argument("--request", action="store_true")
    args = parser.parse_args(argv)
    status = read_permissions(args.request)
    # This is structured command output; diagnostics use the logger on stderr.
    json.dump(status, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.flush()
    ready, missing = permission_state(status)
    if not ready:
        message = (
            "已请求缺失权限；请在系统提示或「系统设置 → 隐私与安全」中授权，必要时重启启动终端，再运行 computer-use-permissions。"
            if args.request
            else "可运行 computer-use-permissions --request 发起系统授权提示。"
        )
        LOGGER.error(
            "未就绪：%s。%s\n权限归属以系统显示的启动程序为准（驱动、终端或 SSH 服务，例如 "
            "sshd-keygen-wrapper）；请从与 MCP 相同的终端或 SSH 连接复查。请求命令不会代替你批准授权。",
            ", ".join(missing), message,
        )
        return 1
    return 0


def _cli(action):
    try:
        return action()
    except Exception as error:
        LOGGER.error("%s", error)
        # Console entry point; preserve the failure exit code for callers.
        # pylint: disable-next=avoid-using-exit
        raise SystemExit(1) from None


def build_cli(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description='Install/build native driver dependencies; this does not verify desktop access.')
    parser.parse_args(argv)
    _cli(build_native)


def permissions_cli():
    # Console entry point; preserve the failure exit code for callers.
    # pylint: disable-next=avoid-using-exit
    raise SystemExit(_cli(permissions))
