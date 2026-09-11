# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""xiaoyi 渠道计费上报客户端（task/status/update，经 np://claw-billing 管道）。

正式计费方案（2026-09-02 起，替代 x-hag-trace-id 标记方案——
common/invocation_context/billing_trace.py 的 NO_REPLY 虚拟调用机制已删除）：

xiaoyi 渠道（手机端 ws/link 触发）的会话由 gateway 驱动、桌面主进程不可见，
因此生命周期判定在本进程（interface_deep 挂点）完成，但**上报不直接出网**——
请求打向桌面主进程的计费代理管道 ``np://claw-billing``（BillingProxy），由主进程
注入 businessCredential/x-uid/x-device-id/x-request-from=xiaoyiWork 后转发
fulfillment 服务（与模型访问 np://claw-model 同一套「鉴权收拢主进程」封装，
本进程零业务凭证）。

协议体与桌面 billing-client.ts buildStatusBody 逐字段一致：
  - NEW     携带 query，不带 sessionId/interactionId；响应含 hasBalance
  - FINISH/FAILED 携带 sessionId/interactionId，不带 query
  - endpoint.device 固定字段照抄桌面 buildEndpointDevice（服务端校验以该形态为准）
NEW 的 query 上报前剥离渠道注入段（_strip_injected_directives：<claw_workspace>
尾段），与桌面 ReportConversations sanitizeReportQuery 同口径——工作空间路径
不随计费接口出网。

x-hag-trace-id = 裸核心段（``sessionId&interactionId短码``，≤45），与本轮全部
模型调用的 x-hag-trace-id 完全同值（TraceAwareModel 经 invocation context 注入）。

生命周期语义（对齐桌面条目）：
  - NEW 每 core 只发一次（HITL 续跑同 core 不重复）；
  - 终态（FINISH/FAILED）在一轮 query 收口时发出；HITL 挂起不发
    （续跑收口时才发）；CancelledError 按 FINISH（用户主动停止按正常完成计费）；
  - 除 NEW 外全部 fire-and-forget + 失败重试 1 次——计费永不影响会话主路径。

启用条件（缺一静默禁用——旧桌面/非桌面形态零影响）：
  密钥包携带 ``pipes.billing`` + ``billingToken`` + ``uid``。
  env ``JIUWEN_XIAOYI_BILLING=off`` 可整体关闭。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from jiuwenswarm.common.secrets_bootstrap import get_secret

logger = logging.getLogger(__name__)

_STATUS_PATH = "/fulfillment/v1/fulfillments/celia/task/status/update"
_REQUEST_TIMEOUT_SECONDS = 15.0

# NEW 去重注册表：core → 最近活跃时间戳。TTL 2h + LRU 上限 2048（防泄漏；
# 终态不驱逐——HITL 续跑同 core 仍属同一轮，不重复 NEW）。
_REGISTRY_TTL_SECONDS = 2 * 3600
_REGISTRY_MAX_SIZE = 2048
_new_reported: OrderedDict[str, float] = OrderedDict()

# fire-and-forget 任务集合（防 GC；任务自带 done_callback 移除）
_REPORT_TASKS: set[asyncio.Task] = set()

# 计费 query 剥离口径：渠道层 with_workspace_directive（common/permission_profile.py）
# 在手机端选择工作空间时拼入的 <claw_workspace> + 【工作空间】指令尾段不进计费上报——
# 工作空间路径不随计费接口出网，且与桌面侧同口径（BillingService.report 经
# restoreVisibleUserText 剥离 claw_context/claw_workspace/claw_cron_create 注入段；
# 手机端不产生 claw_context，本侧唯一注入源即该尾段）。
_WORKSPACE_DIRECTIVE_TAIL_RE = re.compile(
    r"\s*<claw_workspace>[^\r\n]*</claw_workspace>\r?\n【工作空间】[^\r\n]*\s*$"
)


def _strip_injected_directives(query: str) -> str:
    """NEW 上报前剥离渠道注入段（当前仅 <claw_workspace> 尾段；受限/完全访问两档文案同形态）。"""
    return _WORKSPACE_DIRECTIVE_TAIL_RE.sub("", query).rstrip()


def xiaoyi_billing_enabled() -> bool:
    """xiaoyi 渠道计费开关（默认开；JIUWEN_XIAOYI_BILLING=off 关闭）。"""
    return os.getenv("JIUWEN_XIAOYI_BILLING", "").strip().lower() != "off"


def _billing_config() -> tuple[str, str, str, str] | None:
    """（管道 np base, billingToken, uid, deviceId）；密钥包缺任一项 → None（禁用）。"""
    try:
        pipe_path = str(get_secret("pipes.billing", "") or "").strip()
        token = str(get_secret("billingToken", "") or "").strip()
        uid = str(get_secret("uid", "") or "").strip()
        device_id = str(get_secret("deviceId", "") or "").strip()
    except Exception:  # noqa: BLE001 - 密钥包不可用（非桌面形态）按禁用处理
        return None
    if not pipe_path or not token or not uid:
        return None
    # 管道路径 \\.\pipe\claw-billing → np://claw-billing（authority 段即管道名）
    pipe_name = pipe_path.rsplit("\\", 1)[-1]
    if not pipe_name:
        return None
    return f"np://{pipe_name}", token, uid, device_id


def _build_endpoint_device(device_id: str) -> dict[str, Any]:
    """endpoint.device 组装：字段形态与桌面 billing-client.ts buildEndpointDevice
    逐字段一致（服务端校验以该形态为准）；timezone/localTime/time 现取。"""
    now = datetime.now().astimezone()
    offset = now.utcoffset() or timezone.utc.utcoffset(now)
    offset_min = int(offset.total_seconds() // 60) if offset else 0
    sign = "+" if offset_min >= 0 else "-"
    abs_min = abs(offset_min)
    return {
        "deviceId": device_id.strip() or "unknown-device",
        "prdVer": "11.6.5.414",
        "phoneType": "MNTXM-32A",
        "deviceType": 0,
        "deviceCharacteristics": "phone",
        "screenOrientation": "vertical",
        "brand": "HUAWEI",
        "manufacturer": "HUAWEI",
        "romVer": "OpenHarmony-6.1.1.35(Beta1)",
        "ohosApiVersion": "OpenHarmony-6.1.1.35(Beta1)",
        "timezone": f"GMT{sign}{abs_min // 60:02d}:{abs_min % 60:02d}",
        "localTime": str(int(now.timestamp() * 1000)),
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "osType": "Hap",
    }


def _build_status_body(
    status: str,
    *,
    uid: str,
    device_id: str,
    query: str | None = None,
    session_id: str | None = None,
    interaction_id: str | None = None,
) -> dict[str, Any]:
    """组装请求体（与桌面 buildStatusBody 同协议）：NEW 带 query 不带
    session/interaction；FINISH/FAILED 反之。"""
    body: dict[str, Any] = {
        "userId": uid,
        "conversationStatus": status,
        "endpoint": {"device": _build_endpoint_device(device_id)},
    }
    if status == "NEW":
        if query is not None:
            body["query"] = query
    else:
        if session_id:
            body["sessionId"] = session_id
        if interaction_id:
            body["interactionId"] = interaction_id
    return body


def _purge_registry(now: float) -> None:
    expired = [k for k, ts in _new_reported.items() if now - ts > _REGISTRY_TTL_SECONDS]
    for key in expired:
        _new_reported.pop(key, None)
    while len(_new_reported) > _REGISTRY_MAX_SIZE:
        _new_reported.popitem(last=False)


def has_reported_new(core: str) -> bool:
    """该 core 是否已上报过 NEW（终态触发守卫：未发 NEW 的轮次不发终态）。"""
    return core in _new_reported


async def _post_once(np_base: str, token: str, trace_id: str, payload: dict[str, Any]) -> bool:
    """经 np://claw-billing 上报一次；2xx 返回 True，其余 False（响应码由代理透传上游）。"""
    import httpx  # 延迟导入：非桌面形态零依赖开销

    from jiuwenswarm.common.np_transport import named_pipe_transport_for

    async with httpx.AsyncClient(
        transport=named_pipe_transport_for(np_base),
        trust_env=False,  # 管道流量绝不能被 HTTP_PROXY 系环境变量劫持
        timeout=_REQUEST_TIMEOUT_SECONDS,
    ) as client:
        resp = await client.post(
            f"{np_base}{_STATUS_PATH}",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "x-hag-trace-id": trace_id,
            },
        )
        return 200 <= resp.status_code < 300


async def _run_report(status: str, trace_id: str, payload: dict[str, Any]) -> None:
    """执行一次上报（失败重试 1 次）；任何失败仅记日志——计费永不影响会话主路径。"""
    cfg = _billing_config()
    if cfg is None:
        return
    np_base, token, _uid, _device_id = cfg
    tag = f"[billing] status={status} traceid={trace_id}"
    for attempt in (1, 2):
        try:
            ok = await _post_once(np_base, token, trace_id, payload)
            if ok:
                logger.info("%s 上报成功", tag)
                return
            logger.info("%s 上游非 2xx%s", tag, "，重试一次" if attempt == 1 else "（重试后仍失败）")
        except Exception as exc:  # noqa: BLE001
            logger.info("%s 上报异常%s: %s", tag, "，重试一次" if attempt == 1 else "（重试后仍失败）", exc)
        if attempt == 1:
            await asyncio.sleep(1)
    logger.warning("%s 上报最终失败（本轮计费可能丢失）", tag)


def _schedule_report(status: str, trace_id: str, payload: dict[str, Any]) -> bool:
    """派发一次上报（fire-and-forget，不阻塞调用方主路径）。成功派发返回 True。"""
    coro = _run_report(status, trace_id, payload)
    try:
        task = asyncio.create_task(coro)
    except Exception:  # noqa: BLE001 - 无运行中事件循环等
        coro.close()
        logger.debug("[billing] 上报派发失败: %s traceid=%s", status, trace_id, exc_info=True)
        return False
    _REPORT_TASKS.add(task)
    task.add_done_callback(_REPORT_TASKS.discard)
    return True


def report_new(query: str, trace_id: str) -> bool:
    """上报 NEW（一轮 query 开始，模型调用之前）。每 core 只发一次
    （HITL 续跑同 core 不重复）；登记后终态才被允许发出。

    返回是否实际派发（已登记过/被禁用/无 core → False）。
    """
    if not trace_id or not xiaoyi_billing_enabled():
        return False
    cfg = _billing_config()
    if cfg is None:
        return False
    _np_base, _token, uid, device_id = cfg
    now = time.monotonic()
    _purge_registry(now)
    if trace_id in _new_reported:
        _new_reported[trace_id] = now
        _new_reported.move_to_end(trace_id)
        return False
    payload = _build_status_body(
        "NEW", uid=uid, device_id=device_id, query=_strip_injected_directives(query)
    )
    if not _schedule_report("NEW", trace_id, payload):
        return False
    _new_reported[trace_id] = now
    _purge_registry(now)
    return True


def report_terminal(
    trace_id: str,
    *,
    session_id: str,
    interaction_id: str,
    ok: bool,
) -> bool:
    """上报终态（FINISH/FAILED，一轮 query 收口）。守卫：未登记 NEW 的轮次不发
    orphan 终态（slash/早退路径）；调用方负责 HITL 挂起不调用本函数
    （续跑收口时才发）。CancelledError 由调用方按 ok=True（FINISH）处理。
    """
    if not trace_id or not xiaoyi_billing_enabled():
        return False
    if not has_reported_new(trace_id):
        return False
    cfg = _billing_config()
    if cfg is None:
        return False
    _np_base, _token, uid, device_id = cfg
    status = "FINISH" if ok else "FAILED"
    payload = _build_status_body(
        status,
        uid=uid,
        device_id=device_id,
        session_id=session_id,
        interaction_id=interaction_id,
    )
    return _schedule_report(status, trace_id, payload)


def reset_xiaoyi_billing_registry() -> None:
    """测试用：清空 NEW 去重注册表。"""
    _new_reported.clear()


__all__ = [
    "has_reported_new",
    "report_new",
    "report_terminal",
    "reset_xiaoyi_billing_registry",
    "xiaoyi_billing_enabled",
]
