# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/skill-sync/*`` 独立 HTTP handler（Server / Client 分离部署技能同步）.

三个接口（diff / package / apply）统一要求 Bearer token：
- Server 配置 ``skill_sync.enabled``（默认 false）+ ``skill_sync.token``（非空）；
- 环境变量 ``JIUWENSKILL_SYNC_TOKEN`` 可覆盖配置 token，用于容器注入；
- 未显式启用或未配置 token 时一律返回 503 ``SKILL_SYNC_DISABLED``，
  禁止出现"空 token 放行"。

错误映射自持于本模块（不混入 ``_SKILL_HTTP_ERROR_STATUS``），保持接口独立，
将来统一路由体系时仅改注册层（见设计文档第 7/8 节）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from jiuwenswarm.server.runtime.skill.skill_manager import (
    ERROR_SKILL_ALREADY_EXISTS,
    ERROR_SKILL_BUILTIN_READ_ONLY,
    ERROR_SKILL_INVALID_METADATA,
    ERROR_SKILL_INVALID_PACKAGE,
    ERROR_SKILL_NOT_FOUND,
    ERROR_SKILL_OPERATION_UNSUPPORTED,
    ERROR_SKILL_RESERVED_PATH,
    ERROR_SKILL_UNSAFE_PATH,
    ERROR_SKILL_SYNC_CHECKSUM_ALGO_MISMATCH,
    ERROR_SKILL_SYNC_CHECKSUM_MISMATCH,
    ERROR_SKILL_SYNC_DISABLED,
    ERROR_SKILL_SYNC_EMPTY_PACKAGE,
    ERROR_SKILL_SYNC_FILE_TOO_LARGE,
    ERROR_SKILL_SYNC_INSTALL_FAILED,
    ERROR_SKILL_SYNC_INVALID_PAYLOAD,
    ERROR_SKILL_SYNC_PACKAGE_FAILED,
    ERROR_SKILL_SYNC_PACKAGE_TOO_LARGE,
    ERROR_SKILL_SYNC_UNAUTHORIZED,
    SkillManager,
    SkillRpcError,
)
from jiuwenswarm.server.runtime.skill.archive_store import ERROR_VERSION_NOT_FOUND
from jiuwenswarm.server.runtime.skill.skills_multipart_http import parse_multipart_form

logger = logging.getLogger(__name__)

# HTTP 通用请求体上限（diff / package JSON）
_SKILL_SYNC_MAX_JSON_BYTES = 1024 * 1024
# apply 上传配额（Content-Length 预检查，413）。须高于打包侧
# _SKILL_SYNC_MAX_ZIP_BYTES（50MiB）：multipart body = zip + boundary +
# 字段开销，若两值相等，接近 50MiB 的合法包永远回传不了
_SKILL_SYNC_MAX_UPLOAD_BYTES = 60 * 1024 * 1024
# 公开别名：供 app_web 路由层在读 body 前做 413 预检（勿访问下划线私有名）
SKILL_SYNC_MAX_UPLOAD_BYTES = _SKILL_SYNC_MAX_UPLOAD_BYTES
# diff / package JSON body 的 Content-Length 预检上限（与 _SKILL_SYNC_MAX_JSON_BYTES 同源）
SKILL_SYNC_MAX_JSON_BYTES = _SKILL_SYNC_MAX_JSON_BYTES

# 同步请求串行化锁：HTTP 线程各自 new SkillManager()，而 skills_state.json
# 是"读-改-写整份"模型（_save_state 原子写只能防半写，防不了丢失更新），
# 并发 apply / 构造期 _register_unmanaged_local_skills 会互相覆盖
# local_skills 记录。锁覆盖构造 + handler 执行；同步为低频批量操作，
# 串行化代价可接受。跨进程一致性（AgentServer 常驻实例）仍靠 reload_state。
_SKILL_SYNC_REQUEST_LOCK = threading.Lock()

_SKILL_SYNC_HTTP_ERROR_STATUS: dict[str, int] = {
    ERROR_SKILL_SYNC_DISABLED: 503,
    ERROR_SKILL_SYNC_UNAUTHORIZED: 401,
    ERROR_SKILL_SYNC_INVALID_PAYLOAD: 400,
    ERROR_SKILL_SYNC_CHECKSUM_MISMATCH: 400,
    ERROR_SKILL_SYNC_CHECKSUM_ALGO_MISMATCH: 400,
    ERROR_SKILL_SYNC_EMPTY_PACKAGE: 400,
    ERROR_SKILL_SYNC_FILE_TOO_LARGE: 413,
    ERROR_SKILL_SYNC_PACKAGE_TOO_LARGE: 400,
    ERROR_SKILL_SYNC_INSTALL_FAILED: 500,
    ERROR_SKILL_NOT_FOUND: 404,
    ERROR_VERSION_NOT_FOUND: 404,
    ERROR_SKILL_SYNC_PACKAGE_FAILED: 500,
    ERROR_SKILL_ALREADY_EXISTS: 409,
    "SKILL_IMPORT_OVERWRITE_REQUIRED": 409,
    ERROR_SKILL_OPERATION_UNSUPPORTED: 400,
    ERROR_SKILL_INVALID_PACKAGE: 400,
    ERROR_SKILL_UNSAFE_PATH: 400,
    ERROR_SKILL_INVALID_METADATA: 400,
    ERROR_SKILL_RESERVED_PATH: 400,
    "SKILL_FILE_TOO_LARGE": 400,
    ERROR_SKILL_BUILTIN_READ_ONLY: 403,
    "SKILL_VERSION_CONTENT_INVALID": 400,
}


def skill_sync_http_error_status(code: str) -> int:
    return _SKILL_SYNC_HTTP_ERROR_STATUS.get(code, 400)


def skill_sync_http_error_body(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message, "error": message}


def resolve_skill_sync_token() -> str:
    """读取同步 token：环境变量 ``JIUWENSKILL_SYNC_TOKEN`` 覆盖配置."""
    env_token = (os.getenv("JIUWENSKILL_SYNC_TOKEN") or "").strip()
    if env_token:
        return env_token
    try:
        from jiuwenswarm.common.config import get_config_raw

        section = (get_config_raw() or {}).get("skill_sync") or {}
        if isinstance(section, dict):
            return str(section.get("token") or "").strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def skill_sync_enabled() -> bool:
    """同步接口是否显式启用：配置 ``skill_sync.enabled=true`` 且 token 非空.

    设置了环境变量 ``JIUWENSKILL_SYNC_TOKEN`` 时直接视为启用（容器注入
    场景，此时配置里的 ``enabled`` 开关被覆盖）。
    """
    env_token = (os.getenv("JIUWENSKILL_SYNC_TOKEN") or "").strip()
    if env_token:
        return True
    try:
        from jiuwenswarm.common.config import get_config_raw

        section = (get_config_raw() or {}).get("skill_sync") or {}
        if not isinstance(section, dict):
            return False
        enabled = section.get("enabled")
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
        if enabled is not True:
            return False
        return bool(str(section.get("token") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _check_skill_sync_auth(authorization: str) -> None:
    """三接口统一鉴权：未启用 503；缺失/错误 Bearer token 401."""
    if not skill_sync_enabled():
        raise SkillRpcError(
            ERROR_SKILL_SYNC_DISABLED,
            "技能同步接口未启用（需配置 skill_sync.enabled 与 skill_sync.token）",
        )
    expected = resolve_skill_sync_token()
    header = str(authorization or "").strip()
    if not header.lower().startswith("bearer "):
        raise SkillRpcError(ERROR_SKILL_SYNC_UNAUTHORIZED, "缺少 Bearer token")
    provided = header[7:].strip()
    # 两侧先 encode 再比较：str 版 compare_digest 遇非 ASCII 直接抛
    # TypeError（未鉴权对端发非 ASCII 头即可打穿异常处理；配置了非
    # ASCII token 时更会导致接口整体不可用）
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise SkillRpcError(ERROR_SKILL_SYNC_UNAUTHORIZED, "Bearer token 无效")


def precheck_skill_sync_request(
    *, authorization: str, path: str, content_length: int | None
) -> None:
    """读 body 前的纯 header 级预检：鉴权 + Content-Length 上限.

    由 app_web 路由层在读请求体之前调用，保证未鉴权连接不能强制
    服务器读入任意大小的 body（apply 原有 413 预检统一并入此处）。
    """
    _check_skill_sync_auth(authorization)
    if content_length is None or content_length < 0:
        return
    limit = (
        _SKILL_SYNC_MAX_UPLOAD_BYTES
        if path == "/skill-sync/apply"
        else _SKILL_SYNC_MAX_JSON_BYTES
    )
    if content_length > limit:
        raise SkillRpcError(
            ERROR_SKILL_SYNC_FILE_TOO_LARGE,
            f"上传超过限制（{limit} 字节）",
        )


def _parse_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        raise SkillRpcError(ERROR_SKILL_SYNC_INVALID_PAYLOAD, "请求体不能为空")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillRpcError(ERROR_SKILL_SYNC_INVALID_PAYLOAD, "请求体不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise SkillRpcError(ERROR_SKILL_SYNC_INVALID_PAYLOAD, "请求体必须是 JSON 对象")
    return payload


def _run_manager_coro(coro: Any) -> Any:
    """在无事件循环的 HTTP 线程直接运行；已有循环时借助独立线程（复用既有做法）."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    import threading

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")


def _notify_agentserver_skills_landed(apply_result: Any) -> None:
    """apply 落盘后经 AgentServer WS 通知其重建 agent（进程间，与 file-api 同模式）.

    web server 与 AgentServer 是两个进程，进程内回调注册表互相不可见；
    这里复用 ``_call_agent_skill_rpc``（同 ``/file-api/skills/import`` 的
    跨进程通知路径）发送 ``skills.sync.reload``。通知失败只记录日志：
    技能已落盘，重建可由后续任意触发 agent 重建的事件兜底，apply 的
    返回结果不受影响。AgentServer 不可达（如未部署、纯 client 进程）时
    同样静默跳过。
    """
    if not isinstance(apply_result, dict):
        return
    applied = apply_result.get("applied")
    if not applied:
        return
    names = [
        str(item.get("name") or "").strip()
        for item in applied
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    if not names:
        return
    try:
        from jiuwenswarm.common.schema.message import ReqMethod
        from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
            _call_agent_skill_rpc,
        )

        _run_manager_coro(
            _call_agent_skill_rpc(
                method=ReqMethod.SKILL_SYNC_RELOAD,
                params={"applied": names},
                timeout_s=30.0,
            )
        )
        logger.info(
            "[skill_sync_http] notified AgentServer to reload after apply: %s",
            ", ".join(names),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[skill_sync_http] notify AgentServer reload failed (skills已落盘, "
            "等待下次agent重建生效): %s",
            exc,
        )


def handle_sync_diff_http(
    *,
    authorization: str,
    body: bytes,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /skill-sync/diff``，返回 (status, json_body)."""
    try:
        _check_skill_sync_auth(authorization)
        if len(body) > _SKILL_SYNC_MAX_JSON_BYTES:
            # 与 header 级 precheck_skill_sync_request 同条件同错误码
            # （413）；生产路径经预检拦截后不可达，仅防御性兜底
            raise SkillRpcError(
                ERROR_SKILL_SYNC_FILE_TOO_LARGE,
                f"请求体超过限制（{_SKILL_SYNC_MAX_JSON_BYTES} 字节）",
            )
        payload = _parse_json_body(body)
        with _SKILL_SYNC_REQUEST_LOCK:
            result = _run_manager_coro(SkillManager().handle_skill_sync_diff(payload))
        return 200, result
    except SkillRpcError as exc:
        return (
            skill_sync_http_error_status(exc.code),
            skill_sync_http_error_body(exc.code, exc.message),
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("[skill_sync_http] diff failed: %s", exc)
        return (
            500,
            skill_sync_http_error_body(ERROR_SKILL_SYNC_PACKAGE_FAILED, str(exc)),
        )


def handle_sync_package_http(
    *,
    authorization: str,
    body: bytes,
    send_file: Callable[[Path, str, str, int], None],
) -> tuple[int, dict[str, Any] | None]:
    """处理 ``POST /skill-sync/package``.

    整包落临时文件后计算 sha256，经 ``send_file(zip_path, filename, sha256,
    size)`` 回写响应头并流式发送 body（不可边打包边写，设计 5.3 实现注记）。
    打包阶段失败按 500 ``SKILL_SYNC_PACKAGE_FAILED`` 返回；``send_file``
    阶段的 IO 失败发生在响应头发出之后，无法再写 JSON 错误响应，仅记录
    日志（客户端按 sha256 校验失败自行重试）。
    """
    tmp_dir: Path | None = None
    try:
        _check_skill_sync_auth(authorization)
        if len(body) > _SKILL_SYNC_MAX_JSON_BYTES:
            # 与 header 级 precheck_skill_sync_request 同条件同错误码
            # （413）；生产路径经预检拦截后不可达，仅防御性兜底
            raise SkillRpcError(
                ERROR_SKILL_SYNC_FILE_TOO_LARGE,
                f"请求体超过限制（{_SKILL_SYNC_MAX_JSON_BYTES} 字节）",
            )
        payload = _parse_json_body(body)
        with _SKILL_SYNC_REQUEST_LOCK:
            result = _run_manager_coro(
                SkillManager().handle_skill_sync_package(payload)
            )
        zip_path = Path(str(result.get("zip_path") or ""))
        if not zip_path.name:
            # 缺 zip_path 的结果绝不能进入清理逻辑：Path("").parent 是
            # 当前目录，finally 的 rmtree 会误删进程工作目录
            raise SkillRpcError(
                ERROR_SKILL_SYNC_PACKAGE_FAILED,
                "打包结果缺少 zip_path",
            )
        tmp_dir = zip_path.parent
        try:
            send_file(
                zip_path,
                str(result.get("filename") or zip_path.name),
                str(result.get("sha256") or ""),
                int(result.get("size") or 0),
            )
        except (BrokenPipeError, ConnectionResetError):
            # 客户端中断下载：包已构建完成，仅记录，不向已断开连接写错误
            logger.info("[skill_sync_http] package download interrupted by client")
        except OSError as exc:
            # send_file 已发出 200 响应头（或连接已不可写），再写 JSON 错误
            # 响应会产出双状态行损坏报文；只记录，客户端按 sha256 校验失败重试
            logger.exception(
                "[skill_sync_http] package send failed after headers sent: %s", exc
            )
        return 200, None
    except SkillRpcError as exc:
        return (
            skill_sync_http_error_status(exc.code),
            skill_sync_http_error_body(exc.code, exc.message),
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("[skill_sync_http] package failed: %s", exc)
        return (
            500,
            skill_sync_http_error_body(ERROR_SKILL_SYNC_PACKAGE_FAILED, str(exc)),
        )
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _validate_apply_sha256_field(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise SkillRpcError(
            ERROR_SKILL_SYNC_INVALID_PAYLOAD,
            "sha256 必须为 64 位十六进制字符串",
        )
    return text


def handle_sync_apply_http(
    *,
    authorization: str,
    content_type: str,
    body: bytes,
    content_length: int | None = None,
) -> tuple[int, dict[str, Any]]:
    """处理 ``POST /skill-sync/apply``（multipart，三阶段处理链）.

    ``content_length`` 为请求头原始值（在读 body 前由调用方传入用于 413
    预检查）；``body`` 为已读取的完整请求体。安装结果（strict 预检失败的
    整单错误、best_effort 分组响应）原样透传给 HTTP 层。
    """
    try:
        _check_skill_sync_auth(authorization)
        # Content-Length 预检查在读 body 前完成（由调用方保证），此处兜底
        if content_length is not None and content_length > _SKILL_SYNC_MAX_UPLOAD_BYTES:
            raise SkillRpcError(
                ERROR_SKILL_SYNC_FILE_TOO_LARGE,
                f"上传超过限制（{_SKILL_SYNC_MAX_UPLOAD_BYTES} 字节）",
            )
        if len(body) > _SKILL_SYNC_MAX_UPLOAD_BYTES:
            raise SkillRpcError(
                ERROR_SKILL_SYNC_FILE_TOO_LARGE,
                f"上传超过限制（{_SKILL_SYNC_MAX_UPLOAD_BYTES} 字节）",
            )

        fields = parse_multipart_form(content_type, body)
        file_field = fields.get("file")
        if not isinstance(file_field, dict) or not isinstance(
            file_field.get("content"), (bytes, bytearray)
        ):
            raise SkillRpcError(ERROR_SKILL_SYNC_INVALID_PAYLOAD, "缺少 file 字段")
        content = bytes(file_field["content"])

        sha256_field = fields.get("sha256")
        if not isinstance(sha256_field, str):
            raise SkillRpcError(ERROR_SKILL_SYNC_INVALID_PAYLOAD, "缺少 sha256 字段")
        expected = _validate_apply_sha256_field(sha256_field)
        actual = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual, expected):
            raise SkillRpcError(
                ERROR_SKILL_SYNC_CHECKSUM_MISMATCH,
                "上传文件 sha256 校验失败",
            )

        overwrite_raw = fields.get("overwrite", "false")
        mode_raw = fields.get("mode", "strict")

        params = {
            "zip_bytes": content,
            "overwrite": overwrite_raw if isinstance(overwrite_raw, str) else bool(overwrite_raw),
            "mode": mode_raw if isinstance(mode_raw, str) else "strict",
            # origin 服务端强制固定：溯源标记不允许客户端表单改写
            "origin": "sync_client",
        }
        try:
            with _SKILL_SYNC_REQUEST_LOCK:
                result = _run_manager_coro(
                    SkillManager().handle_skill_sync_apply(params)
                )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            # SkillRpcError 不在此列，自然冒泡到外层 code→status 映射
            logger.exception("[skill_sync_http] apply manager failed: %s", exc)
            raise SkillRpcError(
                ERROR_SKILL_SYNC_INSTALL_FAILED, f"安装失败: {exc}"
            ) from exc
        _notify_agentserver_skills_landed(result)
        return 200, result
    except SkillRpcError as exc:
        return (
            skill_sync_http_error_status(exc.code),
            skill_sync_http_error_body(exc.code, exc.message),
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.exception("[skill_sync_http] apply failed: %s", exc)
        return (
            500,
            skill_sync_http_error_body(ERROR_SKILL_SYNC_INSTALL_FAILED, str(exc)),
        )
