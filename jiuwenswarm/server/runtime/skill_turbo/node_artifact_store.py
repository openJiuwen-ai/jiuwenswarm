# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 节点产物持久化（跨请求复用）。

职责：
    将 SkillTurbo 每个节点执行完成后的关键信息与产物记录到 openjiuwen
    ``Session`` 的状态里，键 ``__skill_turbo_node_artifacts__``。当任务中断后
    恢复执行走 DeepAgent 流程时，可读取该记录复用已产出的部分产物与信息，
    避免重复劳动。

设计原则：
    - 复用 checkpointer 持久化链路（pre_run -> update_state -> post_run），
      不引入第二份内存存储。
    - 与 ``permission_bridge.save_resume_ctx`` 范式一致，可共用同一次落盘。
    - 只存摘要 + 小结构化信息 + 文件路径，绝不存大文件内容
      （随 agent state 整体 pickle，体积敏感）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# SkillTurbo 自有 session state key -- 与 openjiuwen 自身命名空间区分，所以前后用双下划线。
SKILL_TURBO_NODE_ARTIFACTS_KEY = "__skill_turbo_node_artifacts__"


def _resolve_session_id(session: Any) -> str:
    """统一获取 session ID,与 executor._session_id 逻辑一致。

    Session 对象的 ID 通过 get_session_id() 方法获取,
    而非 session_id 属性(后者不存在,导致日志中 sid=? )。
    """
    if session is not None and callable(getattr(session, "get_session_id", None)):
        sid = session.get_session_id()
        if sid:
            return str(sid)
    for attr in ("session_id", "_session_id"):
        sid = getattr(session, attr, None)
        if sid:
            return str(sid)
    return "?"


async def save_node_artifacts(
    session: Any,
    *,
    skill: str,
    nodes: dict[str, dict[str, Any]],
    plan_code_hash: str = "",
    skip_post_run: bool = False,
) -> None:
    """持久化节点产物记录到 session state。

    会自己 pre_run+post_run 保证持久化（跨请求靠 checkpointer 取回）。
    调用方如果在已 pre_run 的上下文里，重复 pre_run 是 no-op。

    Args:
        session: openjiuwen Session 实例。
        skill: 当前执行的 skill 标识（如 "ppt"），仅作溯源记录存储，不参与注入时比对。
        nodes: 以 plan_name 为 key 的节点产物字典，结构见模块文档。
        plan_code_hash: 当前 plan_code 的哈希（executor._hash_code）。HITL resume
            重放时按此比对，只有同一 plan 的产物才继承进新 executor（P2-2）。
            旧记录无此字段时视为匹配（fail-open 兼容）。
        skip_post_run: 跳过 post_run（仅 pre_run + update_state）。用于调用方随后
            会自行 post_run 持久化的场景（如中断路径与 save_resume_ctx 合并落盘），
            避免对主 session 重复 post_run 触发 close_stream。
    """
    if session is None:
        logger.warning("[SkillTurboArtifacts] save_node_artifacts: session is None, skipping")
        return
    if not nodes:
        logger.debug("[SkillTurboArtifacts] save_node_artifacts: empty nodes, skipping")
        return
    sid = _resolve_session_id(session)
    logger.info(
        "[SkillTurboArtifacts] save_node_artifacts: sid=%s skill=%s nodes=%d",
        sid,
        skill,
        len(nodes),
    )
    payload = {
        "skill": skill,
        "updated_at": time.time(),
        "plan_code_hash": plan_code_hash,
        "nodes": nodes,
    }
    try:
        await session.pre_run(inputs=None)
    except Exception as e:
        logger.warning(
            "[SkillTurboArtifacts] save_node_artifacts pre_run failed: sid=%s err=%s", sid, e
        )
        return
    try:
        session.update_state({SKILL_TURBO_NODE_ARTIFACTS_KEY: payload})
        logger.debug(
            "[SkillTurboArtifacts] save_node_artifacts: updated OK sid=%s, payload=%s",
            sid,
            payload,
        )
    except Exception as e:
        logger.warning(
            "[SkillTurboArtifacts] save_node_artifacts update_state failed: sid=%s err=%s", sid, e
        )
        return
    if skip_post_run:
        logger.debug(
            "[SkillTurboArtifacts] save_node_artifacts: skip_post_run sid=%s "
            "(caller will post_run)",
            sid,
        )
        return
    try:
        await session.post_run()
        logger.info("[SkillTurboArtifacts] save_node_artifacts: persisted OK sid=%s", sid)
    except Exception as e:
        logger.warning(
            "[SkillTurboArtifacts] save_node_artifacts post_run failed: sid=%s err=%s", sid, e
        )


async def load_node_artifacts(session: Any) -> dict[str, Any] | None:
    """读取节点产物记录。返回 None 表示无可复用记录。

    与 ``clear_node_artifacts`` 范式一致：不自行 ``pre_run``，由调用方
    在已 ``pre_run`` 的上下文里调用（checkpointer state 需 pre_run 后
    才能 get_state 取到）。
    """
    if session is None:
        logger.warning("[SkillTurboArtifacts] load_node_artifacts: session is None")
        return None
    sid = _resolve_session_id(session)
    try:
        state = session.get_state(SKILL_TURBO_NODE_ARTIFACTS_KEY)
        logger.debug(
            "[SkillTurboArtifacts] load_node_artifacts: get_state sid=%s state=%s",
            sid,
            state,
        )
    except Exception as e:
        logger.warning(
            "[SkillTurboArtifacts] load_node_artifacts get_state failed: sid=%s err=%s", sid, e
        )
        return None
    if isinstance(state, dict) and state.get("nodes"):
        logger.info(
            "[SkillTurboArtifacts] load_node_artifacts: found records sid=%s skill=%s nodes=%d",
            sid,
            state.get("skill"),
            len(state.get("nodes", {})),
        )
        return state
    logger.debug(
        "[SkillTurboArtifacts] load_node_artifacts: no records sid=%s state_type=%s",
        sid,
        type(state).__name__,
    )
    return None


async def clear_node_artifacts(session: Any) -> None:
    """清除节点产物记录（任务最终成功后调用）。

    调用方应已 pre_run，并负责 post_run 持久化。
    此函数只 update in-memory state，不触发 post_run，
    避免与调用方自己的 post_run 重复（重复 post_run 可能导致事件重复触发）。
    """
    if session is None:
        return
    try:
        session.update_state({SKILL_TURBO_NODE_ARTIFACTS_KEY: None})
        logger.debug(
            "[SkillTurboArtifacts] clear_node_artifacts: persisted OK sid=%s",
            _resolve_session_id(session),
        )
    except Exception as exc:
        logger.debug(
            "[SkillTurboArtifacts] clear_node_artifacts update_state failed: %s", exc
        )
