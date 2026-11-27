# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Utilities for xiaoyi handset tools.

提供设备侧工具的通用功能：
- 获取 channel 实例
- 发送 command 并等待响应
- 参数验证
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from jiuwenclaw.utils import logger
from jiuwenclaw.channel.xiaoyi_channel import get_xiaoyi_channel
from jiuwenclaw.config import get_config


def _is_data_event_status_success(status: Any) -> bool:
    """设备 data-event 的 status 是否为成功（兼容大小写及部分别名）."""
    if status is True:
        return True
    if status is None or status is False:
        return False
    s = str(status).strip().lower()
    return s in ("success", "succeed", "successful", "ok")


def _outputs_top_level_code_ok(code: Any) -> bool:
    """outputs.code 表示成功或未携带错误码（None 视为不按 code 判失败）."""
    if code is None:
        return True
    if isinstance(code, bool):
        return bool(code)
    try:
        if isinstance(code, (int, float)) and int(code) == 0:
            return True
    except (TypeError, ValueError):
        pass
    return str(code).strip() == "0"


class ToolInputError(Exception):
    """工具输入参数错误.

    抛出此错误会让框架返回 HTTP 400 而非 500，
    LLM 会将其识别为参数错误而非瞬时故障。
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.status = 400


async def execute_device_command(
    intent_name: str,
    command: Dict[str, Any],
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """执行设备命令并等待响应.

    Args:
        intent_name: Intent 名称，用于匹配响应
        command: Command 数据结构
        timeout: 超时时间（秒）

    Returns:
        包含 content 字段的响应字典

    Raises:
        RuntimeError: 会话不存在或执行失败
        ToolInputError: 参数错误
    """
    logger.info(f"[{intent_name}_TOOL] Starting execution")

    # 获取 XiaoyiChannel 实例
    channel = get_xiaoyi_channel()
    if channel is None:
        logger.error(f"[{intent_name}_TOOL] FAILED: No active session found!")
        raise RuntimeError(
            f"No active XY session found. {intent_name} tool can only be used during an active conversation."
        )

    # 从 config 读取 xiaoyi 通道的会话与任务标识
    session_id = ""
    task_id = ""
    message_id = f"cmd_{int(asyncio.get_event_loop().time() * 1000)}"

    try:
        config = get_config()
        xiaoyi_conf = config.get("channels", {}).get("xiaoyi", {})
        session_id = xiaoyi_conf.get("last_session_id", "")
        task_id = xiaoyi_conf.get("last_task_id", "")
    except Exception as e:
        logger.warning(f"[{intent_name}_TOOL] 获取会话信息失败: {e}")

    if not session_id:
        logger.error(f"[{intent_name}_TOOL] FAILED: No valid session found!")
        raise RuntimeError(
            f"No active XY session found. {intent_name} tool can only be used during an active conversation."
        )

    logger.info(
        f"[{intent_name}_TOOL] Session context: session_id={session_id!r} "
        f"task_id={task_id!r} message_id={message_id!r}"
    )

    # 创建事件等待结果
    result_event = asyncio.Event()
    result_data: Optional[Dict[str, Any]] = None
    error_result: Optional[Exception] = None

    # 定义 data-event 处理器
    def on_data_event(event):
        nonlocal result_data, error_result
        logger.info(
            f"[{intent_name}_TOOL] Received data event: intent={event.intent_name}, "
            f"status={event.status}"
        )

        if event.intent_name == intent_name:
            logger.info(f"[{intent_name}_TOOL] Intent name matched! status={event.status}")

            # 设备在无短信等场景常返回 outputs: {}，此处必须用「outputs is not None」判断。
            if _is_data_event_status_success(event.status):
                if event.outputs is None:
                    error_result = RuntimeError(
                        "执行失败: status=success 但 outputs 为 null"
                    )
                    logger.error(
                        f"[{intent_name}_TOOL] success 但 outputs 为 null"
                    )
                else:
                    result_data = event.outputs
                    keys = (
                        list(event.outputs.keys())
                        if isinstance(event.outputs, dict)
                        else []
                    )
                    logger.info(
                        f"[{intent_name}_TOOL] Execution successful, outputs keys={keys}"
                    )
            else:
                error_result = RuntimeError(f"执行失败: {event.status}")
                out_preview = ""
                if isinstance(event.outputs, dict):
                    try:
                        out_preview = json.dumps(
                            event.outputs, ensure_ascii=False
                        )[:600]
                    except Exception:
                        out_preview = str(event.outputs)[:600]
                logger.error(
                    f"[{intent_name}_TOOL] Execution failed: status={event.status!r} "
                    f"outputs_preview={out_preview}"
                )

            result_event.set()
        else:
            logger.debug(
                f"[{intent_name}_TOOL] Intent name mismatch: expected={intent_name}, "
                f"got={event.intent_name}"
            )

    # 注册处理器
    channel.register_data_event_handler(intent_name, on_data_event)

    try:
        # 发送命令
        logger.info(f"[{intent_name}_TOOL] Sending command...")
        sent = await channel.send_xiaoyi_phone_tools_command(
            session_id=session_id,
            task_id=task_id or session_id,
            message_id=message_id,
            command=command,
        )

        if not sent:
            raise RuntimeError("发送指令失败，WebSocket 未连接")

        # 等待响应
        logger.info(f"[{intent_name}_TOOL] Waiting for response (timeout: {timeout}s)...")
        await asyncio.wait_for(result_event.wait(), timeout=timeout)

        if error_result:
            logger.info(f"[{intent_name}_TOOL] Response error_result = {error_result}")
            raise error_result

        logger.info(f"[{intent_name}_TOOL] Response result_event = {result_event}")
        logger.info(f"[{intent_name}_TOOL] Response result_data = {result_data}")

        # 成功时 outputs 可能为 {}，勿用「or」短路（空 dict 在 Python 中为假）
        return {} if result_data is None else result_data

    except asyncio.TimeoutError as e:
        logger.error(
            f"[{intent_name}_TOOL] Timeout: no response within {timeout}s"
        )
        raise RuntimeError(
            f"设备命令超时（{timeout}s 内未收到 {intent_name} 响应）"
        ) from e

    finally:
        channel.unregister_data_event_handler(intent_name, on_data_event)


def raise_if_device_error(outputs: Any, what_failed: str) -> None:
    """若设备 outputs 含失败 code 或 retErrCode，抛出 RuntimeError.

    部分 Intent 使用 code，部分使用 retErrCode（字符串 \"0\" 表示成功）。
    """
    if not isinstance(outputs, dict):
        return
    code = outputs.get("code")
    if not _outputs_top_level_code_ok(code):
        error_msg = outputs.get("errorMsg") or outputs.get("errMsg") or "未知错误"
        raise RuntimeError(
            f"{what_failed}: {error_msg} (错误代码: {code})"
        ) from None
    ret = outputs.get("retErrCode")
    if ret is not None and str(ret) != "0":
        err_msg = outputs.get("errMsg", "未知错误")
        raise RuntimeError(
            f"{what_failed}: {err_msg} (retErrCode: {ret})"
        ) from None


def validate_required_params(params: Dict[str, Any], required: list[str]) -> None:
    """验证必填参数.

    Args:
        params: 参数字典
        required: 必填参数名列表

    Raises:
        ToolInputError: 缺少必填参数
    """
    for param_name in required:
        value = params.get(param_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ToolInputError(f"缺少必填参数 {param_name}")


def format_success_response(data: Dict[str, Any], message: str = "") -> Dict[str, Any]:
    """格式化成功响应.

    Args:
        data: 响应数据
        message: 可选的消息

    Returns:
        包含 content 的响应字典
    """

    response = {"success": True, **data}
    if message:
        response["message"] = message

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(response, ensure_ascii=False),
            }
        ]
    }


def format_error_response(error: str) -> Dict[str, Any]:
    """格式化错误响应.

    Args:
        error: 错误信息

    Returns:
        包含 content 的错误响应字典
    """

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"success": False, "error": error}, ensure_ascii=False),
            }
        ]
    }
