# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extension package commands shared by Python and frozen desktop entry points."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, AsyncIterator
from zipfile import BadZipFile, ZipFile

from jiuwenswarm.common import extension_cli as control

PACKAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
UNCERTAIN_CODES = frozenset({
    "REQUEST_TIMEOUT", "TRANSPORT_ERROR", "INVALID_RESPONSE", "AGENT_SERVER_TIMEOUT",
    "WS_DISCONNECTED", "WS_CLOSED", "REQUEST_ABORTED",
})


class ClientError(Exception):
    """An actionable client or service error, without connection credentials."""

    def __init__(self, code: str, message: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}

    @property
    def uncertain(self) -> bool:
        return self.code in UNCERTAIN_CODES or self.payload.get("outcome") == "unknown"


def valid_id(value: str) -> str:
    if not PACKAGE_ID.fullmatch(value) or ".." in value:
        raise ClientError("INVALID_INPUT", "package_id 格式不合法，请核对 manifest 中的包 ID。")
    return value


@dataclass(frozen=True)
class Settings:
    context: dict[str, Any]
    timeout: float

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Settings:
        if args.timeout is None:
            from jiuwenswarm.extensions_v2.config import resolve_install_report_settings
            try:
                args.timeout = resolve_install_report_settings().dependency_timeout + 30
            except ValueError as error:
                raise ClientError("INVALID_CONFIG", str(error)) from None
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ClientError("INVALID_CONFIG", "--timeout 必须是大于零的有限秒数。")
        try:
            context = control.read_context("gateway" if args.domain == "gateway" else "agent_server")
        except control.ControlError as error:
            raise ClientError("HOST_UNAVAILABLE", str(error)) from None
        return cls(context, args.timeout)


class LocalClient:
    """One RPC per connection, with no automatic mutation retries."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def request(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await control.request(self.settings.context, action, params, self.settings.timeout)
        except TimeoutError:
            raise ClientError("REQUEST_TIMEOUT", "等待宿主响应超时；写操作可能仍在执行。") from None
        except control.ControlError:
            raise ClientError("INVALID_RESPONSE", "宿主响应未通过校验。") from None
        except (ValueError, UnicodeError):
            raise ClientError("INVALID_RESPONSE", "宿主返回了无法解析的消息。") from None
        except OSError:
            raise ClientError("TRANSPORT_ERROR", "当前实例的本机管理连接不可用。") from None
        if type(response.get("ok")) is not bool or not isinstance(response.get("payload"), dict):
            raise ClientError("INVALID_RESPONSE", "宿主响应缺少明确的状态和 payload。")
        payload = response["payload"]
        if not response["ok"]:
            code = str(payload.get("code") or "REQUEST_FAILED")
            message = str(payload.get("error") or "包管理请求失败。")
            raise ClientError(code, message, payload)
        return payload


@asynccontextmanager
async def open_client(settings: Settings) -> AsyncIterator[LocalClient]:
    yield LocalClient(settings)


def inspect_zip(value: str) -> dict[str, str]:
    """Read identity and hash from the final ZIP, without importing package code."""
    source = Path(value).expanduser().resolve()
    if source.suffix.lower() != ".zip" or not source.is_file():
        raise ClientError("INVALID_INPUT", "install 需要一个已存在的 ZIP 文件。")
    try:
        with ZipFile(source) as archive:
            if archive.namelist().count("manifest.json") != 1:
                raise ValueError("ZIP 根目录必须恰好包含一份 manifest.json。")
            entry = archive.getinfo("manifest.json")
            if entry.file_size > 64 * 1024:
                raise ValueError("manifest.json 超过 64 KiB，请检查包内容。")
            manifest = json.loads(archive.read(entry))
        package = manifest.get("package") if isinstance(manifest, dict) else None
        if not isinstance(package, dict) or not isinstance(package.get("id"), str):
            raise ValueError("manifest 缺少 package.id。")
        package_id = valid_id(package["id"])
        version = package.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("manifest 中的 package.version 必须是非空字符串。")
    except ClientError:
        raise
    except (BadZipFile, OSError, ValueError, RuntimeError, UnicodeError) as error:
        raise ClientError("INVALID_ZIP", str(error)) from None
    try:
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise ClientError("INVALID_INPUT", "无法读取最终 ZIP，请核对文件权限。") from None
    domain = package.get("domain")
    if domain not in ("gateway", "agentserver"):
        raise ClientError("INVALID_ZIP", "manifest 的 package.domain 必须是 gateway 或 agentserver。")
    return {"package_id": package_id, "version": version, "zip_path": str(source),
            "zip_sha256": digest, "domain": domain}


def read_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state")
    if not isinstance(state, dict) or not isinstance(state.get("active"), list):
        raise ClientError("INVALID_RESPONSE", "查询响应缺少 active 状态，不能判断包是否生效。")
    if not isinstance(state.get("installed"), list) or any(not isinstance(item, dict) for item in state["active"]):
        raise ClientError("INVALID_RESPONSE", "查询响应中的包状态不完整。")
    if not isinstance(state.get("installed_packages", []), list):
        raise ClientError("INVALID_RESPONSE", "查询响应中的安装副本记录不合法。")
    return state


def active_record(state: dict[str, Any], package_id: str) -> dict[str, Any] | None:
    records = [item for item in state["active"] if item.get("package_id") == package_id]
    if len(records) > 1:
        raise ClientError("INVALID_RESPONSE", "同一包出现多条激活记录，无法确认目标实例。")
    return records[0] if records else None


def has_installed_copy(state: dict[str, Any], package_id: str) -> bool:
    return any(isinstance(item, dict) and item.get("package_id") == package_id
               for item in state.get("installed_packages", [])) or bool(state["installed"])


def read_report(payload: dict[str, Any], action: str, package_id: str) -> dict[str, Any]:
    reports = payload.get("reports")
    if not isinstance(reports, list) or len(reports) != 1 or not isinstance(reports[0], dict):
        raise ClientError("INVALID_RESPONSE", "单包操作没有返回一份明确的安装或管理报告。")
    report = reports[0]
    if report.get("package_id") != package_id or report.get("status") not in {"SUCCESS", "FAILED"}:
        raise ClientError("INVALID_RESPONSE", "管理报告的 package_id 或 status 不符合本次请求。")
    if report.get("operation") != action:
        raise ClientError("INVALID_RESPONSE", "管理报告的操作类型与本次请求不一致。")
    return report


def verify_state(
    state: dict[str, Any], action: str, package_id: str, version: str | None, report: dict[str, Any],
) -> bool:
    record = active_record(state, package_id)
    if action == "uninstall":
        return record is None and not has_installed_copy(state, package_id)
    generation = report.get("current_generation")
    return bool(
        record and type(generation) is int and type(record.get("generation")) is int
        and record.get("generation") == generation
        and record.get("version") == version and isinstance(record.get("root"), str) and record["root"]
    )


async def recover_state(settings: Settings, package_id: str, result: dict[str, Any]) -> None:
    # Query once; current activation does not prove the original operation succeeded.
    try:
        async with open_client(settings) as client:
            result["state"] = read_state(await client.request("query", {"package_id": package_id}))
    except ClientError as error:
        result["query_error"] = error.as_dict()


async def perform(args: argparse.Namespace, settings: Settings, result: dict[str, Any]) -> int:
    action = args.action
    package_id = result.get("package_id")
    query_params = {"package_id": package_id} if package_id else {}
    async with open_client(settings) as client:
        before = read_state(await client.request("query", query_params))
        result["before_state"] = before
        if action == "query":
            result["state"] = before
            result.update(ok=True, status="SUCCESS")
            return 0
        record = active_record(before, package_id)
        if action == "install" and (record or has_installed_copy(before, package_id)):
            raise ClientError("PACKAGE_ALREADY_PRESENT", "已有同 ID 包或安装副本；请先确定更新方式，不会自动卸载或覆盖。")
        if action == "reload" and record is None:
            raise ClientError("PACKAGE_NOT_ACTIVE", "包尚未激活，不能执行热重载。")
        if action == "uninstall" and record is None and not has_installed_copy(before, package_id):
            raise ClientError("PACKAGE_NOT_FOUND", "没有找到目标包或安装副本。")
        version = result.get("version") if action == "install" else (record or {}).get("version")
        params = {"paths": [result["server_path"]]} if action == "install" else {"package_id": package_id}
        try:
            payload = await client.request(action, params)
            if "reports" in payload:
                result["reports"] = payload["reports"]
            if "ui_notified" in payload:
                result["ui_notified"] = payload["ui_notified"]
            report = read_report(payload, action, package_id)
        except ClientError as error:
            result["error"] = error.as_dict()
            if not error.uncertain:
                result.update(status="FAILED", operation_status="FAILED")
                return 1
            result.update(status="UNKNOWN", operation_status="UNKNOWN", verification_status="UNKNOWN")
            await recover_state(settings, package_id, result)
            return 3
        result["reports"] = payload["reports"]
        result["operation_status"] = report["status"]
        if report["status"] == "FAILED":
            result.update(status="FAILED", error=report.get("error") or {"code": "OPERATION_FAILED"})
            return 1
        # Verify the active version and generation after receiving a successful report.
        try:
            state = read_state(await client.request("query", query_params))
            result["state"] = state
            verified = verify_state(state, action, package_id, version, report)
        except ClientError as error:
            result.update(status="UNKNOWN", verification_status="UNKNOWN", error=error.as_dict())
            return 3
        result["verification_status"] = "SUCCESS" if verified else "FAILED"
        if not verified:
            result.update(status="FAILED", error={"code": "STATE_MISMATCH", "message": "报告成功，但当前包状态未通过核对。"})
            return 1
        current = active_record(state, package_id)
        result.update(ok=True, status="SUCCESS", active=current is not None)
        if current:
            result.update(version=current["version"], install_path=current["root"])
        return 0


async def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {"ok": False, "operation": args.action, "status": "FAILED",
                              "verification_status": "not_run", "business_verification": "not_run"}
    try:
        if args.action == "install":
            if not args.target:
                raise ClientError("INVALID_INPUT", "install 需要 ZIP 路径。")
            result.update(inspect_zip(args.target))
            if args.domain and args.domain != result["domain"]:
                raise ClientError("DOMAIN_MISMATCH", "--domain 与最终 ZIP 的 manifest 归属不一致。")
            args.domain = result["domain"]
            server_path = args.server_path or result["zip_path"]
            if not (PurePosixPath(server_path).is_absolute() or PureWindowsPath(server_path).is_absolute()):
                raise ClientError("INVALID_INPUT", "--server-path 必须是运行服务可读取的 ZIP 绝对路径。")
            if not server_path.lower().endswith(".zip"):
                raise ClientError("INVALID_INPUT", "--server-path 必须指向 ZIP 文件。")
            result["server_path"] = server_path
        else:
            if args.server_path:
                raise ClientError("INVALID_INPUT", "--server-path 只用于 install。")
            if args.target:
                result["package_id"] = valid_id(args.target)
            elif args.action != "query":
                raise ClientError("INVALID_INPUT", "reload 和 uninstall 需要 package_id。")
        result["domain"] = args.domain or "agentserver"
        settings = Settings.from_args(args)
        return result, await perform(args, settings, result)
    except ClientError as error:
        result["error"] = error.as_dict()
        return result, 2


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ClientError("INVALID_INPUT", f"命令参数不正确：{message}")


def parser() -> argparse.ArgumentParser:
    command = JsonArgumentParser(description="管理当前运行实例中的扩展包；无需提供 Gateway 地址或浏览器登录凭据。")
    command.add_argument("action", choices=("install", "query", "reload", "uninstall"))
    command.add_argument("target", nargs="?", help="install 传最终 ZIP；其他操作传 package_id，query 可省略")
    command.add_argument("--domain", choices=("agentserver", "gateway"), help="install 从 package.domain 确定；其他操作默认 agentserver")
    command.add_argument("--json", action="store_true", help="使用结构化 JSON 输出（默认也是 JSON）")
    command.add_argument("--server-path", help="宿主可读取的 ZIP 绝对路径；默认使用最终 ZIP 路径，不负责上传")
    command.add_argument("--timeout", type=float, default=None, help="每次请求的超时秒数，默认配置的依赖等待时间加 30 秒")
    return command


def main(argv: list[str] | None = None) -> int:
    # Preserve Chinese paths in captured output on every platform.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args(argv)
        result, code = asyncio.run(execute(args))
    except ClientError as error:
        result = {"ok": False, "status": "FAILED", "error": error.as_dict()}
        code = 2
    except KeyboardInterrupt:
        result = {"ok": False, "status": "UNKNOWN", "error": {"code": "INTERRUPTED",
                  "message": "客户端已中断；如已发送写请求，结果需另行查询。"}}
        code = 3
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
