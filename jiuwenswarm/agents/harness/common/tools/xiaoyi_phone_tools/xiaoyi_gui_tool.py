# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""小艺 GUI 自动化（xiaoyi_gui_agent）：通过 InvokeJarvisGUIAgent 与设备协同完成屏幕操作."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import get_xiaoyi_channel

from .utils import ToolInputError, format_success_response


def _get_gui_tool_async_lock(channel: Any) -> asyncio.Lock:
    gl = getattr(channel, "gui_tool_lock", None)
    if gl is not None:
        return gl
    inner = getattr(channel, "_gui_tool_lock", None)
    if inner is None:
        inner = asyncio.Lock()
        setattr(channel, "_gui_tool_lock", inner)
    return inner


def _payload_is_gui_final(payload: Dict[str, Any]) -> bool:
    """兼容设备 isFinal 为 bool / 1 / \"true\" 等."""
    v = payload.get("isFinal")
    if v is True:
        return True
    if isinstance(v, (int, float)) and int(v) == 1:
        return True
    if isinstance(v, str) and v.strip().lower() in ("1", "true", "yes"):
        return True
    return False


@tool(
    name="xiaoyi_gui_agent",
    description=(
        "通过模拟手机屏幕交互（点击、滑动、输入等）完成仅能在 App 内完成的操作。\n\n"
        "注意：超时约 3 分钟；执行期间勿并行调用其他工具；备忘录/日程请用专用工具而非写入 query。"
        "参数 query：自然语言操作指令与期望结果。"
    ),
)
async def xiaoyi_gui_agent(query: str) -> Dict[str, Any]:
    """执行 GUI Agent 指令."""
    if not query or not isinstance(query, str) or not query.strip():
        raise ToolInputError("缺少有效参数 query（非空字符串）")

    query = query.strip()
    channel = get_xiaoyi_channel()
    if channel is None:
        raise RuntimeError(
            "无活跃小艺会话，xiaoyi_gui_agent 仅能在小艺会话活跃时使用。"
        )

    session_id = ""
    task_id = ""
    last_message_id = ""
    try:
        from jiuwenswarm.common.config import get_config

        cfg = get_config()
        xiaoyi_conf = cfg.get("channels", {}).get("xiaoyi", {})
        session_id = (xiaoyi_conf.get("last_session_id") or "").strip()
        task_id = (xiaoyi_conf.get("last_task_id") or "").strip()
        last_message_id = (xiaoyi_conf.get("last_message_id") or "").strip()
    except Exception as e:
        logger.warning("[XIAOYI_GUI_TOOL] 读取会话配置失败: %s", e)

    if not session_id:
        raise RuntimeError(
            "无活跃小艺会话，xiaoyi_gui_agent 仅能在小艺会话活跃时使用。"
        )

    # 与 TS xiaoyi-gui-tool 一致：优先 taskId；空则回退 sessionId，避免 interactionId 为空
    interaction_id = task_id if task_id else session_id
    # JSON-RPC id 与当前用户轮次对齐（见 XiaoyiChannel message/stream 写入的配置）
    message_id = last_message_id or f"gui_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

    logger.info(
        "[XIAOYI_GUI_TOOL] call session_id=%s interaction_id=%s rpc_id=%s",
        session_id[:12] + "..." if len(session_id) > 12 else session_id,
        interaction_id[:12] + "..." if len(interaction_id) > 12 else interaction_id,
        message_id[:32] + "..." if len(message_id) > 32 else message_id,
    )

    done = asyncio.Event()
    result_holder: Dict[str, Any] = {}

    def on_gui(item: dict[str, Any]) -> None:
        try:
            payload = item.get("payload") or {}
            riid = payload.get("interactionId")
            if riid is not None and str(riid).strip() != "":
                if str(riid).strip() != str(interaction_id).strip():
                    logger.debug(
                        "[XIAOYI_GUI_TOOL] 忽略非本单回包 interactionId=%r expected=%r",
                        riid,
                        interaction_id,
                    )
                    return
            if not _payload_is_gui_final(payload):
                logger.debug("[XIAOYI_GUI_TOOL] 非终帧，继续等待 isFinal")
                return
            sc = (payload.get("streamInfo") or {}).get("streamContent")
            if sc:
                result_holder["streamContent"] = sc
            else:
                result_holder["error"] = "GUI 响应缺少 streamContent"
            done.set()
        except Exception as ex:
            logger.warning("[XIAOYI_GUI_TOOL] on_gui 异常（已隔离）: %s", ex, exc_info=True)
            result_holder["error"] = f"GUI 回调异常: {ex}"
            done.set()

    # 与 channel 层锁配合：同一时间仅一单 GUI，避免多 handler 共收同一 WS 帧
    async with _get_gui_tool_async_lock(channel):
        channel.register_gui_agent_handler(on_gui)
        try:
            command = {
                "header": {
                    "namespace": "ClawAgent",
                    "name": "InvokeJarvisGUIAgentRequest",
                },
                "payload": {
                    "query": query,
                    "sessionId": session_id,
                    "interactionId": interaction_id,
                },
            }
            logger.info("[XIAOYI_GUI_TOOL] sending InvokeJarvisGUIAgentRequest")
            sent = await channel.send_xiaoyi_phone_tools_command(
                session_id=session_id,
                task_id=task_id or session_id,
                message_id=message_id,
                command=command,
            )
            if not sent:
                raise RuntimeError("发送 GUI 指令失败，WebSocket 未连接")

            await asyncio.wait_for(done.wait(), timeout=180.0)

            err = result_holder.get("error")
            if err:
                raise RuntimeError(str(err))
            text = result_holder.get("streamContent", "")
            return format_success_response(
                {"success": True, "result": text},
                "GUI 操作完成",
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError("小艺 GUI Agent 操作超时（3 分钟）") from e
        finally:
            try:
                channel.unregister_gui_agent_handler(on_gui)
            except Exception as unreg_err:
                logger.warning(
                    "[XIAOYI_GUI_TOOL] unregister_gui_agent_handler: %s",
                    unreg_err,
                )
