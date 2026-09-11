# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Login Token tool - 自动获取用户授权信息.

翻译自 xy_channel-openclaw6.6/src/tools/login-token-tool.ts（huawei_id_tool）.

闭环：
1. 工具被某个需要用户鉴权的 skill 调用，带上 clientId / skillName；
2. 通过 DeviceCommandManager 跨进程桥把 getLoginToken 请求投到 gateway 进程，
   gateway 侧 XiaoyiDeviceCommandHandler 见 intent=GetLoginToken 特判，调
   XiaoyiChannel.send_login_token_artifact 下发 artifact-update（parts 为单个
   自定义 part {kind: "getLoginToken", clientId, skillName}，1:1 复刻 TS 第 53-90 行）；
3. 端侧小艺 App 收到该 part 弹授权 UI，用户完成授权后端侧
   LoginTokenEvent.ClawAutoLogin 回写 /home/sandbox/.openclaw/.xiaoyitoken.json
   （由端侧 login-token-handler.ts 写入，每条 {clientId, timestamp, message, code}）；
4. 工具每 5s 轮询该文件，匹配 clientId 且 timestamp 在 5 分钟有效期内即返回成功.

跨进程说明：XiaoyiChannel 实例只存在于 gateway 进程（app_gateway），工具在
agentserver 进程（app_agentserver）执行，两进程内存不共享，直接 get_xiaoyi_channel()
会拿到 None → "Xiaoyi channel 不可用"。故照搬隔壁手机端工具（call_phone /
send_message / check_plugin_privilege 等）的解法：走 execute_device_command 桥，
由 gateway 侧代为下发，agentserver 侧不碰 channel 实例。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.invocation_context import get_current_invocation_context
from jiuwenswarm.server.xiaoyi_invocation import build_xiaoyi_device_command_context
from jiuwenswarm.common.utils import logger

from .utils import ToolInputError, execute_device_command


# 跨进程桥使用的 intent_name；gateway 侧 XiaoyiDeviceCommandHandler 见此值特判
# 走 send_login_token_artifact（artifact wire），不走普通 phone tool command。
LOGIN_TOKEN_INTENT = "GetLoginToken"

POLL_INTERVAL_MS = 5000  # 5 秒轮询一次
TIMEOUT_MS = 60000  # 1 分钟总超时

# 端侧小艺 App 授权完成后的真实回写行为：把 token 写入 .xiaoyienv 的
#   <clientId>_login_token            = <token 值>
#   <clientId>_login_token_expire_time = <ISO8601 UTC 过期时间，如 2026-07-30T14:39:33Z>
# 这与 mcd / eastmoney skill 文档读凭证的位置完全一致（它们本就读 .xiaoyienv 的
# 该字段）。注意：TS 原版 login-token-handler.ts 设计的是监听 ClawAutoLogin 事件写
# .xiaoyitoken.json，但 jiuwenswarm 未移植该 handler，且端侧实际行为是直接写
# .xiaoyienv——故轮询改读 .xiaoyienv，与端侧真实回写对齐。
#
# 重要：端侧回写的是 /home/sandbox/.openclaw/.xiaoyienv（authoritative）。该文件
# 与 /home/sandbox/.jiuwenswarm/.xiaoyienv 原本硬链接同 inode，但端侧回写用的是
# 换 inode 的写法（rename/sed -i），会断硬链接——断开后两文件独立，token 只在
# .openclaw 那份。故工具同时读两份取并集（.openclaw 优先），并把读到的 token
# 同步回 .jiuwenswarm 那份 + 重建硬链接，让 skill（它读 .jiuwenswarm 那份）也能拿到。
XIAOYIENV_OPENCLAW = "/home/sandbox/.openclaw/.xiaoyienv"
XIAOYIENV_JIUWEN = "/home/sandbox/.jiuwenswarm/.xiaoyienv"


def _read_env_file(path: str) -> dict[str, str]:
    """读取单个 .xiaoyienv 文件为 key->value 字典（KEY=VALUE 行格式）."""
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    except OSError as err:
        logger.warning("[LOGIN_TOKEN] 读取 %s 失败: %s", path, err)
    return env


def _read_xiaoyienv() -> dict[str, str]:
    """读两份 .xiaoyienv 取并集（.openclaw 端侧 authoritative 优先覆盖）."""
    env = _read_env_file(XIAOYIENV_JIUWEN)
    env.update(_read_env_file(XIAOYIENV_OPENCLAW))
    return env


def _sync_token_to_jiuwenenv(client_id: str) -> None:
    """把 .openclaw/.xiaoyienv 里的 <clientId>_login_token(_expire_time) 同步到
    .jiuwenswarm/.xiaoyienv，并重建硬链接，让读 jiuwen 那份的 skill 也能拿到 token.

    用覆盖写同 inode 的方式改 jiuwen 文件（不换 inode），再把 openclaw 删后硬链接
    回 jiuwen（重建 nlink=2），避免端侧下次回写又因换 inode 断链。失败只记日志，不抛
    ——同步是锦上添花，工具结果已由 _read_xiaoyienv（读 openclaw）保证。
    """
    token_key = f"{client_id}_login_token"
    expire_key = f"{client_id}_login_token_expire_time"
    src = _read_env_file(XIAOYIENV_OPENCLAW)
    token_value = src.get(token_key, "").strip()
    if not token_value:
        return  # openclaw 里没有该 token，无需同步
    dst = _read_env_file(XIAOYIENV_JIUWEN)
    dst[token_key] = token_value
    dst[expire_key] = src.get(expire_key, "")
    lines = [f"{k}={v}" for k, v in dst.items()]
    payload = "\n".join(lines) + "\n"
    try:
        # 覆盖写 jiuwen 文件（cat > 同 inode，不断现有链接；若 jiuwen 已与 openclaw
        # 断链则此处只改 jiuwen 自己的 inode）
        with open(XIAOYIENV_JIUWEN, "w", encoding="utf-8") as f:
            f.write(payload)
        os.chmod(XIAOYIENV_JIUWEN, 0o600)
        # 重建硬链接：删 openclaw 再 ln 到 jiuwen，两路径重新共享同 inode
        if os.path.exists(XIAOYIENV_OPENCLAW):
            os.unlink(XIAOYIENV_OPENCLAW)
        os.link(XIAOYIENV_JIUWEN, XIAOYIENV_OPENCLAW)
        logger.info(
            "[LOGIN_TOKEN] token 已同步到 .jiuwenswarm/.xiaoyienv 并重建硬链接 "
            "clientId=%s", client_id,
        )
    except OSError as err:
        logger.warning("[LOGIN_TOKEN] 同步 token 到 jiuwenenv 失败: %s", err)


def _parse_expire_time(expire_raw: str) -> float | None:
    """解析 <clientId>_login_token_expire_time（ISO8601，如 2026-07-30T14:39:33Z）为 epoch 秒.

    返回 None 表示无法解析（当作未过期，保守放行）。
    """
    if not expire_raw:
        return None
    text = expire_raw.strip()
    try:
        # 兼容带 Z / 带偏移 / 不带时区三种写法
        iso = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _match_valid_token(
    env: dict[str, str],
    client_id: str,
    baseline_expire: float | None = None,
) -> str | None:
    """从 .xiaoyienv 匹配 <clientId>_login_token 且未过期，返回 token 值或 None.

    baseline_expire: 下发授权前该 clientId 旧 token 的 expire_time(epoch 秒)。
    轮询时只接受 expire_time **严格大于** baseline 的新 token —— 否则 .xiaoyienv
    里残留的旧 token（即使尚未到 expire 字面过期时间，但东方财富服务端早已作废）
    会被误判为"授权成功"提前返回，导致 skill 拿旧 token 去请求得到
    "loginToken 无效或已过期"。baseline 为 None（首次授权、无旧 token）时
    退化为只查"非空且未过期"。
    """
    token_key = f"{client_id}_login_token"
    token_value = env.get(token_key, "").strip()
    if not token_value:
        return None
    expire_key = f"{client_id}_login_token_expire_time"
    expire_epoch = _parse_expire_time(env.get(expire_key, ""))
    if expire_epoch is not None:
        if time.time() >= expire_epoch:
            logger.info(
                "[LOGIN_TOKEN] token 已过期 clientId=%s expire=%s",
                client_id, env.get(expire_key),
            )
            return None
        if baseline_expire is not None and expire_epoch <= baseline_expire:
            # 文件里的 token 还是下发前那份旧 token（expire 没更新），继续等端侧回写新 token。
            logger.info(
                "[LOGIN_TOKEN] 仍是旧 token（expire 未更新）clientId=%s "
                "expire=%s baseline=%s，继续轮询等新 token",
                client_id, env.get(expire_key), baseline_expire,
            )
            return None
    return token_value


def _token_result_text(token_value: str | None) -> str:
    """根据是否拿到 token 翻译结果文案（对齐 login-token-tool.ts 语义）."""
    if token_value:
        return "Successfully obtained user authorization"
    return "Failed to obtain user authorization"


async def _poll_for_token(client_id: str, baseline_expire: float | None = None) -> str:
    """每 5s 轮询 .xiaoyienv 的 <clientId>_login_token 字段，1 分钟超时.

    端侧小艺 App 授权完成后把 token 回写到 .xiaoyienv 的
    <clientId>_login_token + <clientId>_login_token_expire_time，工具轮询该文件
    取结果（对齐 login-token-tool.ts 第 92-153 行的节奏：先等 5s，再每 5s 轮询，
    1 分钟超时）。

    baseline_expire: 下发授权前旧 token 的 expire_time。只接受 expire 更新的新 token，
    避免把残留旧 token（未到字面过期但服务端已作废）误判为成功。
    """
    start_ms = time.time() * 1000

    # 与 TS 一致：先等 5 秒再开始第一次轮询（给端侧弹卡 + 用户点授权留时间）
    await asyncio.sleep(POLL_INTERVAL_MS / 1000)

    while True:
        elapsed_ms = time.time() * 1000 - start_ms
        if elapsed_ms >= TIMEOUT_MS:
            logger.warning(
                "[LOGIN_TOKEN] 超时未拿到授权 clientId=%s elapsed_ms=%d",
                client_id, int(elapsed_ms),
            )
            return "Failed to obtain user authorization"

        env = _read_xiaoyienv()
        token_value = _match_valid_token(env, client_id, baseline_expire)
        if token_value is not None:
            logger.info(
                "[LOGIN_TOKEN] 拿到授权 clientId=%s token_len=%d expire=%s",
                client_id, len(token_value), env.get(f"{client_id}_login_token_expire_time"),
            )
            # 同步 token 到 .jiuwenswarm/.xiaoyienv + 重建硬链接，让读该文件的
            # skill 也能拿到凭证（端侧回写的是 .openclaw 那份，可能已断硬链接）。
            _sync_token_to_jiuwenenv(client_id)
            return _token_result_text(token_value)

        await asyncio.sleep(POLL_INTERVAL_MS / 1000)


@tool(
    name="huawei_id_tool",
    description=(
        "Obtains the user's authorization information. Call this tool when a skill requires user "
        "authentication. The tool sends an authorization request to the user's device via the current "
        "Xiaoyi conversation channel (an authorization card pops up), and returns the result once you "
        "complete the authorization. Do not call this tool repeatedly. "
        "Parameter clientId: the unique identifier of the account service, which is provided during "
        "the execution of a specific skill; "
        "Parameter skillName: the name of the specific skill."
    ),
)
async def huawei_id_tool(clientId: str, skillName: str) -> Dict[str, Any]:
    """翻译自 login-token-tool.ts 的 huawei_id_tool（loginTokenTool）.

    通过通用 Reverse RPC 下发 getLoginToken artifact（gateway 侧执行），
    再轮询 /home/sandbox/.openclaw/.xiaoyitoken.json 取授权结果。

    Args:
        clientId: 账号服务唯一标识（非空字符串）
        skillName: skill 名称（非空字符串）

    Returns:
        content[0].text 为授权结果文案（获取用户授权成功 / 失败 / App版本较低）
    """
    client_id = (clientId or "").strip()
    skill_name = (skillName or "").strip()
    if not client_id:
        raise ToolInputError("Missing required parameter: clientId must be a non-empty string")
    if not skill_name:
        raise ToolInputError("Missing required parameter: skillName must be a non-empty string")

    invocation = get_current_invocation_context()
    if invocation is None:
        raise RuntimeError("No active Jiuwen invocation context")
    context = build_xiaoyi_device_command_context(invocation)

    # 跨进程桥：把 getLoginToken 请求投到 gateway 进程，由 gateway 侧
    # XiaoyiDeviceCapability 的共享 executor 特判 intent=GetLoginToken 调
    # channel.send_login_token_artifact 下发 artifact。command 带 client_id /
    # skill_name，gateway 侧从中取参。message_id（JSON-RPC id）由 gateway 侧从
    # context.xiaoyi_rpc_id 解析，与 TS 第 75 行 id=messageId 对齐。
    command = {
        "client_id": client_id,
        "skill_name": skill_name,
    }

    # 下发前先读当前 .xiaoyienv 里该 clientId 的旧 token expire_time 作 baseline。
    # 轮询时只接受 expire_time 严格大于 baseline 的新 token —— 否则 .xiaoyienv 里
    # 残留的旧 token（expire 字面时间可能还没到，但东方财富服务端早已作废）会被
    # _match_valid_token 误判为"授权成功"提前返回，skill 拿旧 token 去请求得到
    # "loginToken 无效或已过期"。
    pre_env = _read_xiaoyienv()
    baseline_expire = _parse_expire_time(
        pre_env.get(f"{client_id}_login_token_expire_time", "")
    )
    logger.info(
        "[LOGIN_TOKEN] 下发授权请求（走桥）clientId=%s skillName=%s intent=%s "
        "channel_id=%s session=%s baseline_expire=%s",
        client_id, skill_name, LOGIN_TOKEN_INTENT,
        context.channel_id, context.jiuwen_session_id, baseline_expire,
    )

    try:
        # 下发本身是异步的（artifact 发完即返回），给足桥往返时间；授权完成由
        # 端侧回写 token 文件，下面 _poll_for_token 独立轮询，不占这个 timeout。
        await execute_device_command(
            LOGIN_TOKEN_INTENT,
            command,
            timeout=30.0,
        )
    except Exception as exc:
        # 桥下发失败（channel 不可用 / gateway 未连 / 超时）——如实抛出，让上层
        # 把错误透传给 LLM，不要静默吞掉。
        logger.exception("[LOGIN_TOKEN] 下发授权请求失败（桥）: %s", exc)
        raise RuntimeError(f"Failed to send the authorization request: {exc}") from exc

    logger.info("[LOGIN_TOKEN] 授权请求已下发，开始轮询 token 文件")
    result_text = await _poll_for_token(client_id, baseline_expire)
    return {
        "content": [
            {
                "type": "text",
                "text": result_text,
            }
        ]
    }
