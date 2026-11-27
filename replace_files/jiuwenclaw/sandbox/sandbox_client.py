# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""SandboxClient — 沙箱服务 HTTP 客户端.

封装沙箱 REST API，提供创建/删除/续期、执行命令/代码、上传/下载文件等能力。
所有路径段与 SandboxInsertDescription.md 中的表格严格一致，仅 api_base 可替换。

用法::

    config = SandboxConfig(api_base="https://sandbox.example.com", template_id="tpl-xxx")
    client = SandboxClient(config)
    sid = await client.create_sandbox()
    await client.upload_file(sid, "/skill/SKILL.md", b"# My Skill")
    content = await client.download_file(sid, "/skill/SKILL.md")
    await client.delete_sandbox(sid)
    await client.close()
"""

from __future__ import annotations

import os
import base64
import json
import tempfile
import logging
from typing import Any, Union, Dict, Optional

from dataclasses import dataclass, field
from pathlib import Path
import httpx

from jiuwenclaw.sandbox.auth import sha256, build_headers
from jiuwenclaw.log import interface_info

logger = logging.getLogger(__name__)

SANDBOX_HOME = "/home/sandbox"
SIDECAR_FILE_PATH = "proxy_policy.fifo"

_SANDBOX_DURATION_SECONDS = 3600
_SANDBOX_TIMEOUT_SECONDS = 120


# ------------------------------------------------------------------
# 自定义异常
# ------------------------------------------------------------------
class SandboxClientError(Exception):
    """沙箱客户端基础异常."""


class SandboxConnectionError(SandboxClientError):
    """网络连接错误（DNS、拒绝连接等）."""


class SandboxTimeoutError(SandboxClientError):
    """请求超时."""


class SandboxAPIError(SandboxClientError):
    """API 返回非 2xx 状态码."""

    def __init__(self, status_code: int, message: str, response_body: Optional[str] = None):
        self.status_code = status_code
        self.message = message
        self.response_body = response_body
        super().__init__(f"HTTP {status_code}: {message}")


class SandboxResponseError(SandboxClientError):
    """响应解析错误（缺少必需字段、格式错误）."""


def _interface_log_severity_for_status(status: int | None) -> str:
    if status is not None and status >= 500:
        return "ERROR"
    if status is not None and status >= 400:
        return "WARN"
    return "INFO"


def log_sandbox_manager_http_interface(
    *,
    method: str,
    url: str,
    interface_name: str = "",
    status: int | None = None,
    session_id: str = "",
) -> None:
    interface_info.log_interface_event(
        severity=_interface_log_severity_for_status(status),
        source="SkillCreator",
        destination="SandboxManager",
        interface_type="HTTP",
        interface_name=interface_name,
        http_url=f"{str(method or '').upper()} {url}",
        http_status=status,
    )


@dataclass
class SandboxConfig:
    """沙箱服务配置.

    Attributes:
        api_base: 沙箱服务地址（协议+域名，不含尾斜杠），如 ``https://sandbox.example.com``
        template_id: 沙箱模板 ID，用于 ``x-sandbox-template-id`` 请求头
        duration_seconds: 沙箱存活时长（秒），创建/续期时使用
        timeout_seconds: HTTP 请求超时（秒）
        metadata: 创建沙箱时传入的 metadata（teamid/userid 等）
        command_timeout_seconds: 执行命令默认超时（秒）
        code_timeout_seconds: 执行代码默认超时（秒）
    """

    api_base: str
    template_id: str
    duration_seconds: int = _SANDBOX_DURATION_SECONDS
    timeout_seconds: int = _SANDBOX_TIMEOUT_SECONDS
    metadata: dict[str, str] = field(default_factory=dict)
    command_timeout_seconds: int = 60
    code_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> SandboxConfig:
        api_base = os.environ.get("SANDBOX_API_BASE", "").strip()
        if not api_base:
            raise RuntimeError(
                "SANDBOX_API_BASE environment variable is required when sandbox routing is enabled"
            )
        template_id = os.environ.get("SANDBOX_TEMPLATE_ID", "").strip()
        if not template_id:
            raise RuntimeError(
                "SANDBOX_TEMPLATE_ID environment variable is required when sandbox routing is enabled"
            )
        return cls(
            api_base=api_base,
            template_id=template_id,
            duration_seconds=_SANDBOX_DURATION_SECONDS,
            timeout_seconds=_SANDBOX_TIMEOUT_SECONDS,
            metadata={},
        )


@dataclass
class ExecutionResult:
    """执行结果数据类"""
    success: bool = False
    output: str = ""
    error: Optional[str] = None
    execution_time_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "execution_time_ms": self.execution_time_ms
        }


# TODO: 增强代码的健壮性，如异常捕获、重试机制等等
class SandboxClient:
    """沙箱服务 HTTP 客户端.

    每个实例持有一个 ``httpx.AsyncClient``，用完后需调用 :meth:`close` 释放资源。
    """

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._api_base = config.api_base.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def get_config(self):
        return self._config

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def create_sandbox(self) -> ExecutionResult:
        """创建沙箱，返回 ``sandboxId``."""
        uri = "/sandbox/v1/lifecycle/sandboxes"
        url = f"{self._api_base}{uri}"

        body: dict[str, Any] = {
            "templateId": self._config.template_id,
            "duration": {"durationInSeconds": self._config.duration_seconds}
        }
        logger.info(f"[SandboxClient] 开始创建沙箱...")
        client = await self._get_client(uri, body)
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        sandbox_id: str = data["sandboxId"]
        logger.info(f"[SandboxClient] [{sandbox_id}] 沙箱已创建")
        log_sandbox_manager_http_interface(
            method="POST",
            url=uri,
            interface_name="SandboxCreate",
            status=resp.status_code,
        )
        return ExecutionResult(
            success=True,
            output=sandbox_id
        )

    async def delete_sandbox(self, sandbox_id: str) -> ExecutionResult:
        """删除沙箱."""
        uri = f"/sandbox/v1/lifecycle/sandboxes/{sandbox_id}"
        url = f"{self._api_base}{uri}"

        logger.info(f"[SandboxClient] [{sandbox_id}] 开始删除沙箱...")
        client = await self._get_client(uri, {}, 'DELETE')
        resp = await client.delete(url)

        # 404表示沙箱已不存在，视为成功
        if resp.status_code == 404:
            logger.info(f"[SandboxClient] [{sandbox_id}] 沙箱已不存在")
        else:
            resp.raise_for_status()
        logger.info(f"[SandboxClient] [{sandbox_id}] 沙箱已删除")
        log_sandbox_manager_http_interface(
            method="DELETE",
            url=uri,
            interface_name="SandboxDelete",
            status=resp.status_code,
        )
        return ExecutionResult(success=True)

    async def refresh_duration(
            self, sandbox_id: str, duration_seconds: int | None = None
    ) -> ExecutionResult:
        """续期沙箱."""
        uri = f"/sandbox/v1/lifecycle/sandboxes/{sandbox_id}/refreshes"
        url = f"{self._api_base}{uri}"

        body = {
            "durationInSeconds": duration_seconds or self._config.duration_seconds,
        }

        logger.info(f"[SandboxClient] [{sandbox_id}] 开始续期沙箱...")
        client = await self._get_client(uri, body)
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        logger.debug("[SandboxClient] [%s] 沙箱已续期:  (%ds)", sandbox_id, body["durationInSeconds"])
        log_sandbox_manager_http_interface(
            method="POST",
            url=uri,
            interface_name="SandboxRefresh",
            status=resp.status_code,
        )
        return ExecutionResult(success=True)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def exec_command(
            self,
            sandbox_id: str,
            command: str,
            timeout_seconds: int | None = None,
    ) -> ExecutionResult:
        """在沙箱中执行 shell 命令.

        Returns:
            ``{"exitStatus": int, "stdout": str, "stderr": str, "executionTimeInMilliseconds": int}``
        """
        uri = f"/sandbox/v1/proxy/sandboxes/{sandbox_id}/command-executions/"
        url = f"{self._api_base}{uri}"

        body = {
            "command": command,
            "timeoutInSeconds": timeout_seconds or self._config.command_timeout_seconds,
        }

        logger.info(f"[SandboxClient] [{sandbox_id}] 开始执行命令 {command[:100]}...")
        client = await self._get_client(uri, body)
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        logger.info(f"[SandboxClient] [{sandbox_id}] 命令执行结束")
        result_data = resp.json()
        return ExecutionResult(
            success=result_data.get("exitStatus") == 0,
            output=result_data.get("stdout"),
            error=result_data.get("stderr"),
            execution_time_ms=result_data.get("executionTimeInMilliseconds")
        )

    async def exec_code(
            self,
            sandbox_id: str,
            code: str,
            language: str = "python",
            timeout_seconds: int | None = None,
    ) -> ExecutionResult:
        """在沙箱中执行代码.

        Returns:
            ``{"exitStatus": int, "stdout": str, "stderr": str, "executionTimeInMilliseconds": int}``
        """
        uri = f"/sandbox/v1/proxy/sandboxes/{sandbox_id}/code-executions/"
        url = f"{self._api_base}{uri}"

        body = {
            "code": code,
            "language": language,
            "timeoutInSeconds": timeout_seconds or self._config.code_timeout_seconds,
        }

        logger.info(f"[SandboxClient] [{sandbox_id}] 开始执行代码 {code[:100]}...")
        client = await self._get_client(uri, body)
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        logger.info(f"[SandboxClient] [{sandbox_id}] 代码执行完成")
        result_data = resp.json()
        return ExecutionResult(
            success=result_data.get("exitStatus") == 0,
            output=result_data.get("stdout"),
            error=result_data.get("stderr"),
            execution_time_ms=result_data.get("executionTimeInMilliseconds")
        )

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    async def upload_file(
            self, local_path: str,
            remote_path: str,
            sandbox_id: str,
            sidecar: bool = False,
            **kwargs
    ) -> ExecutionResult:
        """上传文件到沙箱.

        Args:
            local_path: 上传文件路径
            remote_path: 沙箱中目标路径
            sandbox_id: 沙箱 ID
            sidecar: 是否上传到伴生容器

        Returns:
            ``{"name": str, "lastModifiedAt": str}``
        """
        uri = f"/sandbox/v1/proxy/sandboxes/{sandbox_id}/files"
        url = f"{self._api_base}{uri}"

        logger.info(f"[SandboxClient] [{sandbox_id}] 开始上传文件...")
        # 获取上传的文件名
        file_name = os.path.basename(local_path)
        logger.info(f"[SandboxClient] [{sandbox_id}] 文件名: {file_name}")

        # 以二进制模式读取文件
        with open(local_path, 'rb') as file:
            file_content = file.read()
        logger.info(f"[SandboxClient] [{sandbox_id}] 二进制文件大小: {len(file_content)} bytes")

        # 内容编码
        content_hex = sha256(file_content)

        # 记录沙箱中的文件地址
        logger.info(f"[SandboxClient] [{sandbox_id}] 沙箱中目标路径: {remote_path}")

        # 构建多部分表单数据
        files = {
            'file': (file_name, file_content, 'application/octet-stream')
        }
        data = {
            'fileName': remote_path
        }

        # 构造body
        body = f"file={content_hex}fileName={remote_path}"

        client = await self._get_client(uri, body, sidecar=sidecar)
        resp = await client.post(url, files=files, data=data)
        resp.raise_for_status()
        logger.info(f"[SandboxClient] [{sandbox_id}] 文件上传成功: {remote_path}")
        log_sandbox_manager_http_interface(
            method="POST",
            url=uri,
            interface_name="SandboxUpload",
            status=resp.status_code,
        )
        return ExecutionResult(success=True)

    async def download_file(self, remote_path: str, sandbox_id: str) -> ExecutionResult:
        """从沙箱下载文件.

        Args:
            remote_path: 沙箱中源文件路径
            sandbox_id: 沙箱 ID

        Returns:
            文件内容（str）
        """
        uri = f"/sandbox/v1/proxy/sandboxes/{sandbox_id}/files"
        url = f"{self._api_base}{uri}"

        logger.info(f"[SandboxClient] [{sandbox_id}] 开始下载文件 {remote_path}...")
        # 构造查询参数
        params = f"fileName={remote_path}"
        # 记录沙箱中的文件地址
        logger.info(f"[SandboxClient] [{sandbox_id}] 沙箱中源文件路径: {remote_path}")

        client = await self._get_client(uri, {}, 'GET', params)
        resp = await client.get(url, params=params)

        # 获取文件内容（二进制数据）
        content = resp.content

        # 将二进制内容转换为base64编码的字符串，避免UnicodeDecodeError
        # 这样可以保证任何类型的文件都能被正确处理
        if content:
            output = base64.b64encode(content).decode('ascii')
        else:
            output = ""

        logger.info(f"[SandboxClient] [{sandbox_id}] 文件下载成功")
        return ExecutionResult(success=True, output=output)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def list_sandbox_files(self, sandbox_id: str, root: str = ".") -> list[str]:
        """列出沙箱内的文件（通过 find 命令）.

        Returns:
            相对于 ``/home/sandbox`` 的文件路径列表
        """
        search_dir = f"{SANDBOX_HOME}/{root.lstrip('/')}" if root != "." else SANDBOX_HOME
        result = await self.exec_command(
            sandbox_id,
            f"find {search_dir} -type f 2>/dev/null",
        )
        stdout = result.get("stdout", "")
        prefix = f"{SANDBOX_HOME}/"
        files: list[str] = []
        for line in stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith(prefix):
                line = line[len(prefix):]
            files.append(line)
        return files

    async def close(self) -> None:
        """关闭 HTTP 客户端."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_client(
            self, uri: str,
            payload: Union[Dict, str],
            method: str = 'POST',
            query: str = '',
            sidecar: bool = False
    ) -> httpx.AsyncClient:
        """提供统一获取HTTP客户端的方式"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                    keepalive_expiry=30.0
                ),
                trust_env=False,
            )
        headers = build_headers(payload, uri, method, query, sidecar)
        # 为当前客户端设置默认请求头
        self._client.headers = headers
        return self._client

    async def upload_apikey_to_sidecar(
            self,
            sandbox_id: str,
            apikey: str,
            user_id: str
    ) -> ExecutionResult:
        remote_path = SIDECAR_FILE_PATH
        content = {
            "notes": "配置文件以host作为资源匹配的标识，精确匹配。"
                     "action定义1直接使用key字段中的值直接覆盖header字段中的消息头。"
                     "原来是http协议修改为https，原来是ws协议修改为wss",
            "version": "1.0",
            "proxys": [
                {
                    "host": os.environ.get("AGENTSERVER_TO_UA_HOST"),
                    "actions": [
                        {
                            "action": "1",
                            "header": "x-api-key",
                            "key": apikey
                        }
                    ]
                }
            ]
        }
        if user_id:
            content["proxys"][0]["actions"].append({
                "action": "1",
                "header": "x-uid",
                "key": user_id
                })
        if os.environ.get("AGENTSERVER_TO_OA_HOST") != os.environ.get("AGENTSERVER_TO_UA_HOST"):
            content["proxys"].append({
                "host": os.environ.get("AGENTSERVER_TO_OA_HOST"),
                "actions": [
                    {
                        "action": "1",
                        "header": "x-api-key",
                        "key": apikey
                    }
                ]
            })
        content_json = json.dumps(content, ensure_ascii=False)
        local_path: str | None = None
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".fifo",
            delete=False,
        ) as handle:
            handle.write(content_json)
            local_path = handle.name
        result = await self.upload_file(
            local_path,
            remote_path,
            sandbox_id,
            sidecar=True,
        )
        if not result.success:
            raise RuntimeError(result.error or "upload_file failed for sidecar config")
        logger.info(
            "Uploaded sidecar config for sandbox_id=%s remote_path=%s",
            sandbox_id,
            remote_path,
        )
        if local_path:
            try:
                Path(local_path).unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove temporary sidecar config file: %s", local_path)
        return result
