"""AgentServer 端到端启动预热。

进程启动时创建一个临时 JiuWenSwarm DeepAgent 并执行一次简单 query，触发模块
import / checkpointer / 模型 client / DeepAgent 工厂全链路首次初始化，降低首个
真实请求时延。后台执行，失败或超时仅告警不阻塞启动。

模型调用通过 ``WarmupModelClient`` 短路（构造期注入到 ClientRegistry，零
monkeypatch）：走通 DeepAgent 全链路构建，只短路最终 LLM HTTP 调用，不消耗 token。
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from openjiuwen.core.foundation.llm.model_clients.base_model_client import (
    BaseModelClient,
)
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import (
    AssistantMessageChunk,
)

from jiuwenswarm.common.config import get_config, get_merged_config_dict
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.edition import is_enterprise

logger = logging.getLogger(__name__)


class WarmupModelClient(BaseModelClient):
    """Mock LLM client for startup warmup.

    构造期注入：类定义即触发 ``BaseModelClient.__init_subclass__`` 自动注册到进程级
    ``ClientRegistry``（provider name = "warmup", type = "llm"）。预热 agent 构建时
    ``create_model_client`` 命中本类，走通整个 Model 构建链路（ModelClientConfig 验证、
    create_model_client、callback framework 包装、create_deep_agent 工厂、rails、
    checkpointer），只短路最终 HTTP 调用。
    """

    __client_name__ = "warmup"
    __client_type__ = "llm"

    def __init__(self, model_config: Any, model_client_config: Any) -> None:
        # 跳过 _validate_config（warmup 无需真 api_key/api_base），只存引用。
        self.model_config = model_config
        self.model_client_config = model_client_config

    async def invoke(self, messages: Any, **kwargs: Any) -> AssistantMessage:
        return AssistantMessage(content="warmup ok")

    async def stream(self, messages: Any, **kwargs: Any):  # type: ignore[override]
        yield AssistantMessageChunk(content="warmup ok")

    async def generate_image(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError

    async def generate_speech(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError

    async def generate_video(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError


def _build_warmup_config_base() -> dict[str, Any]:
    """Deep copy 当前 config 并把模型 client_provider 改成 "warmup"。

    覆盖 defaults 列表 + legacy 单条目 + react 段三条路径，确保预热 agent 构建时
    命中 WarmupModelClient。
    """
    cfg = deepcopy(get_config())
    models = cfg.setdefault("models", {})
    # defaults 列表（新格式）。setdefault 确保缺失时回写，避免 mock 不生效。
    for entry in models.get("defaults", []) or []:
        entry.setdefault("model_client_config", {})["client_provider"] = "warmup"
    # legacy 单条目
    default_mc = models.get("default", {})
    if isinstance(default_mc.get("model_client_config"), dict):
        default_mc["model_client_config"]["client_provider"] = "warmup"
    # react 段
    react = cfg.get("react", {})
    if isinstance(react.get("model_client_config"), dict):
        react["model_client_config"]["client_provider"] = "warmup"
    return cfg


async def _cleanup_prewarm_agent(agent: Any) -> None:
    """释放预热 agent 持有的 sys_operation / tool / rails 引用。

    预热 agent 独立构造，不进 AgentManager 缓存，用完即清理，避免与生产会话纠缠。
    ``agent`` 可能为 None（构造失败时），此时直接返回。
    """
    if agent is None:
        return
    try:
        adapter = getattr(agent, "_adapter", None)
        if adapter is None:
            return
        # DeepAgent 资源释放：优先调用 adapter 的 close/teardown（若提供）。
        close = getattr(adapter, "close", None)
        if callable(close):
            await close()
    except Exception:  # noqa: BLE001
        logger.warning("[Prewarm] cleanup prewarm agent failed", exc_info=True)


async def warmup_import_and_checkpointer() -> None:
    """阶段1/2：interface_deep import + checkpointer。

    快速、有界、不抛异常（失败仅告警）。本函数由 ``_startup_warmup_task`` 承载，
    ``ensure_interface_deep_and_checkpointer`` 兜底 await 本 task 时只会等待
    阶段1/2 完成，不会被阶段3 的慢速 query 拖住。
    """
    # 延迟 import：避免模块加载时拉起 interface_deep（破坏 fire-and-forget 语义）。
    from jiuwenswarm.server.agent_ws_server import (
        _warm_interface_deep_module,
    )
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        ensure_persistent_checkpointer,
    )

    # 阶段0：合并配置缓存预热。
    # ``get_merged_config_dict`` 冷加载（双 YAML 读+合并，实测 ~150ms）会被
    # RuntimePromptRail 的模型名兜底路径在每次模型调用时触发；启动时在线程池里
    # 预热一次，消除首个真实请求的冷加载时延（OA.05000010）。放在快速有界的
    # 阶段1/2 序列里，``ensure_interface_deep_and_checkpointer`` 兜底 await 本
    # task 时即可保证缓存就绪（阶段3 为慢速 fire-and-forget，企业版还会跳过）。
    t0 = time.perf_counter()
    try:
        await asyncio.to_thread(get_merged_config_dict)
    except Exception:  # noqa: BLE001
        logger.warning("[Prewarm] stage=0 merged_config failed", exc_info=True)
    logger.info(
        "[Prewarm] stage=0 name=merged_config elapsed_ms=%.1f",
        (time.perf_counter() - t0) * 1000,
    )

    # 阶段1：interface_deep import（原 _warm_interface_deep_module 职责）。
    # 其内部已有 ``interface_deep_warmup cache=miss ok elapsed_ms=`` 埋点。
    t0 = time.perf_counter()
    try:
        await _warm_interface_deep_module()
    except Exception:  # noqa: BLE001
        logger.warning("[Prewarm] stage=1 import_interface_deep failed", exc_info=True)
    logger.info(
        "[Prewarm] stage=1 name=import_interface_deep elapsed_ms=%.1f",
        (time.perf_counter() - t0) * 1000,
    )

    # 阶段2：checkpointer（原 ensure_persistent_checkpointer 职责，DeepAgent 构建前置）。
    t0 = time.perf_counter()
    try:
        await ensure_persistent_checkpointer()
    except Exception:  # noqa: BLE001
        logger.warning("[Prewarm] stage=2 checkpointer failed", exc_info=True)
    logger.info(
        "[Prewarm] stage=2 name=checkpointer elapsed_ms=%.1f",
        (time.perf_counter() - t0) * 1000,
    )


async def warmup_deep_agent_query(
    *,
    query: str = "hello",
    channel_id: str = "__prewarm__",
    mode: str = "agent",
    timeout_s: float = 120.0,
    mock_model: bool = True,
) -> None:
    """阶段3：创建临时 DeepAgent 并执行一次 query（端到端预热）。

    慢速、独立 fire-and-forget task，**不**由 ``_startup_warmup_task`` 承载——
    这样 ``ensure_interface_deep_and_checkpointer`` 兜底 await 阶段1/2 时不会被
    本阶段的慢速 query（mock ~2-3s / 真实 LLM 最长 timeout_s=120s）阻塞。

    三阶段全部 try/except，失败仅告警不抛。预热 agent 独立构造，不进 AgentManager
    缓存，与生产隔离。本函数应在 ``warmup_import_and_checkpointer`` 之后单独
    ``create_task`` 调用。

    企业版无请求 ``routing`` identity，DeepAgent 预热会误读空模型配置并打 ERROR，
    故跳过阶段3（阶段1/2 import+checkpointer 仍可跑）。
    """
    if is_enterprise():
        logger.info(
            "[Prewarm] skip stage=3 deep agent query on enterprise "
            "(no routing identity for model config)"
        )
        return

    from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm

    t_listen = time.perf_counter()

    # 阶段3a：创建临时 DeepAgent（独立临时实例，不经 AgentManager）。
    config_base = _build_warmup_config_base() if mock_model else get_config()
    agent: Any = None
    t0 = time.perf_counter()
    try:
        agent = JiuWenSwarm()
        await agent.create_instance({}, mode=mode, config_base=config_base)
    except Exception:  # noqa: BLE001
        logger.warning("[Prewarm] stage=3a create_instance failed", exc_info=True)
        await _cleanup_prewarm_agent(agent)
        return
    logger.info(
        "[Prewarm] stage=3a name=create_instance elapsed_ms=%.1f",
        (time.perf_counter() - t0) * 1000,
    )

    # 阶段3b：执行 query（复用首请求 latency 风格）。结果不阻塞预热完成。
    request = AgentRequest(
        request_id=f"prewarm-{uuid4().hex[:12]}",
        channel_id=channel_id,
        session_id=f"{channel_id}_session",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": query, "mode": mode},
        timestamp=time.time(),
    )
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(agent.process_message(request), timeout=timeout_s)
        logger.info(
            "[Prewarm] stage=3b name=query ok elapsed_ms=%.1f",
            (time.perf_counter() - t0) * 1000,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[Prewarm] stage=3b name=query timeout elapsed_ms=%.1f timeout_s=%.1f",
            (time.perf_counter() - t0) * 1000,
            timeout_s,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[Prewarm] stage=3b name=query failed elapsed_ms=%.1f",
            (time.perf_counter() - t0) * 1000,
            exc_info=True,
        )
    finally:
        await _cleanup_prewarm_agent(agent)

    # 总耗时（从阶段3起点起）。
    logger.info(
        "[Prewarm] done mock_model=%s total_ms=%.1f listen_to_ready_ms=%.1f",
        mock_model,
        (time.perf_counter() - t_listen) * 1000,
        (time.perf_counter() - t_listen) * 1000,
    )


async def run_startup_warmup(
    *,
    query: str = "hello",
    channel_id: str = "__prewarm__",
    mode: str = "agent",
    timeout_s: float = 120.0,
    mock_model: bool = True,
) -> None:
    """端到端预热：阶段1/2（import+checkpointer）→ 阶段3（临时 DeepAgent+query）。

    保留向后兼容。新代码应直接调用 ``warmup_import_and_checkpointer`` +
    ``warmup_deep_agent_query`` 并分别 create_task，使 ``_startup_warmup_task``
    只承载阶段1/2（供 ensure_interface_deep_and_checkpointer 兜底 await）。
    """
    await warmup_import_and_checkpointer()
    await warmup_deep_agent_query(
        query=query,
        channel_id=channel_id,
        mode=mode,
        timeout_s=timeout_s,
        mock_model=mock_model,
    )


_LTM_PREWARM_MAX_DIRS = 20
_LTM_PREWARM_TIMEOUT_S = 120.0


async def warmup_ltm_vector_store() -> None:
    """预热 LTM 向量库（chromadb PersistentClient）。

    openjiuwen 的 ChromaVectorStore 首次使用才创建 PersistentClient，rust 扩展
    加载 + sqlite 建库在事件循环线程上可达秒级停顿，曾触发网关 keepalive ping
    timeout 主动断连（2026-10-20 E-1020-1017-1cb3）。启动期在后台线程预热已存在
    的向量目录，chromadb 按 path 共享 system 缓存，运行期首建不再发生停顿。
    失败仅告警，不影响启动。
    """
    started = time.monotonic()
    try:
        warmed = await asyncio.wait_for(
            asyncio.to_thread(_prewarm_ltm_vector_store_sync),
            timeout=_LTM_PREWARM_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Prewarm] stage=ltm name=chroma failed elapsed_ms=%.1f err=%s",
                       (time.monotonic() - started) * 1000, exc)
        return
    if warmed:
        logger.info(
            "[Prewarm] stage=ltm name=chroma ok dirs=%s elapsed_ms=%.1f",
            ",".join(warmed),
            (time.monotonic() - started) * 1000,
        )


def _prewarm_ltm_vector_store_sync() -> list[str]:
    """同步预热：import chromadb + 打开已存在的 LTM 向量目录。"""
    import chromadb

    warmed: list[str] = []
    for vector_dir in _ltm_vector_dirs_for_prewarm():
        try:
            chromadb.PersistentClient(path=str(vector_dir))
            warmed.append(str(vector_dir))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Prewarm] chroma warmup failed for %s: %s", vector_dir, exc)
    return warmed


def _ltm_vector_dirs_for_prewarm() -> list[Path]:
    """收集需要预热的向量目录（仅已存在的目录，不为新用户凭空建库）。

    路径解析与运行期构建（external_memory_config.build_openjiuwen_provider_config）
    完全一致：tip/env → config.yaml memory.external.openjiuwen.vector_persist_dir →
    租户默认目录。多实例兄弟目录扫描的末级子路径从默认解析结果推导
    （workspace_default → * 与 * /* 两级展开），不再硬编码 ltm/chroma。
    """
    from jiuwenswarm.agents.harness.common.memory.external_memory_config import (
        get_external_memory_config,
        resolve_openjiuwen_store_paths,
    )
    from jiuwenswarm.common.utils import get_user_workspace_dir

    dirs: list[Path] = []
    oj_cfg = get_external_memory_config().get("openjiuwen") or {}
    _, vector_dir, _ = resolve_openjiuwen_store_paths(oj_cfg)
    default_dir = Path(vector_dir)
    if default_dir.is_dir():
        dirs.append(default_dir)
    candidates: list[Path] = []
    try:
        root = get_user_workspace_dir()
        rel = default_dir.relative_to(root)
        parts = rel.parts
        patterns: list[str] = []
        if len(parts) > 1:
            tail = parts[1:]
            patterns.append(str(Path("*").joinpath(*tail)))
            patterns.append(str(Path("*").joinpath("*", *tail)))
        for pattern in patterns:
            candidates.extend(sorted(root.glob(pattern)))
    except Exception:  # noqa: BLE001
        logger.warning("[Prewarm] chroma workspace scan failed", exc_info=True)
    for candidate in candidates:
        if len(dirs) >= _LTM_PREWARM_MAX_DIRS:
            break
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)
    return dirs
