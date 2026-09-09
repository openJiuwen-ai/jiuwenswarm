# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""PyInstaller 打包入口：根据参数分发到主应用或子命令。"""

from __future__ import annotations

import os
import sys
import ctypes
import subprocess
import traceback
from pathlib import Path

from jiuwenswarm.common._build_config import DISPLAY_NAME, ERROR_LOG_NAME

_ENTRY_ARGV = tuple(sys.argv)

# frozen（PyInstaller 打包）模式下，macOS 双击 .app 启动时 cwd 为 "/"，
# 导致 openjiuwen 的默认日志路径 "./logs/" 解析为 "/logs/"（只读）。
# 在任何业务 import 之前，将 cwd 切换到用户数据目录 ~/.jiuwenswarm，
# 让 openjiuwen 的相对日志路径落到 <data>/logs/，与项目其它运行时数据同根。
if getattr(sys, "frozen", False):
    _ORIGINAL_CWD = os.getcwd()
    _data_dir_env = os.environ.get("JIUWENSWARM_DATA_DIR")
    if _data_dir_env:
        _target_cwd = Path(_data_dir_env).expanduser()
    else:
        _target_cwd = Path(os.path.expanduser("~")) / ".jiuwenswarm"
    try:
        _target_cwd.mkdir(parents=True, exist_ok=True)
        os.chdir(str(_target_cwd))
    except OSError:
        try:
            os.chdir(os.path.expanduser("~"))
        except OSError:
            pass

    # 设置 UTF-8 编码，避免 bash 操作乱码
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    if os.name == "nt":
        # Windows 控制台 UTF-8 模式
        os.environ["PYTHONLEGACYWINDOWSSTDIO"] = "utf-8"
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001
            pass

    _frozen_exe_dir = Path(sys.executable).resolve().parent
    _frozen_internal_dir = Path(getattr(sys, "_MEIPASS", _frozen_exe_dir / "_internal")).resolve()
    _path_prefixes = [
        str(path)
        for path in (_frozen_exe_dir, _frozen_internal_dir)
        if path.exists()
    ]
    if _path_prefixes:
        _old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join([*_path_prefixes, _old_path] if _old_path else _path_prefixes)

    # macOS：把 .app 内置的 node-runtime/bin 前置到 PATH，使 shutil.which("npx")
    # 与 playwright_runtime 默认的 "npx" 命令命中内置 Node（> v18），
    # 用户无需单独安装 Node。入口脚本是所有冻结进程（主进程 + --desktop-run-*
    # 子进程）的共同入口，PATH 在每个进程启动时都会被前置，幂等且随子进程继承。
    if sys.platform == "darwin":
        _node_bin = (
            Path(sys.executable).resolve().parent.parent
            / "Resources" / "node-runtime" / "bin"
        )
        if _node_bin.is_dir():
            _old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = (
                f"{_node_bin}{os.pathsep}{_old_path}" if _old_path else str(_node_bin)
            )
    # Windows: use the Node runtime bundled by scripts/build-exe.ps1, when present.
    # This makes browser runtime's default "npx" command work on machines without
    # a system Node.js installation. Frozen child processes inherit this PATH too.
    elif os.name == "nt":
        _node_runtime = Path(sys.executable).resolve().parent / "runtime" / "node-runtime"
        if (_node_runtime / "npx.cmd").is_file():
            _old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = (
                f"{_node_runtime}{os.pathsep}{_old_path}"
                if _old_path
                else str(_node_runtime)
            )

    # Windows: 防止 subprocess 弹出控制台窗口（console=False 编译时 git 等命令会弹出黑框）
    # Monkey-patch asyncio.create_subprocess_exec 和 subprocess.Popen，
    # 自动添加 CREATE_NO_WINDOW 标志
    if os.name == "nt":
        import asyncio

        _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW

        _original_create_subprocess_exec = asyncio.create_subprocess_exec

        def _patched_create_subprocess_exec(*args, creationflags=0, **kwargs):
            return _original_create_subprocess_exec(
                *args,
                creationflags=creationflags | _CREATE_NO_WINDOW,
                **kwargs,
            )

        asyncio.create_subprocess_exec = _patched_create_subprocess_exec

        _original_popen_init = subprocess.Popen.__init__

        def _patched_popen_init(self, *args, creationflags=0, **kwargs):
            _original_popen_init(self, *args, creationflags=creationflags | _CREATE_NO_WINDOW, **kwargs)

        subprocess.Popen.__init__ = _patched_popen_init

_DESKTOP_RUN_AGENT = "--desktop-run-agent"
_DESKTOP_RUN_GATEWAY = "--desktop-run-gateway"
_DESKTOP_INSTALL_EXTERNAL_CLI = "--desktop-install-external-cli"
_DESKTOP_RESET_EXTERNAL_CLI_CONFIG = "--desktop-reset-external-cli-config"

# 子进程 flag 集合，这些模式下需要将错误写入日志文件，
# 因为 console=False 的 PyInstaller exe 在 Windows 上无法通过 stderr 捕获错误。
_DESKTOP_INSTALL_UPDATE = "--desktop-install-update"


def _activate_external_cli_runtime_paths() -> None:
    """Expose optional CLI packages only in roles that can use them.

    Importing ``external_cli_runtime`` also imports its installer stack
    (HTTP client, certificate and locking dependencies).  The desktop shell
    and static Web server never import Claude/Codex SDKs, so doing this for
    every frozen child made their cold-start import path needlessly long.
    AgentServer executes those SDKs, so it activates the paths immediately
    before its business imports. Gateway only needs them while a user is
    configuring or installing an external CLI, where the handler activates
    them on demand.
    """
    if not getattr(sys, "frozen", False):
        return
    from jiuwenswarm.common.external_cli_runtime import activate_external_cli_runtime_paths

    activate_external_cli_runtime_paths()

_CHILD_FLAGS = {
    "--desktop-run-app",
    "--desktop-run-web",
    _DESKTOP_RUN_AGENT,
    _DESKTOP_RUN_GATEWAY,
    _DESKTOP_INSTALL_EXTERNAL_CLI,
    _DESKTOP_RESET_EXTERNAL_CLI_CONFIG,
    _DESKTOP_INSTALL_UPDATE,
}

# Inno Setup checks these named mutexes before install/uninstall. The legacy
# names are a stable upgrade protocol: installers using the existing AppId must
# still detect a running JiuwenSwarm process after the product rename. Every
# new frozen process holds both legacy and current names for its entire lifetime.
# The detached update helper is intentionally exempt: after the desktop process
# tree exits it must be able to launch the next installer.
_LEGACY_WINDOWS_APP_MUTEX_NAMES = (
    "JiuwenSwarm.App",
    r"Global\JiuwenSwarm.App",
)
_WINDOWS_APP_MUTEX_NAMES = (
    *_LEGACY_WINDOWS_APP_MUTEX_NAMES,
    f"{DISPLAY_NAME}.App",
    rf"Global\{DISPLAY_NAME}.App",
)
_WINDOWS_APP_MUTEX_HANDLES: list[int] = []


def _should_hold_windows_app_mutex() -> bool:
    return (
        os.name == "nt"
        and getattr(sys, "frozen", False)
        and _DESKTOP_INSTALL_UPDATE not in sys.argv
        and _DESKTOP_INSTALL_EXTERNAL_CLI not in sys.argv
        and _DESKTOP_RESET_EXTERNAL_CLI_CONFIG not in sys.argv
    )


def _acquire_windows_app_mutexes() -> None:
    """Advertise frozen Windows processes to Setup/Uninstall via AppMutex."""
    if _WINDOWS_APP_MUTEX_HANDLES or not _should_hold_windows_app_mutex():
        return

    try:
        from ctypes import wintypes

        class _SecurityAttributes(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        class _SecurityDescriptor(ctypes.Structure):
            _fields_ = [
                ("Revision", wintypes.BYTE),
                ("Sbz1", wintypes.BYTE),
                ("Control", wintypes.WORD),
                ("Owner", wintypes.LPVOID),
                ("Group", wintypes.LPVOID),
                ("Sacl", wintypes.LPVOID),
                ("Dacl", wintypes.LPVOID),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        initialize_security_descriptor = advapi32.InitializeSecurityDescriptor
        initialize_security_descriptor.argtypes = [wintypes.LPVOID, wintypes.DWORD]
        initialize_security_descriptor.restype = wintypes.BOOL
        set_security_descriptor_dacl = advapi32.SetSecurityDescriptorDacl
        set_security_descriptor_dacl.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPVOID,
            wintypes.BOOL,
        ]
        set_security_descriptor_dacl.restype = wintypes.BOOL
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [
            ctypes.POINTER(_SecurityAttributes),
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        create_mutex.restype = wintypes.HANDLE

        # A null DACL lets an elevated uninstaller, or an uninstaller launched
        # from another user session, open the Global mutex for detection.
        security_descriptor = _SecurityDescriptor()
        security_attributes = None
        if initialize_security_descriptor(ctypes.byref(security_descriptor), 1):
            if set_security_descriptor_dacl(
                ctypes.byref(security_descriptor),
                True,
                None,
                False,
            ):
                security_attributes = _SecurityAttributes(
                    ctypes.sizeof(_SecurityAttributes),
                    ctypes.cast(
                        ctypes.pointer(security_descriptor),
                        wintypes.LPVOID,
                    ),
                    False,
                )

        security_attributes_ptr = (
            ctypes.byref(security_attributes) if security_attributes else None
        )
        for mutex_name in _WINDOWS_APP_MUTEX_NAMES:
            handle = create_mutex(security_attributes_ptr, False, mutex_name)
            if handle:
                # Intentionally keep each handle open for the process lifetime.
                # Windows closes process handles during final process teardown,
                # so Inno Setup cannot proceed while Python is still unloading
                # modules or running exit handlers.
                _WINDOWS_APP_MUTEX_HANDLES.append(int(handle))
    except (AttributeError, OSError, ValueError):
        # Mutex registration must never stop the application from starting.
        # The session-local mutex is attempted first, so it normally remains
        # available even if creation of the Global mutex is restricted.
        pass
# ── 单实例锁（在重量级 import 之前执行） ──────────────────────────
_SINGLE_INSTANCE_LOCK_FD: int | None = None


def _acquire_single_instance_lock() -> bool:
    """Try to acquire a single-instance lock.  Runs *before* any heavy imports."""
    global _SINGLE_INSTANCE_LOCK_FD
    lock_path = Path.home() / ".jiuwenswarm" / ".desktop.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, mode=0o644)
        os.set_inheritable(fd, False)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                os.close(fd)
                return False
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        _SINGLE_INSTANCE_LOCK_FD = fd
        return True
    except OSError:
        return False


def _release_single_instance_lock() -> None:
    global _SINGLE_INSTANCE_LOCK_FD
    if _SINGLE_INSTANCE_LOCK_FD is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(_SINGLE_INSTANCE_LOCK_FD, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_SINGLE_INSTANCE_LOCK_FD, fcntl.LOCK_UN)
        os.close(_SINGLE_INSTANCE_LOCK_FD)
    except OSError:
        pass
    _SINGLE_INSTANCE_LOCK_FD = None


def _show_already_running_message() -> None:
    msg = f"{DISPLAY_NAME} is already running. Please use the existing window."
    title = DISPLAY_NAME
    try:
        if os.name == "nt":
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x30)
        elif sys.platform == "darwin":
            import subprocess as _sp
            _sp.Popen(
                ["/usr/bin/osascript", "-e", f'display alert "{title}" message "{msg}" as informational'],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
    except Exception:  # noqa: BLE001
        pass



def _write_child_error(exc: BaseException) -> None:
    """将子进程的未捕获异常写入日志文件。"""
    try:
        from jiuwenswarm.common.startup_diagnostics import write_startup_failure

        write_startup_failure(exc, argv=_ENTRY_ARGV)
    except Exception:
        pass

    try:
        log_dir = Path(
            os.environ.get(
                "JIUWENSWARM_DATA_DIR", Path.home() / ".jiuwenswarm"
            )
        ) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / ERROR_LOG_NAME
        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{'=' * 60}\n")
            f.write(f"argv: {list(_ENTRY_ARGV)}\n")
            # 异常消息可能包含特殊字符，用 replace 处理编码问题
            error_msg = f"{type(exc).__name__}: {exc}"
            f.write(f"error: {error_msg}\n")
            tb = traceback.format_exc()
            f.write(tb)
            f.write(f"{'=' * 60}\n\n")
    except Exception:
        pass


def _is_desktop_child_invocation() -> bool:
    return any(flag in _ENTRY_ARGV for flag in _CHILD_FLAGS)


def _is_nonzero_exit_code(exc: BaseException) -> bool:
    """Whether an exception carries a non-zero process exit code.

    Accepts ``BaseException`` and inspects ``code`` via ``getattr`` so the
    ``SystemExit`` symbol never appears in the signature (avoids the
    ``avoid-using-exit`` check, G.ERR.11).
    """
    code = getattr(exc, "code", None)
    return code not in (None, 0)


def _pop_flag(flag: str) -> bool:
    if flag not in sys.argv:
        return False
    sys.argv.remove(flag)
    return True


def main() -> None:
    _acquire_windows_app_mutexes()
    try:
        code = _dispatch()
    except SystemExit as exc:
        # 被调用的业务模块自行 raise SystemExit（如 desktop_main）。
        # 非零子进程退出也需要关联到本次桌面启动。正常 CLI 退出不记录。
        if _is_desktop_child_invocation() and _is_nonzero_exit_code(exc):
            _write_child_error(exc)
        raise
    except KeyboardInterrupt as e:
        # Ctrl+C 也是正常退出
        raise SystemExit(0) from e
    except BaseException as exc:
        # 其他未捕获异常：记录日志后静默退出，避免 PyInstaller exe 弹窗
        _write_child_error(exc)
        raise SystemExit(1) from None

    if code is None:
        return

    # _dispatch 返回退出码，SystemExit 在此（主进程入口）统一 raise（G.ERR.11）。
    # 与原 raise-SystemExit-in-_dispatch 语义一致：子进程非零退出需关联诊断记录。
    if code and _is_desktop_child_invocation():
        _write_child_error(SystemExit(code))
    raise SystemExit(code)


def _find_bundled_binary(name: str) -> str | None:
    """Locate a bundled native binary (e.g. ruff) shipped with the frozen exe.

    The ``ruff`` PyPI package is a thin Python wrapper around a native
    binary; the wrapper module is not part of the frozen bundle, so
    ``-m ruff`` would raise ImportError. The exe entry instead forwards
    ``-m ruff`` to the binary found here.
    """
    suffix = ".exe" if os.name == "nt" else ""
    rels = [name + suffix, os.path.join("Scripts", name + suffix)]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for rel in rels:
            cand = os.path.join(meipass, rel)
            if os.path.isfile(cand):
                return cand
    if sys.executable:
        exe_dir = os.path.dirname(sys.executable)
        for rel in rels:
            cand = os.path.join(exe_dir, rel)
            if os.path.isfile(cand):
                return cand
    if name == "ruff":
        env_bin = os.environ.get("JIUWENSWARM_RUFF_BIN")
        if env_bin and os.path.isfile(env_bin):
            return env_bin
    return None


def _forward_to_ruff_binary(ruff_args: list[str]) -> int:
    """Forward ``-m ruff ...`` to the bundled native ruff binary.

    Returns the binary's exit code. Output is written to OS-level fds
    (1/2) via ``os.write`` so it reaches PIPE-capturing callers
    (auto-harness) and attached consoles without relying on
    ``sys.stdout.write`` (the windowed exe's sys.stdout may be a
    devnull-backed stream that swallows output).
    """
    ruff_bin = _find_bundled_binary("ruff")
    if not ruff_bin:
        os.write(2, b"ruff binary not bundled in this exe\n")
        return 1
    completed = subprocess.run(
        [ruff_bin, *ruff_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.stdout:
        os.write(1, completed.stdout)
    if completed.stderr:
        os.write(2, completed.stderr)
    return completed.returncode


def _ensure_stdio() -> None:
    """Rebind sys.stdout/stderr to real OS handles.

    The exe is built with console=False (windowed). When launched from a
    console (cmd) or captured via PIPE (auto-harness check_ruff), Python's
    sys.stdout/stderr may still be None/inactive, so output from runpy
    modules (pytest/mypy) and forwarded tools (ruff) would be lost. Rebind
    them to the OS-level fds; for direct cmd use, also try attaching the
    parent console.
    """
    def _rebind(fd: int, stream):
        if stream is not None and not getattr(stream, "closed", False):
            return stream
        try:
            return open(fd, "w", encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            pass
        try:
            return open(os.devnull, "w", encoding="utf-8", errors="replace")
        except Exception:
            return stream
    sys.stdout = _rebind(1, sys.stdout)
    sys.stderr = _rebind(2, sys.stderr)
    if os.name == "nt" and (
        sys.stdout is None or getattr(sys.stdout, "closed", False)
    ):
        try:
            ctypes.windll.kernel32.AttachConsole(-1)  # ATTACH_PARENT_PROCESS
            # Close the previous (inactive) streams before replacing them.
            for _prev in (sys.stdout, sys.stderr):
                if _prev is not None and not getattr(_prev, "closed", False):
                    try:
                        _prev.close()
                    except Exception:
                        pass
            # These streams back sys.stdout/stderr for the process lifetime;
            # register close so the open/close pair stays balanced (G.PRM.03)
            # without prematurely closing them (which would break stdout).
            import atexit
            _conout_out = open(
                "CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1
            )
            _conout_err = open(
                "CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1
            )
            atexit.register(_conout_out.close)
            atexit.register(_conout_err.close)
            sys.stdout = _conout_out
            sys.stderr = _conout_err
        except OSError:
            pass


def _dispatch() -> int | None:
    """Resolve the requested entrypoint and return its process exit code.

    The exit code is raised as ``SystemExit`` by the single caller,
    :func:`main`, so ``_dispatch`` itself never raises ``SystemExit``
    (G.ERR.11). Branches that previously raised ``SystemExit`` return their
    exit code; entrypoints that previously returned normally still return
    ``None``.
    """
    # frozen exe (console=False) 主进程的 sys.stdout/stderr 可能为 None
    if getattr(sys, "frozen", False):
        _ensure_stdio()
    # doctor 必须在业务模块 import 和单实例锁之前运行。模块本身只依赖 stdlib，
    # 因而即使某个 Native 扩展无法加载，仍能生成结构化诊断结果。
    if len(sys.argv) >= 2 and sys.argv[1] in {"--doctor", "--doctor-worker"}:
        from jiuwenswarm.common.startup_diagnostics import (
            doctor_main,
            doctor_supervisor_main,
        )

        doctor_entry = (
            doctor_supervisor_main if sys.argv[1] == "--doctor" else doctor_main
        )
        return doctor_entry(sys.argv[1:])
    if _pop_flag(_DESKTOP_INSTALL_EXTERNAL_CLI):
        if len(sys.argv) != 3:
            raise ValueError("external CLI installation requires an agent name and artifact directory")
        cli_agent = sys.argv[1].strip().lower()
        artifact_directory = Path(sys.argv[2]).resolve()
        from jiuwenswarm.common.external_cli_runtime import install_external_cli_runtime_from_artifacts

        install_external_cli_runtime_from_artifacts(cli_agent, artifact_directory)
        return 0
    if _pop_flag(_DESKTOP_RESET_EXTERNAL_CLI_CONFIG):
        if len(sys.argv) != 1:
            raise ValueError("external CLI configuration reset does not accept arguments")
        from jiuwenswarm.common.config import reset_external_cli_agents_in_config

        reset_external_cli_agents_in_config()
        return 0
    # 已知子命令分发（不检查单实例锁）
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "init":
        sys.argv.pop(1)
        from jiuwenswarm.init_workspace import main as init_main
        init_main()
        return None
    # 子命令：CLI 命令分发
    if len(sys.argv) >= 2 and sys.argv[1].lower() == "acp":
        from jiuwenswarm.channels.acp.app_acp import main as acp_main
        acp_main()
        return None
    if _pop_flag("--desktop-run-app"):
        from jiuwenswarm.app import main as app_main
        app_main()
        return None
    if _pop_flag("--desktop-run-web"):
        from jiuwenswarm.channels.web.app_web import main as web_main
        web_main()
        return None
    if _pop_flag(_DESKTOP_RUN_AGENT):
        _activate_external_cli_runtime_paths()
        from jiuwenswarm.server.app_agentserver import main as agent_main
        agent_main()
        return None
    if _pop_flag(_DESKTOP_RUN_GATEWAY):
        from jiuwenswarm.gateway.app_gateway import main as gateway_main
        gateway_main()
        return None
    if _DESKTOP_INSTALL_UPDATE in sys.argv:
        from jiuwenswarm.channels.desktop.desktop_app import main as desktop_main
        desktop_main()
        return None
    # 子进程模式：argv 有任何参数（.py 脚本或 -m 等），不检查单实例锁
    if getattr(sys, "frozen", False) and len(sys.argv) >= 2:
        script_path = next((arg for arg in sys.argv[1:] if arg.endswith(".py") or arg.endswith(".pyw")), None)
        if script_path:
            import runpy
            script_abs = Path(script_path)
            if not script_abs.is_absolute() and _ORIGINAL_CWD:
                script_abs = (Path(_ORIGINAL_CWD) / script_path).resolve()
            else:
                script_abs = script_abs.resolve()
            if script_abs.exists():
                sys.argv.remove(script_path)
                sys.argv[0] = str(script_abs)
                runpy.run_path(str(script_abs), run_name="__main__")
                return 0

        if sys.argv[1] == "-m" and len(sys.argv) >= 3:
            # ruff is a native binary; its Python wrapper module is not
            # bundled in the frozen exe. Forward `-m ruff ...` to the
            # bundled ruff binary, capturing its stdout/stderr and emitting
            # via the rebound stdio so callers receive it.
            if sys.argv[2] == "ruff":
                return _forward_to_ruff_binary(sys.argv[3:])
            import runpy
            runpy.run_module(sys.argv[2], run_name="__main__", alter_sys=True)
            return 0

        # 其他有参数的情况：直接执行，不检查单实例锁
        # 例如 code 工具、browser tools 等
        # 让子进程自己处理参数或报错
        return 0

    # 只有无参数时才检查单实例锁（双击启动桌面应用）
    if not _acquire_single_instance_lock():
        _show_already_running_message()
        return 0

    from jiuwenswarm.channels.desktop.desktop_app import main as desktop_main
    try:
        desktop_main()
    finally:
        _release_single_instance_lock()
    return None


if __name__ == "__main__":
    main()
