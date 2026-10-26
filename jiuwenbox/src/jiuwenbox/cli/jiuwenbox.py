# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""jiuwenbox HTTP API 命令行客户端 (单文件实现).

入口: 通过 ``pyproject.toml`` 的 ``[project.scripts]`` 安装为可执行脚本 ``jiuwenbox``
(行为类似 ``uvicorn``); 也支持 ``python -m jiuwenbox.cli.jiuwenbox`` 的用法。

安装 (在 ``code_agent/jiuwenbox/`` 目录下)::

    pip install -e .   # 开发模式
    # 或
    pip install .

总体结构:

- ``_CliClient``: httpx.Client 薄封装, 复刻 ``agent-core`` 中
  ``_JiuwenBoxClient`` 的请求语义, 同时覆盖 ``policies`` / ``proxies`` 端点。
- ``build_parser``: argparse 嵌套子命令 ``health / sandbox / policy / proxy``。
- ``cmd_<group>_<action>``: 每个 leaf 子命令的处理函数;
  返回 ``dict`` / ``list`` 时主流程以 JSON 打印; ``sandbox exec`` 透传 stdout/stderr;
  返回 ``_ExecResult`` 时主流程直接透传退出码; 返回 ``None`` 表示已自行输出。
- ``_handle_error``: 区分 HTTPStatusError / ConnectError / 本地错误, 映射退出码。

退出码:
- 0: 成功; 沙箱 exec exit_code=0
- 1: HTTP 4xx/5xx 错误
- 2: 网络不可达
- 3: 本地参数错误 (env 解析、缺文件等); bg-get/bg-kill 404
- 130: Ctrl+C
- 其他正整数: 沙箱 exec 透传退出码; bg-exec started=false 时返回 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, IO, NoReturn, Optional

import httpx

logger = logging.getLogger("jiuwenbox.cli")


# ────────────────────────────── CLI output via logging ──────────────────────────────
#
# CLI 的 stdout / stderr 写出统一走 ``logging`` 模块, 满足 G.LOG.02
# (使用日志记录工具) 编码规范, 同时保留"调用方自行控制换行 / 不附加级别前缀"
# 的原始语义, 行为等同 ``sys.stdout/stderr.write``。
#
# 设计要点:
# - ``_NoTerminatorHandler`` 子类化 ``StreamHandler`` 把 ``terminator=""``,
#   不在每条记录后追加 ``\n``;
# - ``%(message)s`` Formatter 不输出级别 / 时间戳前缀;
# - ``propagate=False`` 防止冒泡到 root logger 造成重复输出;
# - 使用 ``logger.info("%s", text)`` 而非 ``logger.info(text)``, 避免 ``text``
#   中含 ``%`` 时被当作格式串。


class _NoTerminatorHandler(logging.StreamHandler):
    """``StreamHandler`` 不附加 ``\\n`` 终止符, 行为等同 ``stream.write``."""
    terminator = ""


_cli_stdout_logger = logging.getLogger("jiuwenbox.cli._stdout")
_cli_stderr_logger = logging.getLogger("jiuwenbox.cli._stderr")
_cli_output_loggers_initialized = False


def _ensure_cli_output_loggers() -> None:
    """首次输出前挂载 handler; 多次调用幂等。"""
    global _cli_output_loggers_initialized
    if _cli_output_loggers_initialized:
        return
    fmt = logging.Formatter("%(message)s")
    if not _cli_stdout_logger.handlers:
        h_out = _NoTerminatorHandler(sys.stdout)
        h_out.setFormatter(fmt)
        _cli_stdout_logger.addHandler(h_out)
        _cli_stdout_logger.setLevel(logging.INFO)
        _cli_stdout_logger.propagate = False
    if not _cli_stderr_logger.handlers:
        h_err = _NoTerminatorHandler(sys.stderr)
        h_err.setFormatter(fmt)
        _cli_stderr_logger.addHandler(h_err)
        _cli_stderr_logger.setLevel(logging.INFO)
        _cli_stderr_logger.propagate = False
    _cli_output_loggers_initialized = True


def _write_stdout(text: str) -> None:
    """CLI 程序输出 (JSON / table / plain / 透传 stdout); 不附加换行。"""
    _ensure_cli_output_loggers()
    _cli_stdout_logger.info("%s", text)


def _write_stderr(text: str) -> None:
    """CLI 用户诊断输出 (确认提示 / 进度信息 / 错误消息); 不附加换行。"""
    _ensure_cli_output_loggers()
    _cli_stderr_logger.info("%s", text)


def _flush_stderr() -> None:
    """同步刷新 stderr handler, 等价于 ``sys.stderr.flush()``。"""
    _ensure_cli_output_loggers()
    for handler in _cli_stderr_logger.handlers:
        handler.flush()


# ────────────────────────────── constants ──────────────────────────────

_ENV_BASE_URL = "JIUWENBOX_URL"
_ENV_TIMEOUT = "JIUWENBOX_TIMEOUT"
_ENV_API_TOKEN_NAME = "JIUWENBOX_API_TOKEN"
_DEFAULT_BASE_URL = "http://127.0.0.1:8321"
_DEFAULT_TIMEOUT = 30.0
_API_PREFIX = "/api/v1"
_UDS_SCHEME = "unix://"
# UDS transport 仍需要一个合法 absolute base_url 才能让 httpx 拼相对路径;
# 这里给一个占位 host, 实际请求完全走 UDS 不读它。
_UDS_PLACEHOLDER_BASE_URL = "http://jiuwenbox"

# 退出码
EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_CONNECT_ERROR = 2
EXIT_LOCAL_ERROR = 3
EXIT_INTERRUPT = 130


# ────────────────────────────── sentinels ──────────────────────────────


@dataclass
class _ExecResult:
    """``sandbox exec`` 哨兵返回值, 让主流程直接透传沙箱退出码。"""
    exit_code: int


class _CliError(Exception):
    """本地参数 / 配置错误, 退出码 3。"""

    def __init__(self, message: str, *, exit_code: int = EXIT_LOCAL_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ────────────────────────────── http helpers ──────────────────────────────


def _response_error_detail(response: httpx.Response) -> str:
    """从响应中尽力提取人类可读的错误信息。"""
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()

    if isinstance(payload, dict):
        for key in ("error", "detail", "message"):
            value = payload.get(key)
            if value:
                return value if isinstance(value, str) else json.dumps(
                    value, ensure_ascii=False,
                )
        return json.dumps(payload, ensure_ascii=False)

    if payload:
        return payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False,
        )
    return response.text.strip()


def _raise_for_status(response: httpx.Response) -> None:
    """复刻 agent-core 版本: 非 2xx 抛 HTTPStatusError 并附加 detail。"""
    if response.is_success:
        return
    detail = _response_error_detail(response)
    message = (
        f"HTTP {response.status_code} {response.reason_phrase}"
        f" for {response.request.method} {response.request.url}"
    )
    if detail:
        message = f"{message}: {detail}"
    raise httpx.HTTPStatusError(message, request=response.request, response=response)


def _split_uds_endpoint(base_url: str) -> Optional[str]:
    """如果 ``base_url`` 是 ``unix:///abs/path`` 则返回 socket 路径, 否则返回 None.

    与 server 端 :func:`jiuwenbox.server.launcher.parse_listen` 的 UDS 语义保持一致:
    必须三斜杠 + 绝对路径; 相对路径直接抛 :class:`_CliError`, 走退出码 3。
    """
    if not base_url.startswith(_UDS_SCHEME):
        return None
    path = base_url[len(_UDS_SCHEME):]
    if not path.startswith("/"):
        raise _CliError(
            f"unix endpoint requires absolute path (unix:///abs/path), "
            f"got {base_url!r}"
        )
    return path


# ────────────────────────────── _CliClient ──────────────────────────────


class _CliClient:
    """httpx.Client 薄封装。

    与 ``agent-core/openjiuwen/extensions/sys_operation/sandbox/providers/
    jiuwenbox.py::_JiuwenBoxClient`` 同语义, 额外覆盖 ``policies`` / ``proxies``
    端点; 文件上传走 multipart, 下载流式收 bytes。

    ``base_url`` 同时支持 TCP (``http://host:port``) 与 Unix Domain Socket
    (``unix:///abs/path/to/sock``) 两种形态; 后者会走
    :class:`httpx.HTTPTransport` 的 ``uds=...`` 参数, 而对外暴露的
    ``self._base_url`` 仍保留原始 URI 串, 便于错误提示 (如
    ``cannot connect to unix:///tmp/jw.sock``) 仍可读。
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
        api_token: str | None = None,
    ) -> None:
        cleaned = base_url.rstrip("/")
        self._base_url = cleaned
        self._timeout = timeout_seconds
        client_headers: dict[str, str] = {}
        if api_token:
            client_headers["Authorization"] = f"Bearer {api_token}"
        uds_path = _split_uds_endpoint(cleaned)
        if uds_path is not None:
            self._client = httpx.Client(
                transport=httpx.HTTPTransport(uds=uds_path),
                base_url=_UDS_PLACEHOLDER_BASE_URL,
                timeout=timeout_seconds,
                headers=client_headers or None,
            )
        else:
            self._client = httpx.Client(
                base_url=cleaned,
                timeout=timeout_seconds,
                headers=client_headers or None,
            )

    def __enter__(self) -> "_CliClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def base_url(self) -> str:
        return self._base_url

    # ── 基础 GET/POST/DELETE/PUT 包装 (统一抛 HTTPStatusError) ──

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        logger.debug("GET %s params=%s", path, kwargs.get("params"))
        response = self._client.get(path, **kwargs)
        _raise_for_status(response)
        return response

    def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        logger.debug("POST %s", path)
        response = self._client.post(path, **kwargs)
        _raise_for_status(response)
        return response

    def _put(self, path: str, **kwargs: Any) -> httpx.Response:
        logger.debug("PUT %s", path)
        response = self._client.put(path, **kwargs)
        _raise_for_status(response)
        return response

    def _delete(self, path: str, **kwargs: Any) -> httpx.Response:
        logger.debug("DELETE %s", path)
        response = self._client.delete(path, **kwargs)
        _raise_for_status(response)
        return response

    # ── /health ──

    def health(self) -> dict[str, Any]:
        return dict(self._get("/health").json())

    # ── /api/v1/sandboxes/* ──

    def sandbox_create(
        self,
        *,
        env: dict[str, str] | None = None,
        policy: Any = None,
        policy_mode: str | None = None,
        sandbox_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if env is not None:
            body["env"] = env
        if policy is not None:
            body["policy"] = policy
        if policy_mode is not None:
            body["policy_mode"] = policy_mode
        if sandbox_id is not None:
            body["sandbox_id"] = sandbox_id
        return dict(self._post(f"{_API_PREFIX}/sandboxes", json=body).json())

    def sandbox_list(self) -> list[dict[str, Any]]:
        return list(self._get(f"{_API_PREFIX}/sandboxes").json())

    def sandbox_get(self, sandbox_id: str) -> dict[str, Any]:
        return dict(self._get(f"{_API_PREFIX}/sandboxes/{sandbox_id}").json())

    def sandbox_delete(self, sandbox_id: str) -> None:
        self._delete(f"{_API_PREFIX}/sandboxes/{sandbox_id}")

    def sandbox_start(self, sandbox_id: str) -> dict[str, Any]:
        return dict(self._post(f"{_API_PREFIX}/sandboxes/{sandbox_id}/start").json())

    def sandbox_stop(self, sandbox_id: str) -> dict[str, Any]:
        return dict(self._post(f"{_API_PREFIX}/sandboxes/{sandbox_id}/stop").json())

    def sandbox_restart(self, sandbox_id: str) -> dict[str, Any]:
        return dict(self._post(f"{_API_PREFIX}/sandboxes/{sandbox_id}/restart").json())

    def sandbox_exec(
        self,
        sandbox_id: str,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"command": command}
        if cwd is not None:
            body["workdir"] = cwd
        if env is not None:
            body["env"] = env
        if stdin is not None:
            body["stdin"] = stdin
        if timeout_seconds is not None:
            body["timeout_seconds"] = timeout_seconds
        # 让 http 超时大于沙箱内部 timeout, 避免提前断流
        http_timeout = max(self._timeout, (timeout_seconds or 0) + 5)
        return dict(self._post(
            f"{_API_PREFIX}/sandboxes/{sandbox_id}/exec",
            json=body, timeout=http_timeout,
        ).json())

    def sandbox_exec_background(
        self,
        sandbox_id: str,
        command: list[str],
        *,
        job_id: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "command": command,
        }
        if job_id is not None:
            body["job_id"] = job_id
        if cwd is not None:
            body["workdir"] = cwd
        if env is not None:
            body["env"] = env
        if stdin is not None:
            body["stdin"] = stdin
        return dict(self._post(
            f"{_API_PREFIX}/sandboxes/{sandbox_id}/exec_background", json=body,
        ).json())

    def sandbox_bg_get(self, sandbox_id: str, job_id: str) -> dict[str, Any]:
        return dict(self._get(
            f"{_API_PREFIX}/sandboxes/{sandbox_id}/background/{job_id}",
        ).json())

    def sandbox_bg_list(
        self,
        sandbox_id: str,
        *,
        running_only: bool = False,
    ) -> list[dict[str, Any]]:
        payload = self._get(
            f"{_API_PREFIX}/sandboxes/{sandbox_id}/background",
            params={"running_only": str(bool(running_only)).lower()},
        ).json()
        return list(payload.get("items") or [])

    def sandbox_bg_kill(
        self,
        sandbox_id: str,
        job_id: str,
        *,
        signal: int = 15,
    ) -> dict[str, Any]:
        return dict(self._post(
            f"{_API_PREFIX}/sandboxes/{sandbox_id}/background/{job_id}/kill",
            json={"signal": signal},
        ).json())

    def sandbox_logs(self, sandbox_id: str) -> str:
        return self._get(f"{_API_PREFIX}/sandboxes/{sandbox_id}/logs").text

    def sandbox_upload(
        self,
        sandbox_id: str,
        sandbox_path: str,
        *,
        file_name: str,
        content: bytes,
    ) -> None:
        self._post(
            f"{_API_PREFIX}/sandboxes/{sandbox_id}/upload",
            params={"sandbox_path": sandbox_path},
            files={"file": (file_name, content)},
        )

    def sandbox_download(self, sandbox_id: str, sandbox_path: str) -> bytes:
        response = self._get(
            f"{_API_PREFIX}/sandboxes/{sandbox_id}/download",
            params={"sandbox_path": sandbox_path},
        )
        return response.content

    def sandbox_files(
        self,
        sandbox_id: str,
        sandbox_path: str,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        include_files: bool = True,
        include_dirs: bool = True,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "sandbox_path": sandbox_path,
            "recursive": str(bool(recursive)).lower(),
            "include_files": str(bool(include_files)).lower(),
            "include_dirs": str(bool(include_dirs)).lower(),
        }
        if max_depth is not None:
            params["max_depth"] = int(max_depth)
        payload = self._get(f"{_API_PREFIX}/sandboxes/{sandbox_id}/files", params=params).json()
        return list(payload.get("items") or [])

    def sandbox_search(
        self,
        sandbox_id: str,
        sandbox_path: str,
        pattern: str,
        *,
        exclude_patterns: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("sandbox_path", sandbox_path),
            ("pattern", pattern),
        ]
        for ex in exclude_patterns or []:
            params.append(("exclude_patterns", ex))
        payload = self._get(f"{_API_PREFIX}/sandboxes/{sandbox_id}/search", params=params).json()
        return list(payload.get("items") or [])

    # ── /api/v1/policies/* ──

    def policy_get(self, sandbox_id: str) -> dict[str, Any]:
        return dict(self._get(f"{_API_PREFIX}/policies/{sandbox_id}").json())

    # ── /api/v1/proxies/* ──

    def proxy_create(
        self,
        *,
        path_prefix: str,
        target_endpoint: str,
        api_key: str | None = None,
        skip_cert_verify: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "path_prefix": path_prefix,
            "target_endpoint": target_endpoint,
            "skip_cert_verify": bool(skip_cert_verify),
        }
        if api_key is not None:
            body["api_key"] = api_key
        return dict(self._post(f"{_API_PREFIX}/proxies", json=body).json())

    def proxy_list(self) -> list[dict[str, Any]]:
        return list(self._get(f"{_API_PREFIX}/proxies").json())

    def proxy_get(self, name: str) -> dict[str, Any]:
        return dict(self._get(f"{_API_PREFIX}/proxies/{name}").json())

    def proxy_delete(self, name: str) -> dict[str, Any]:
        return dict(self._delete(f"{_API_PREFIX}/proxies/{name}").json())

    def proxy_start(self, name: str) -> dict[str, Any]:
        return dict(self._post(f"{_API_PREFIX}/proxies/{name}/start").json())

    def proxy_stop(self, name: str) -> dict[str, Any]:
        return dict(self._post(f"{_API_PREFIX}/proxies/{name}/stop").json())

    def proxy_update(
        self,
        name: str,
        *,
        path_prefix: str,
        target_endpoint: str,
        api_key: str | None = None,
        skip_cert_verify: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "path_prefix": path_prefix,
            "target_endpoint": target_endpoint,
            "skip_cert_verify": bool(skip_cert_verify),
        }
        if api_key is not None:
            body["api_key"] = api_key
        return dict(self._put(f"{_API_PREFIX}/proxies/{name}", json=body).json())

    def proxy_logs(self, name: str, *, lines: int | None = None) -> str:
        params: dict[str, Any] = {}
        if lines is not None:
            params["lines"] = int(lines)
        return self._get(f"{_API_PREFIX}/proxies/{name}/logs", params=params).text


# ────────────────────────────── arg parsers helpers ──────────────────────────────


def _parse_env_pair(token: str) -> tuple[str, str]:
    if "=" not in token:
        raise _CliError(
            f"invalid --env value: {token!r}; expected KEY=VAL",
        )
    key, _, value = token.partition("=")
    if not key:
        raise _CliError(
            f"invalid --env value: {token!r}; KEY must be non-empty",
        )
    return key, value


def _parse_env_list(tokens: list[str] | None) -> dict[str, str] | None:
    if not tokens:
        return None
    out: dict[str, str] = {}
    for tok in tokens:
        key, value = _parse_env_pair(tok)
        out[key] = value
    return out


def _load_policy_file(path: str) -> Any:
    p = Path(path)
    if not p.is_file():
        raise _CliError(f"policy file not found: {path}")
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:  # pragma: no cover - pyyaml is a dep
            raise _CliError(f"pyyaml required to load {path}: {exc}") from exc
        return yaml.safe_load(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _CliError(f"policy file {path} is not valid JSON: {exc}") from exc


def _resolve_stdin_input(value: Optional[str]) -> Optional[str]:
    """``--stdin -`` → 从 host stdin 读取整段; 普通字符串 → 直接返回。"""
    if value is None:
        return None
    if value == "-":
        return sys.stdin.read()
    return value


# ────────────────────────────── output formatters ──────────────────────────────


def _print_json(result: Any) -> None:
    _write_stdout(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


# ────────────────────────────── stderr helpers ──────────────────────────────


def _stderr_supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stderr.isatty()


def _eprint(message: str, *, kind: str = "error", color: bool = True) -> None:
    """带可选颜色的 stderr 输出。"""
    prefix = f"{kind}: "
    if color and _stderr_supports_color():
        # 红色 ERROR / 黄色 WARN
        ansi = "\033[31m" if kind == "error" else "\033[33m"
        prefix = f"{ansi}{prefix}\033[0m"
    _write_stderr(prefix + message + "\n")


# ────────────────────────────── command handlers ──────────────────────────────


# ── health ──

def cmd_health(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.health()


# ── sandbox ──

def cmd_sandbox_create(args: argparse.Namespace, client: _CliClient) -> Any:
    env = _parse_env_list(args.env)
    policy = _load_policy_file(args.policy_file) if args.policy_file else None
    return client.sandbox_create(
        env=env,
        policy=policy,
        policy_mode=args.policy_mode,
        sandbox_id=args.sandbox_id,
    )


def cmd_sandbox_ls(args: argparse.Namespace, client: _CliClient) -> Any:
    sandboxes = client.sandbox_list()
    if args.phase:
        sandboxes = [s for s in sandboxes if s.get("phase") == args.phase]
    return sandboxes


def cmd_sandbox_get(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.sandbox_get(args.sandbox_id)


def cmd_sandbox_rm(args: argparse.Namespace, client: _CliClient) -> Any:
    if not args.yes and sys.stdin.isatty():
        _write_stderr(f"Delete sandbox {args.sandbox_id}? [y/N] ")
        _flush_stderr()
        answer = sys.stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            _write_stderr("aborted\n")
            return None
    client.sandbox_delete(args.sandbox_id)
    _write_stderr(f"deleted sandbox {args.sandbox_id}\n")
    return None


def cmd_sandbox_start(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.sandbox_start(args.sandbox_id)


def cmd_sandbox_stop(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.sandbox_stop(args.sandbox_id)


def cmd_sandbox_restart(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.sandbox_restart(args.sandbox_id)


def _strip_command_separator(command: list[str]) -> list[str]:
    """``argparse.REMAINDER`` 会把分隔符 ``--`` 也吞进 list, 这里去掉首个 ``--``。"""
    if command and command[0] == "--":
        return command[1:]
    return command


def cmd_sandbox_exec(args: argparse.Namespace, client: _CliClient) -> Any:
    command: list[str] = _strip_command_separator(list(args.command or []))
    if not command:
        raise _CliError("missing command after `--`; example: exec ID -- ls /tmp")
    env = _parse_env_list(args.env)
    stdin_text = _resolve_stdin_input(args.stdin)
    result = client.sandbox_exec(
        args.sandbox_id,
        command,
        cwd=args.cwd,
        env=env,
        stdin=stdin_text,
        timeout_seconds=args.timeout_seconds,
    )
    stdout_text = result.get("stdout") or ""
    stderr_text = result.get("stderr") or ""
    if stdout_text:
        _write_stdout(stdout_text)
    if stderr_text:
        _write_stderr(stderr_text)
    exit_code = int(result.get("exit_code") or 0)
    return _ExecResult(exit_code=exit_code)


def cmd_sandbox_bg_exec(args: argparse.Namespace, client: _CliClient) -> Any:
    command: list[str] = _strip_command_separator(list(args.command or []))
    if not command:
        raise _CliError("missing command after `--`")
    env = _parse_env_list(args.env)
    stdin_text = _resolve_stdin_input(args.stdin)
    result = client.sandbox_exec_background(
        args.sandbox_id,
        command,
        job_id=args.job_id,
        cwd=args.cwd,
        env=env,
        stdin=stdin_text,
    )
    if not result.get("started"):
        raise _CliError(result.get("error_message") or "background exec failed")
    return result


def _reraise_bg_http_error(exc: httpx.HTTPStatusError) -> NoReturn:
    """Map background-job 404 responses to exit code 3."""
    if exc.response.status_code == 404:
        detail = _response_error_detail(exc.response)
        raise _CliError(detail or "background job not found", exit_code=EXIT_LOCAL_ERROR) from exc
    raise exc


def cmd_sandbox_bg_get(args: argparse.Namespace, client: _CliClient) -> Any:
    try:
        return client.sandbox_bg_get(args.sandbox_id, args.job_id)
    except httpx.HTTPStatusError as exc:
        _reraise_bg_http_error(exc)


def cmd_sandbox_bg_list(args: argparse.Namespace, client: _CliClient) -> Any:
    items = client.sandbox_bg_list(args.sandbox_id, running_only=args.running_only)
    return {"items": items}


def cmd_sandbox_bg_kill(args: argparse.Namespace, client: _CliClient) -> Any:
    try:
        return client.sandbox_bg_kill(
            args.sandbox_id,
            args.job_id,
            signal=args.signal,
        )
    except httpx.HTTPStatusError as exc:
        _reraise_bg_http_error(exc)


def cmd_sandbox_logs(args: argparse.Namespace, client: _CliClient) -> Any:
    text = client.sandbox_logs(args.sandbox_id)
    # logs 是 text/plain, 直接打 stdout, 不参与 JSON 格式化
    _write_stdout(text)
    if text and not text.endswith("\n"):
        _write_stdout("\n")
    return None


def cmd_sandbox_upload(args: argparse.Namespace, client: _CliClient) -> Any:
    local = args.local_path
    if local == "-":
        content = sys.stdin.buffer.read()
        file_name = "stdin.bin"
    else:
        path = Path(local)
        if not path.is_file():
            raise _CliError(f"local file not found: {local}")
        content = path.read_bytes()
        file_name = path.name
    client.sandbox_upload(
        args.sandbox_id,
        args.sandbox_path,
        file_name=file_name,
        content=content,
    )
    _write_stderr(f"uploaded {len(content)} bytes -> {args.sandbox_path}\n")
    return None


def cmd_sandbox_download(args: argparse.Namespace, client: _CliClient) -> Any:
    content = client.sandbox_download(args.sandbox_id, args.sandbox_path)
    local = args.local_path or "-"
    if local == "-":
        # binary-safe to stdout
        sys.stdout.buffer.write(content)
        sys.stdout.buffer.flush()
    else:
        Path(local).write_bytes(content)
        _write_stderr(f"downloaded {len(content)} bytes -> {local}\n")
    return None


def cmd_sandbox_files(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.sandbox_files(
        args.sandbox_id,
        args.sandbox_path,
        recursive=args.recursive,
        max_depth=args.max_depth,
        include_files=not args.no_files,
        include_dirs=not args.no_dirs,
    )


def cmd_sandbox_find(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.sandbox_search(
        args.sandbox_id,
        args.sandbox_path,
        args.pattern,
        exclude_patterns=args.exclude or None,
    )


# ── policy ──

def cmd_policy_get(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.policy_get(args.sandbox_id)


# ── proxy ──

def cmd_proxy_create(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.proxy_create(
        path_prefix=args.prefix,
        target_endpoint=args.target,
        api_key=args.api_key,
        skip_cert_verify=args.skip_cert,
    )


def cmd_proxy_ls(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.proxy_list()


def cmd_proxy_get(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.proxy_get(args.name)


def cmd_proxy_rm(args: argparse.Namespace, client: _CliClient) -> Any:
    if not args.yes and sys.stdin.isatty():
        _write_stderr(f"Delete proxy {args.name}? [y/N] ")
        _flush_stderr()
        answer = sys.stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            _write_stderr("aborted\n")
            return None
    return client.proxy_delete(args.name)


def cmd_proxy_start(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.proxy_start(args.name)


def cmd_proxy_stop(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.proxy_stop(args.name)


def cmd_proxy_update(args: argparse.Namespace, client: _CliClient) -> Any:
    return client.proxy_update(
        args.name,
        path_prefix=args.prefix,
        target_endpoint=args.target,
        api_key=args.api_key,
        skip_cert_verify=args.skip_cert,
    )


def cmd_proxy_logs(args: argparse.Namespace, client: _CliClient) -> Any:
    text = client.proxy_logs(args.name, lines=args.lines)
    _write_stdout(text)
    if text and not text.endswith("\n"):
        _write_stdout("\n")
    return None


# ────────────────────────────── argparse build_parser ──────────────────────────────


def _add_global_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--base-url",
        default=os.environ.get(_ENV_BASE_URL, _DEFAULT_BASE_URL),
        help=(
            f"jiuwenbox HTTP base url; accepts http://host:port or "
            f"unix:///abs/socket/path (env {_ENV_BASE_URL}, "
            f"default {_DEFAULT_BASE_URL})"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get(_ENV_TIMEOUT, _DEFAULT_TIMEOUT)),
        help=f"HTTP client timeout seconds (env {_ENV_TIMEOUT}, default {int(_DEFAULT_TIMEOUT)})",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get(_ENV_API_TOKEN_NAME),
        help=(
            f"Bearer token for API authentication (env {_ENV_API_TOKEN_NAME}; "
            f"must match server-side {_ENV_API_TOKEN_NAME} when enabled)"
        ),
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="enable debug logging on stderr",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable ANSI colors on stderr",
    )


def _add_sandbox_id(p: argparse.ArgumentParser) -> None:
    p.add_argument("sandbox_id", help="sandbox id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jiuwenbox",
        description="jiuwenbox HTTP API client CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              jiuwenbox health
              jiuwenbox sandbox create
              jiuwenbox sandbox exec <ID> -- python3 -c 'print(1)'
              jiuwenbox sandbox upload <ID> ./local.txt /tmp/remote.txt
              jiuwenbox --base-url unix:///tmp/jw.sock health
              JIUWENBOX_URL=unix:///tmp/jw.sock jiuwenbox sandbox ls
        """),
    )
    _add_global_options(parser)

    groups = parser.add_subparsers(
        dest="group", metavar="GROUP",
    )
    groups.required = True

    # ── health ──
    p_health = groups.add_parser("health", help="server health check")
    p_health.set_defaults(_handler=cmd_health)

    # ── sandbox group ──
    p_sandbox = groups.add_parser("sandbox", help="manage sandboxes")
    sandbox_subs = p_sandbox.add_subparsers(dest="action", metavar="ACTION")
    sandbox_subs.required = True

    p = sandbox_subs.add_parser("create", help="create sandbox")
    p.add_argument(
        "--env", action="append", metavar="KEY=VAL",
        help="environment variable (repeatable)",
    )
    p.add_argument(
        "--policy-file", help="local JSON or YAML policy file",
    )
    p.add_argument(
        "--policy-mode", choices=["override", "append"], default=None,
        help="policy merge mode (default server-side 'override')",
    )
    p.add_argument(
        "--sandbox-id",
        help="optional sandbox id (4-40 chars: lowercase letters, digits, -, _)",
    )
    p.set_defaults(_handler=cmd_sandbox_create)

    p = sandbox_subs.add_parser("ls", help="list sandboxes")
    p.add_argument("--phase", help="filter by phase locally")
    p.set_defaults(_handler=cmd_sandbox_ls)

    p = sandbox_subs.add_parser("get", help="get sandbox state")
    _add_sandbox_id(p)
    p.set_defaults(_handler=cmd_sandbox_get)

    p = sandbox_subs.add_parser("rm", help="delete sandbox")
    _add_sandbox_id(p)
    p.add_argument(
        "--yes", "-y", action="store_true", help="skip confirmation",
    )
    p.set_defaults(_handler=cmd_sandbox_rm)

    for action_name, handler in (
        ("start", cmd_sandbox_start),
        ("stop", cmd_sandbox_stop),
        ("restart", cmd_sandbox_restart),
    ):
        p = sandbox_subs.add_parser(action_name, help=f"{action_name} sandbox")
        _add_sandbox_id(p)
        p.set_defaults(_handler=handler)

    p = sandbox_subs.add_parser(
        "exec", help="execute command in sandbox (sync)",
    )
    _add_sandbox_id(p)
    p.add_argument("--cwd", help="working directory inside sandbox")
    p.add_argument(
        "--env", action="append", metavar="KEY=VAL",
        help="environment variable (repeatable)",
    )
    p.add_argument(
        "--timeout-seconds", type=int, default=None,
        help="command timeout seconds (inside sandbox)",
    )
    p.add_argument(
        "--stdin",
        help="stdin text; use '-' to read from host stdin",
    )
    # 必须用 `--` 把待执行命令与本 CLI 自身的选项隔开 (类似 docker exec / kubectl exec)。
    # 不使用 argparse.REMAINDER, 否则 ``--stdin``/``--cwd`` 等出现在 sandbox_id 之后时
    # 会被一股脑塞进 command 列表; ``nargs="*"`` + 用户加 ``--`` 才能让本侧选项被识别。
    p.add_argument(
        "command", nargs="*",
        help="command to run; place after `--`",
    )
    p.set_defaults(_handler=cmd_sandbox_exec)

    p = sandbox_subs.add_parser(
        "bg-exec", help="execute command in background (non-blocking)",
    )
    _add_sandbox_id(p)
    p.add_argument("--cwd", help="working directory inside sandbox")
    p.add_argument(
        "--env", action="append", metavar="KEY=VAL",
        help="environment variable (repeatable)",
    )
    p.add_argument(
        "--stdin",
        help="stdin text; use '-' to read from host stdin",
    )
    p.add_argument(
        "--job-id",
        help="optional background job id (4-40 chars, [0-9a-z_-])",
    )
    p.add_argument(
        "command", nargs="*",
        help="command to run; place after `--`",
    )
    p.set_defaults(_handler=cmd_sandbox_bg_exec)

    p = sandbox_subs.add_parser("bg-get", help="get background job status")
    _add_sandbox_id(p)
    p.add_argument("job_id", help="background job id")
    p.set_defaults(_handler=cmd_sandbox_bg_get)

    p = sandbox_subs.add_parser("bg-list", help="list background jobs")
    _add_sandbox_id(p)
    p.add_argument(
        "--running-only",
        action="store_true",
        help="only show running jobs",
    )
    p.set_defaults(_handler=cmd_sandbox_bg_list)

    p = sandbox_subs.add_parser("bg-kill", help="kill a background job")
    _add_sandbox_id(p)
    p.add_argument("job_id", help="background job id")
    p.add_argument(
        "--signal",
        type=int,
        default=15,
        help="signal number (default 15 = SIGTERM)",
    )
    p.set_defaults(_handler=cmd_sandbox_bg_kill)

    p = sandbox_subs.add_parser("logs", help="get audit logs (text)")
    _add_sandbox_id(p)
    p.set_defaults(_handler=cmd_sandbox_logs)

    p = sandbox_subs.add_parser("upload", help="upload local file to sandbox")
    _add_sandbox_id(p)
    p.add_argument("local_path", help="local file path; '-' = stdin")
    p.add_argument("sandbox_path", help="sandbox destination path")
    p.set_defaults(_handler=cmd_sandbox_upload)

    p = sandbox_subs.add_parser("download", help="download sandbox file")
    _add_sandbox_id(p)
    p.add_argument("sandbox_path", help="sandbox source path")
    p.add_argument(
        "local_path", nargs="?", default=None,
        help="local destination path; '-' or omit = stdout",
    )
    p.set_defaults(_handler=cmd_sandbox_download)

    p = sandbox_subs.add_parser("files", help="list files in sandbox directory")
    _add_sandbox_id(p)
    p.add_argument("sandbox_path", help="sandbox directory")
    p.add_argument("--recursive", "-r", action="store_true")
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--no-files", action="store_true", help="exclude files")
    p.add_argument("--no-dirs", action="store_true", help="exclude directories")
    p.set_defaults(_handler=cmd_sandbox_files)

    p = sandbox_subs.add_parser("find", help="search sandbox files by glob")
    _add_sandbox_id(p)
    p.add_argument("sandbox_path", help="search root")
    p.add_argument("pattern", help="glob pattern")
    p.add_argument(
        "-x", "--exclude", action="append",
        help="exclude pattern (repeatable)",
    )
    p.set_defaults(_handler=cmd_sandbox_find)

    # ── policy group ──
    p_policy = groups.add_parser("policy", help="inspect sandbox policy")
    policy_subs = p_policy.add_subparsers(dest="action", metavar="ACTION")
    policy_subs.required = True

    p = policy_subs.add_parser("get", help="get effective policy of sandbox")
    _add_sandbox_id(p)
    p.set_defaults(_handler=cmd_policy_get)

    # ── proxy group ──
    p_proxy = groups.add_parser("proxy", help="manage inference proxies")
    proxy_subs = p_proxy.add_subparsers(dest="action", metavar="ACTION")
    proxy_subs.required = True

    p = proxy_subs.add_parser("create", help="create proxy route")
    p.add_argument("--prefix", required=True, help="path prefix, e.g. /openai")
    p.add_argument("--target", required=True, help="upstream endpoint")
    p.add_argument("--api-key", default=None)
    p.add_argument("--skip-cert", action="store_true", help="skip TLS verify")
    p.set_defaults(_handler=cmd_proxy_create)

    p = proxy_subs.add_parser("ls", help="list proxies")
    p.set_defaults(_handler=cmd_proxy_ls)

    p = proxy_subs.add_parser("get", help="get proxy detail")
    p.add_argument("name")
    p.set_defaults(_handler=cmd_proxy_get)

    p = proxy_subs.add_parser("rm", help="delete proxy")
    p.add_argument("name")
    p.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    p.set_defaults(_handler=cmd_proxy_rm)

    for action_name, handler in (
        ("start", cmd_proxy_start),
        ("stop", cmd_proxy_stop),
    ):
        p = proxy_subs.add_parser(action_name, help=f"{action_name} proxy")
        p.add_argument("name")
        p.set_defaults(_handler=handler)

    p = proxy_subs.add_parser("update", help="update proxy route")
    p.add_argument("name")
    p.add_argument("--prefix", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--api-key", default=None)
    p.add_argument("--skip-cert", action="store_true")
    p.set_defaults(_handler=cmd_proxy_update)

    p = proxy_subs.add_parser("logs", help="tail proxy logs")
    p.add_argument("name")
    p.add_argument("--lines", type=int, default=None)
    p.set_defaults(_handler=cmd_proxy_logs)

    return parser


# ────────────────────────────── error handler ──────────────────────────────


def _handle_error(exc: BaseException, *, verbose: bool, base_url: str) -> int:
    if isinstance(exc, KeyboardInterrupt):
        _eprint("interrupted", kind="warn")
        return EXIT_INTERRUPT
    if isinstance(exc, _CliError):
        _eprint(str(exc))
        return exc.exit_code
    if isinstance(exc, httpx.HTTPStatusError):
        _eprint(str(exc))
        return EXIT_API_ERROR
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        _eprint(
            f"cannot connect to {base_url}; is jiuwenbox running?",
        )
        return EXIT_CONNECT_ERROR
    if isinstance(exc, httpx.ReadTimeout):
        _eprint(f"request to {base_url} timed out")
        return EXIT_CONNECT_ERROR
    if isinstance(exc, FileNotFoundError):
        _eprint(f"{exc}")
        return EXIT_LOCAL_ERROR
    _eprint(f"{type(exc).__name__}: {exc}")
    if verbose:
        import traceback
        traceback.print_exc(file=sys.stderr)
    return EXIT_API_ERROR


# ────────────────────────────── main entry ──────────────────────────────


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


_EXEC_SUBCOMMAND_SEQUENCES: tuple[tuple[str, str], ...] = (
    ("sandbox", "exec"),
    ("sandbox", "bg-exec"),
)


def _split_argv_on_command_separator(
    argv: list[str],
) -> tuple[list[str], list[str] | None]:
    """把首次独立出现的 ``--`` 之后的 token 切出来作为 ``sandbox exec`` 命令尾部。

    背景: argparse 的 subparser × ``nargs="*"`` × ``--`` 组合长期存在不稳定行为
    (子解析器消费完已声明的选项后, 残留的 ``-- cat`` 会被冒泡回顶层 parser 报
    ``unrecognized arguments``)。我们在送入 argparse 之前手动切, 让 argparse
    完全看不到 ``--``, 也就不会触发这个坑。

    只在 argv 中出现连续的 ``sandbox exec`` / ``sandbox bg-exec`` 子命令序列
    且 ``--`` 出现在该序列之后时才做预切分; 其余命令保留 argparse 标准的
    ``--`` 终止符语义 (比如允许用户写 ``sandbox rm -- --weird-id`` 删除以
    ``--`` 开头的 id)。

    返回 (head_argv, tail_argv); tail_argv 为 None 表示无需切分。
    """
    if "--" not in argv:
        return list(argv), None
    sep_idx = argv.index("--")
    prefix = argv[:sep_idx]
    has_exec_sequence = False
    for sequence in _EXEC_SUBCOMMAND_SEQUENCES:
        seq_len = len(sequence)
        for start in range(len(prefix) - seq_len + 1):
            if tuple(prefix[start:start + seq_len]) == sequence:
                has_exec_sequence = True
                break
        if has_exec_sequence:
            break
    if not has_exec_sequence:
        return list(argv), None
    return list(prefix), list(argv[sep_idx + 1:])


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    head_argv, tail_argv = _split_argv_on_command_separator(list(argv))

    parser = build_parser()
    args = parser.parse_args(head_argv)
    # 仅 ``sandbox exec`` / ``sandbox bg-exec`` 会声明 ``command`` 属性。
    # 用户在 ``--`` 之后写的 token 一律拼到该 list 末尾。
    if tail_argv is not None and hasattr(args, "command"):
        existing = list(getattr(args, "command", None) or [])
        args.command = existing + tail_argv
    _configure_logging(getattr(args, "verbose", False))

    handler: Callable[[argparse.Namespace, _CliClient], Any] | None = getattr(
        args, "_handler", None,
    )
    if handler is None:
        parser.error("no command specified")

    base_url = args.base_url
    api_token = getattr(args, "api_token", None)
    if isinstance(api_token, str):
        api_token = api_token.strip() or None
    try:
        with _CliClient(
            base_url=base_url,
            timeout_seconds=args.timeout,
            api_token=api_token,
        ) as client:
            try:
                result = handler(args, client)
            except _CliError:
                raise
            except httpx.HTTPError:
                raise
            except KeyboardInterrupt:
                raise
        if isinstance(result, _ExecResult):
            return result.exit_code
        if result is not None:
            _print_json(result)
        return EXIT_OK
    except BaseException as exc:
        return _handle_error(
            exc,
            verbose=getattr(args, "verbose", False),
            base_url=base_url,
        )


if __name__ == "__main__":  # pragma: no cover - module entry
    raise SystemExit(main())
