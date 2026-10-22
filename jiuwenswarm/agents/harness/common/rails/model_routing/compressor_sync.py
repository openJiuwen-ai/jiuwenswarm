"""主对话换模后，把仍攥着旧 ``Model`` 的下游组件一起换掉。

根因：``ContextProcessorRail.init`` 在 agent 创建时把 ``model_config`` 烤进
DialogueCompressor / RoundLevelCompressor 等实例；随后 ``ModelRoutingRail`` /
``interface_deep._apply_model_to_react_agent`` 只 ``set_llm`` 主对话模型，**不**更新
已缓存的 context pool 里的压缩器。结果主对话已是具体模型，压缩器仍拿着过期 id →
上游报模型无效，压缩 noop，上下文膨胀。

会话记忆（``SessionMemoryManager``）不在 context processor 列表里。这里只重绑当初
从主模型继承来的 ``memory.model`` / ``model_client``，并清掉已缓存的 update agent。
调用方显式配置的记忆模型不会被覆盖。

本模块原先住在 ``common/model_gears.py``（那张「档位关键字 → 具体模型 id」的表）。
档位翻译整体收回 relay 侧后，四档表连同 ``model_gears`` 一起删除；换模同步压缩器
与档位无关，故单独迁到这里，由 rail 与 adapter 共用。
"""

from __future__ import annotations

import importlib
from typing import Any

from jiuwenswarm.common.utils import logger


def _assign_attr(target: object, name: str, value: object) -> bool:
    """把属性写到目标上。写入失败时记调试日志并返回 False。"""
    try:
        setattr(target, name, value)
    except Exception:
        logger.debug(
            "failed to assign %s on %s",
            name,
            type(target).__name__,
            exc_info=True,
        )
        return False
    return True


def _new_compression_executor(model: object) -> object:
    """按当前 Model 重建压缩执行器。依赖缺失时由调用方处理。"""
    module_name = (
        "openjiuwen.core.context_engine.processor.forked."
        "compressor.support.compression_executor"
    )
    module = importlib.import_module(module_name)
    return module.CompressionExecutor(model)


def _rebind_compression_executor(proc: object, model: object) -> bool:
    """重建压缩执行器。失败则清空并记 warning，避免静默沿用旧模型。"""
    try:
        rebuilt = _new_compression_executor(model)
    except Exception:
        logger.warning(
            "compression executor rebuild failed; cleared stale executor on %s",
            type(proc).__name__,
            exc_info=True,
        )
        _assign_attr(proc, "_compression_executor", None)
        return False
    if _assign_attr(proc, "_compression_executor", rebuilt):
        return True
    logger.warning(
        "compression executor assign failed; cleared stale executor on %s",
        type(proc).__name__,
    )
    _assign_attr(proc, "_compression_executor", None)
    return False


def _iter_session_memory_managers(
    react_agent: object,
    host: object | None,
):
    """从 DeepAgent rail 上找出已启用的 SessionMemoryManager。"""
    seen_owners: set[int] = set()
    seen_mgrs: set[int] = set()
    for owner in (host, react_agent):
        if owner is None or id(owner) in seen_owners:
            continue
        seen_owners.add(id(owner))
        for attr in ("_registered_rails", "_pending_rails", "_stale_rails"):
            rails = getattr(owner, attr, None)
            if not rails:
                continue
            try:
                rail_list = list(rails)
            except TypeError:
                continue
            for rail in rail_list:
                mgr = getattr(rail, "_session_memory_mgr", None)
                if mgr is None or id(mgr) in seen_mgrs:
                    continue
                seen_mgrs.add(id(mgr))
                yield mgr


def _rebind_session_memory_managers(
    react_agent: object,
    model: object,
    *,
    host: object | None,
    previous_model: object | None,
    model_config: object,
    model_client_config: object,
) -> int:
    """换模后重绑继承自主模型的会话记忆。显式配置的 memory.model 保持不动。

    ``ContextProcessorRail`` 只在 ``memory.model is None`` 时把主模型对象抄进去，
    之后 ``bind_model_defaults`` 不再覆盖。因此用对象身份判断「继承」。
    已惰性建出的 update agent / direct model 必须丢掉，否则仍拿旧 ``Model`` 实例。
    """
    prev_config = getattr(previous_model, "model_config", None)
    prev_client = getattr(previous_model, "model_client_config", None)
    rebound = 0
    for mgr in _iter_session_memory_managers(react_agent, host):
        cfg = getattr(mgr, "config", None)
        if cfg is None:
            continue
        current_model = getattr(cfg, "model", None)
        inherited_model = prev_config is not None and current_model is prev_config
        inherited_client = (
            prev_client is not None and getattr(cfg, "model_client", None) is prev_client
        )
        # G.CTL.03: if 内布尔条件不超过 3 个，复合判断先收成变量
        rebind_model = inherited_model
        rebind_client = inherited_client or rebind_model
        if not rebind_client:
            continue
        ok = True
        if rebind_model and not _assign_attr(cfg, "model", model_config):
            ok = False
        if not _assign_attr(cfg, "model_client", model_client_config):
            ok = False
        updater = getattr(mgr, "_update_agent", None)
        if updater is not None:
            for name in ("_agent", "_direct_model", "_workspace_root"):
                if not _assign_attr(updater, name, None):
                    ok = False
        if ok:
            rebound += 1
            logger.info(
                "session memory model rebound to %s",
                getattr(model_config, "model_name", ""),
            )
            continue
        logger.warning("session memory model rebind incomplete")
    return rebound


def sync_agent_compressor_models(
    react_agent: Any,
    model: Any,
    *,
    host: object | None = None,
    previous_model: object | None = None,
) -> int:
    """把上下文压缩器绑到与主对话相同的 ``Model`` 实例。

    ``host`` 是挂着 rail 的 DeepAgent；``previous_model`` 是 ``set_llm`` 之前的
    主模型，用来识别继承来的配置对象。

    Returns:
        实际改写且压缩执行器仍可用的 live processor 数量。执行器重建失败的
        processor 不计入（此时执行器已置空，压缩链不再工作）。
    """
    if react_agent is None or model is None:
        return 0
    mco = getattr(model, "model_config", None)
    mcc = getattr(model, "model_client_config", None)
    if mco is None or mcc is None:
        return 0

    cfg = getattr(react_agent, "_config", None) or getattr(react_agent, "config", None)
    if cfg is not None:
        for _name, pcfg in list(getattr(cfg, "context_processors", None) or []):
            if hasattr(pcfg, "model"):
                _assign_attr(pcfg, "model", mco)
            if hasattr(pcfg, "model_client"):
                _assign_attr(pcfg, "model_client", mcc)

    updated = 0
    engine = getattr(react_agent, "context_engine", None)
    pool = getattr(engine, "_context_pool", None) or {}
    for ctx in pool.values():
        for proc in list(getattr(ctx, "_processors", None) or []):
            if not hasattr(proc, "_model"):
                continue
            if not _assign_attr(proc, "_model", model):
                continue
            if hasattr(proc, "_compression_executor"):
                # CompressionExecutor 在构造时固定 model；必须重建才能换线。
                # _model 已换；重建失败时置空，且不计入 updated。
                if not _rebind_compression_executor(proc, model):
                    continue
            updated += 1
    updated += _rebind_session_memory_managers(
        react_agent,
        model,
        host=host,
        previous_model=previous_model,
        model_config=mco,
        model_client_config=mcc,
    )
    return updated
