# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""PyInstaller 打包入口：根据参数分发到主应用或子命令。"""

from __future__ import annotations

import os
import sys
import ctypes
import shutil
import subprocess
import traceback
from pathlib import Path

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
_DESKTOP_RUN_JIUWENBOX = "--desktop-run-jiuwenbox"
_DESKTOP_RUN_WIN_SETUP = "--desktop-run-win-setup"

# 子进程 flag 集合，这些模式下需要将错误写入日志文件，
# 因为 console=False 的 PyInstaller exe 在 Windows 上无法通过 stderr 捕获错误。
_DESKTOP_INSTALL_UPDATE = "--desktop-install-update"

_CHILD_FLAGS = {"--desktop-run-app", "--desktop-run-web",
        _DESKTOP_RUN_AGENT, _DESKTOP_RUN_GATEWAY, _DESKTOP_RUN_JIUWENBOX,
        _DESKTOP_RUN_WIN_SETUP, _DESKTOP_INSTALL_UPDATE}

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
    msg = "JiuwenSwarm is already running. Please use the existing window."
    title = "JiuwenSwarm"
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
        log_dir = Path(os.environ.get("JIUWENSWARM_DATA_DIR", Path.home() / ".jiuwenswarm")) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "jiuwenswarm_exe_error.log"
        with open(log_file, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"{'=' * 60}\n")
            f.write(f"argv: {sys.argv}\n")
            # 异常消息可能包含特殊字符，用 replace 处理编码问题
            error_msg = f"{type(exc).__name__}: {exc}"
            f.write(f"error: {error_msg}\n")
            tb = traceback.format_exc()
            f.write(tb)
            f.write(f"{'=' * 60}\n\n")
    except Exception:
        pass


def _pop_flag(flag: str) -> bool:
    if flag not in sys.argv:
        return False
    sys.argv.remove(flag)
    return True


def _unshadow_frozen_jiuwenbox_namespace() -> None:
    """挪走 ``_MEIPASS/jiuwenbox/``, 避免磁盘目录盖住 PYZ 包."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    pkg = Path(meipass) / "jiuwenbox"
    if not pkg.is_dir():
        return
    dest = Path(meipass) / "jiuwenbox_legacy_overlay"
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        pkg.rename(dest)
    except OSError:
        pass


def _run_win_setup() -> int:
    """安装器已提权时调用: 在本进程内跑 win_setup, 不再 ShellExecuteW(runas)."""
    # import 失败时也能确认安装器调到了这里.
    try:
        log_dir = Path(os.environ.get("JIUWENSWARM_DATA_DIR", Path.home() / ".jiuwenswarm")) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "win_setup_invoke.log").write_text(
            f"argv={sys.argv!r}\ncwd={os.getcwd()}\n", encoding="utf-8",
        )
    except OSError:
        pass
    _unshadow_frozen_jiuwenbox_namespace()
    from jiuwenbox.supervisor.win_setup import _main as win_setup_main

    return win_setup_main(sys.argv[1:])


def _run_jiuwenbox_server() -> None:
    import argparse
    import uvicorn

    _unshadow_frozen_jiuwenbox_namespace()
    from jiuwenbox.server.app import app as jiuwenbox_app

    parser = argparse.ArgumentParser(prog="jiuwenswarm --desktop-run-jiuwenbox")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8321)
    args, _unknown = parser.parse_known_args()
    uvicorn.run(
        jiuwenbox_app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


def main() -> None:
    try:
        code = _dispatch()
    except SystemExit:
        # SystemExit 是正常的退出请求（如 sys.exit()），直接传递，不弹窗
        raise
    except KeyboardInterrupt as e:
        # Ctrl+C 也是正常退出
        raise SystemExit(0) from e
    except BaseException as exc:
        # 其他未捕获异常：记录日志后静默退出，避免 PyInstaller exe 弹窗
        _write_child_error(exc)
        raise SystemExit(1) from None
    if code is not None:
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
    # frozen exe (console=False) 主进程的 sys.stdout/stderr 可能为 None
    if getattr(sys, "frozen", False):
        _ensure_stdio()
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
        from jiuwenswarm.server.app_agentserver import main as agent_main
        agent_main()
        return None
    if _pop_flag(_DESKTOP_RUN_GATEWAY):
        from jiuwenswarm.gateway.app_gateway import main as gateway_main
        gateway_main()
        return None
    if _pop_flag(_DESKTOP_RUN_JIUWENBOX):
        _run_jiuwenbox_server()
        return None
    if _pop_flag(_DESKTOP_RUN_WIN_SETUP):
        return _run_win_setup()
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
                raise SystemExit(0)

        if sys.argv[1] == "-m" and len(sys.argv) >= 3:
            # ruff is a native binary; its Python wrapper module is not
            # bundled in the frozen exe. Forward `-m ruff ...` to the
            # bundled ruff binary, capturing its stdout/stderr and emitting
            # via the rebound stdio so callers receive it.
            if sys.argv[2] == "ruff":
                raise SystemExit(_forward_to_ruff_binary(sys.argv[3:]))
            module_name = sys.argv[2]
            sys.argv = [sys.argv[0], *sys.argv[3:]]
            import runpy
            runpy.run_module(module_name, run_name="__main__", alter_sys=True)
            raise SystemExit(0)

        # 其他有参数的情况：直接执行，不检查单实例锁
        # 例如 code 工具、browser tools 等
        # 让子进程自己处理参数或报错
        raise SystemExit(0)

    # 只有无参数时才检查单实例锁（双击启动桌面应用）
    if not _acquire_single_instance_lock():
        _show_already_running_message()
        raise SystemExit(0)

    from jiuwenswarm.channels.desktop.desktop_app import main as desktop_main
    try:
        desktop_main()
    finally:
        _release_single_instance_lock()
    return None


if __name__ == "__main__":
    main()
