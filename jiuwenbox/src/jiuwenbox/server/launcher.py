# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""jiuwenbox server 启动器 (HTTP / UDS 二选一).

通过 ``[project.scripts] jiuwenbox-server = jiuwenbox.server.launcher:main`` 安装
后, 可直接 ``jiuwenbox-server`` 启动; 同时支持 ``python -m jiuwenbox.server.launcher``。
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
from pathlib import Path
from typing import Tuple, Union
from urllib.parse import urlparse

from jiuwenbox.logging_config import configure_logging, patch_uvicorn_logging

logger = logging.getLogger("jiuwenbox.server.launcher")

ENV_LISTEN = "JIUWENBOX_LISTEN"
ENV_UDS_PATH = "JIUWENBOX_UDS_PATH"
ENV_UDS_MODE = "JIUWENBOX_UDS_MODE"
# Mirrors ``app.ENV_SAVE_LOGS_DIR``; intentionally re-declared here to keep
# the launcher independent of the FastAPI app module (``main`` runs before
# anything imports ``jiuwenbox.server.app`` lazily inside ``uvicorn.run``).
ENV_SAVE_LOGS_DIR = "JIUWENBOX_SAVE_LOGS_DIR"
_ENV_API_TOKEN_NAME = "JIUWENBOX_API_TOKEN"
DEFAULT_LISTEN = "http://0.0.0.0:8321"

HttpSpec = Tuple[str, str, int]   # ("http", host, port)
UnixSpec = Tuple[str, str]        # ("unix", abs_socket_path)
ListenSpec = Union[HttpSpec, UnixSpec]


class ListenURIError(ValueError):
    """``JIUWENBOX_LISTEN`` URI 解析失败."""


def parse_listen(uri: str) -> ListenSpec:
    """把 ``http://host:port`` / ``unix:///abs/path`` 解析成结构化形式.

    Args:
        uri: 监听地址 URI. ``scheme`` 必须是 ``http`` 或 ``unix``;
            ``unix`` 路径必须是绝对路径 (以 ``/`` 起头), 否则不同 cwd 下行为
            不一致, 且 lifespan / Docker volume 一律按绝对路径处理。

    Returns:
        - HTTP: ``("http", host, port)``;
        - UDS:  ``("unix", absolute_socket_path)``.

    Raises:
        ListenURIError: scheme 未知 / HTTP 缺 host 或 port / UDS 路径相对。
    """
    if not uri:
        raise ListenURIError("listen URI is empty")

    parsed = urlparse(uri)
    scheme = (parsed.scheme or "").lower()

    if scheme == "http":
        host = parsed.hostname
        port = parsed.port
        if not host or port is None:
            raise ListenURIError(
                f"http listen URI requires host and port, got {uri!r}"
            )
        if not (1 <= port <= 65535):
            raise ListenURIError(
                f"http port out of range in {uri!r}: {port}"
            )
        return ("http", host, int(port))

    if scheme == "unix":
        # 三斜杠 ``unix:///tmp/jw.sock`` -> netloc="", path="/tmp/jw.sock";
        # 两斜杠 ``unix://tmp/jw.sock`` -> netloc="tmp", path="/jw.sock"
        # —— 后者绝对会把绝对路径切碎, 直接拒绝。
        if parsed.netloc:
            raise ListenURIError(
                f"unix listen URI must use three slashes (unix:///abs/path), "
                f"got {uri!r}"
            )
        path = parsed.path
        if not path or not path.startswith("/"):
            raise ListenURIError(
                f"unix listen URI requires absolute path, got {uri!r}"
            )
        # ``/`` 本身是目录, 不能 bind socket; 提前拒掉而不是把错误推给
        # uvicorn 的 bind() 报 EISDIR / EACCES。
        if path == "/" or path.endswith("/"):
            raise ListenURIError(
                f"unix listen URI must point to a socket file, not a directory: "
                f"{uri!r}"
            )
        return ("unix", path)

    raise ListenURIError(
        f"unknown listen URI scheme {scheme!r} in {uri!r}; "
        f"expected 'http' or 'unix'"
    )


def _prepare_uds(path: str) -> None:
    """UDS 监听前的 socket 文件预处理.

    - 父目录不存在时 ``mkdir -p`` (用 caller umask, 不主动放权)。
    - ``path`` 已存在时:
        * 是一个 socket 文件 -> ``unlink`` (典型场景: 上一次 server crash
          留下的 stale socket; 不清理会让 uvicorn 在 ``bind()`` 阶段
          ``EADDRINUSE``)。
        * 是普通文件 / 目录 / 其它 -> raise ``FileExistsError`` 拒绝静默
          覆盖, 防止误删用户数据。
    """
    sock_path = Path(path)
    parent = sock_path.parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)

    if sock_path.exists() or sock_path.is_symlink():
        try:
            st = sock_path.lstat()
        except OSError as exc:
            raise FileExistsError(
                f"cannot stat existing path at {path!r}: {exc}"
            ) from exc
        if stat.S_ISSOCK(st.st_mode):
            try:
                sock_path.unlink()
            except OSError as exc:
                raise FileExistsError(
                    f"failed to unlink stale socket at {path!r}: {exc}"
                ) from exc
            logger.info("[launcher] removed stale socket %s", path)
        else:
            raise FileExistsError(
                f"refusing to listen on {path!r}: a non-socket file/dir "
                f"already exists; remove it manually if you really intend to."
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiuwenbox-server",
        description=(
            "Launch the jiuwenbox HTTP management server over TCP or UDS. "
            "Listen address is controlled by --listen or the JIUWENBOX_LISTEN "
            f"environment variable; default is {DEFAULT_LISTEN}."
        ),
    )
    parser.add_argument(
        "--listen",
        default=None,
        help=(
            "Listen URI: 'http://host:port' or 'unix:///abs/socket/path'. "
            f"Falls back to ${ENV_LISTEN} env, then {DEFAULT_LISTEN}."
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("JIUWENBOX_LOG_LEVEL", "info"),
        help="uvicorn log level (env JIUWENBOX_LOG_LEVEL, default 'info').",
    )
    parser.add_argument(
        "--save-logs",
        metavar="DIR",
        default=None,
        help=(
            "Persist the per-sandbox audit JSONL under DIR with a "
            "'{sandbox_id}-{timestamp}.audit.log' filename. "
            f"Falls back to ${ENV_SAVE_LOGS_DIR} env; unset means no "
            "log file is written at all (audit events stay only in the "
            "standard Python logger at DEBUG level). Raw daemon / "
            "background-exec stdout/stderr is never persisted; the "
            "audit log carries the truncated per-command stdout/stderr. "
            "The directory is created if missing; existing files are "
            "kept across boots."
        ),
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help=(
            "Enable Bearer token authentication for all HTTP endpoints "
            f"(including /health and /mcp). Falls back to ${_ENV_API_TOKEN_NAME} "
            "env; unset means authentication is disabled."
        ),
    )
    return parser


def _resolve_listen_uri(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    env_value = os.environ.get(ENV_LISTEN)
    if env_value:
        return env_value
    return DEFAULT_LISTEN


def _resolve_api_token(cli_value: str | None) -> str | None:
    raw = cli_value if cli_value else os.environ.get(_ENV_API_TOKEN_NAME)
    if not raw:
        return None
    token = raw.strip()
    return token or None


def _resolve_save_logs_dir(cli_value: str | None) -> str | None:
    """Pick CLI > env > unset; normalize to absolute path.

    We resolve to an absolute path here (not in app.lifespan) for two
    reasons: the launcher's CWD is more predictable than uvicorn's, and
    operators inspecting ``ps``/``env`` should see the real target dir,
    not a relative ``./logs`` whose meaning depends on where the server
    was started.
    """
    raw = cli_value if cli_value else os.environ.get(ENV_SAVE_LOGS_DIR)
    if not raw:
        return None
    return str(Path(raw).expanduser().resolve())


def main(argv: list[str] | None = None) -> int:
    """jiuwenbox-server CLI 入口.

    Returns:
        进程退出码; URI 非法直接 ``sys.exit(2)``。
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    configure_logging(level=log_level)

    # 启动期错误报告 (URI 解析 / uvicorn 导入 / UDS 文件冲突) 走 logger.error,
    # 而非 print(..., file=sys.stderr)。Python 的 ``logging.lastResort`` 在无
    # 自定义 handler 时会把 ERROR 级别消息写入 stderr, 与原 print 行为对齐;
    # 同时满足 G.LOG.02 (使用 logging 模块) 编码规范。
    raw_uri = _resolve_listen_uri(args.listen)
    try:
        spec = parse_listen(raw_uri)
    except ListenURIError as exc:
        logger.error("jiuwenbox-server: %s", exc)
        return 2

    # 把解析后的最终值塞回 env, 让 lifespan / 子模块都能看到一致的视图;
    # 即便用户没设 env 而是走了 --listen / 默认值, 后续也读得到。
    os.environ[ENV_LISTEN] = raw_uri

    # ``--save-logs`` 同样原则: 解析 + 落 env, app.lifespan 里只 ``os.environ.get``
    # 一处读取, 不必再做 CLI / env 优先级判定。未设置时把 env 也清掉,
    # 避免上一次启动残留干扰 (尤其本进程内多次调用 main 的测试场景)。
    save_logs_dir = _resolve_save_logs_dir(args.save_logs)
    if save_logs_dir:
        os.environ[ENV_SAVE_LOGS_DIR] = save_logs_dir
        logger.info("[launcher] sandbox logs will be saved under %s", save_logs_dir)
    else:
        os.environ.pop(ENV_SAVE_LOGS_DIR, None)

    api_token = _resolve_api_token(args.api_token)
    if api_token:
        os.environ[_ENV_API_TOKEN_NAME] = api_token
        logger.info("[launcher] API authentication enabled")
    else:
        os.environ.pop(_ENV_API_TOKEN_NAME, None)

    try:
        import uvicorn  # 延迟导入: argparse / URI 校验失败时不必拖 uvicorn 进来
    except ImportError as exc:  # pragma: no cover - declared in pyproject
        logger.error("jiuwenbox-server: uvicorn is required (%s)", exc)
        return 3

    patch_uvicorn_logging()

    uvicorn_kwargs: dict = {"log_level": args.log_level}
    if spec[0] == "http":
        _, host, port = spec
        # 清掉残留的 UDS_PATH, 避免多次启动时 lifespan 读到陈旧值
        os.environ.pop(ENV_UDS_PATH, None)
        uvicorn_kwargs["host"] = host
        uvicorn_kwargs["port"] = port
        logger.info("[launcher] starting jiuwenbox on http://%s:%d", host, port)
    else:
        _, path = spec
        try:
            _prepare_uds(path)
        except FileExistsError as exc:
            logger.error("jiuwenbox-server: %s", exc)
            return 2
        os.environ[ENV_UDS_PATH] = path
        uvicorn_kwargs["uds"] = path
        logger.info("[launcher] starting jiuwenbox on unix://%s", path)

    uvicorn.run("jiuwenbox.server.app:app", **uvicorn_kwargs)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
