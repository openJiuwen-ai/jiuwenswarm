# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LLM 花费归因头——**框架原生接线**（2026-09-23）。

## 这个模块解决什么

花费的唯一出口是本地预算代理（`scripts/budget_proxy.py`），它按请求头
`X-Helix-Line` / `X-Helix-Session` 把用量记到**线与会话**上（落
`runs/meta/proxy_usage.jsonl` 的 `line_hint` / `session_hint`）。框架路径此前不发这两个头，
故这两个字段长期为空（实测 34,780 条关口记录里 `session_hint` 非空仅 2,521 条 = 7.2%）。

## 为什么不能用"启动期静态配置"（实测结论）

`X-Helix-Line`/`X-Helix-Session` **不能**靠启动 env 一次性写死：

1. LLM 调用发生在 **agentserver 进程内**（共享、长驻），而按线注入的 env 只给了
   `jiuwenswarm chat` CLI 子进程（`launch_generation.py:1437`）；
2. 模型实例是**进程级缓存**（`agent_ws_server.AgentWebSocketServer._model_cache` /
   `interface_deep.JiuWenSwarmDeepAdapter._model_cache`，key = `model_name`），
   而 `Model.__init__` 会把 `model_client_config.custom_headers` 冻结进
   `client._base_headers`（`openai_model_client.py:380`）⇒ 静态配置在**同一进程内
   多线并发**时只能给出同一个值。

## 接线（三处，全部在**本仓源码**内，不改安装包）

1. **请求级上下文**（权威来源）：`server/agent_ws_server.py` 在**每消息入口**
   `set_current(request.session_id)` —— 会话 id 形如 `gen-<线名>-<ts>`，线名由
   `derive_line()` 推出；
2. **原生回调注入**（本模块 `install_llm_attribution_hook`）：向框架公开的
   `Runner.callback_framework` 注册 `LLMCallEvents.LLM_INVOKE_INPUT` /
   `LLM_STREAM_INPUT` 的 **transform** 回调，在**每次 LLM 调用**的 kwargs 里并入
   `custom_headers`。`Model.__init__` 用 `transform_io(input_event=…)` 包住
   `client.invoke`/`client.stream`，回调返回的 `(args, kwargs)` 即真实调用参数；
   `custom_headers` 由各 model client 原生 `pop` 后并入 `extra_headers`
   （`openai_model_client.py:1514/1672`、`openai_account_model_client.py:109/184`、
   `anthropic_model_client.py:767/875`）——**不需要给安装包打补丁**；
3. **进程 env 兜底**：`HELIX_LINE`/`HELIX_LINE_ID`、`HELIX_SESSION`/`HELIX_SESSION_ID`/
   `PROBE_ID`（口径与 `scripts/llm_api.request_headers` **逐字一致**），供单线部署、
   无会话 id 的请求（心跳/定时/探测）使用。

## 默认行为（不破坏）

三者都取不到值 ⇒ `headers()` 返回 `{}` ⇒ 注入器原样返回 `(args, kwargs)`，
**不发任何头**，与接线前逐字节相同。`sanitize_headers` 也会丢弃空值头。

## 与 `patches/openjiuwen/0003` 的关系

0003 把归因逻辑做进了安装包（新增 `openjiuwen/.../attribution.py` 并改
`headers_helper.build_base_headers`），且**在 Model 构造期**取头（值会被冻结）。
本模块是它的**原生替代**：同样的头名、同样的 env 口径、同样的 `derive_line` 规则，
但注入点从"构造期（包内）"移到"调用期（本仓回调）"。
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: 关口（`scripts/budget_proxy.py`）读取的头名——**不得改名**。
HEADER_LINE = "X-Helix-Line"
HEADER_SESSION = "X-Helix-Session"

#: 线与会话的 env 取值顺序（与 `scripts/llm_api.request_headers` 一致）。
LINE_ENVS = ("HELIX_LINE", "HELIX_LINE_ID")
SESSION_ENVS = ("HELIX_SESSION", "HELIX_SESSION_ID", "PROBE_ID")

#: 线名别名（`obs` → `observability` 等）。⚠️ 这是"别名表"，**不是**线名单：
#: 真正的线名由会话 id 形状给出（见 `_SESSION_RE`），新开一条线不必改本文件。
_LINE_CANON = {
    "obs": "observability",
    "ext": "external",
    "gov": "governance",
    "se": "self-evolution",
}

#: 会话 id 的通用形状：`gen-<线名>-<ts>` / `iter-<线名>-<ts>` / `line-<线名>-<ts>`。
#: 线名可含 `-`（`self-evolution`），末段是时间戳。`task-*`/`cron_*`/`p-*` **不匹配**。
_SESSION_RE = re.compile(r"^(?:gen|iter|line)-([a-z][a-z0-9-]*?)-\d+(?:-\d+)?$", re.I)

#: 兜底：id 里**任意位置**出现已知线名别名（`line-se-v1-r2-…` 这类旧形）。
_LINE_RE = re.compile(
    r"(observability|external|governance|self-evolution|se|obs|ext|gov)(?:-|$|_)",
    re.I,
)

_current: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "jiuwenswarm_llm_attribution", default=None
)


def derive_line(session_id: Optional[str]) -> str:
    """从会话 id 推线名（`gen-observability-1789…` → `observability`）。

    通用形状优先（`gen-<线名>-<ts>` 的 `<线名>` 经别名规范化后原样返回，不查硬编码
    名单 ⇒ 新线自动生效）；形状不匹配时才退回"id 里出现已知别名"的宽松匹配。
    推不出返回 ""（`task-*`、`cron_*`、`p-*` 这类**不是线会话**，不得乱归）。
    """
    if not session_id:
        return ""
    text = str(session_id).strip()
    if not text:
        return ""
    match = _SESSION_RE.match(text)
    if match:
        token = match.group(1).lower()
        return _LINE_CANON.get(token, token)
    match = _LINE_RE.search(text)
    if not match:
        return ""
    token = match.group(1).lower()
    return _LINE_CANON.get(token, token)


def set_current(
    session_id: Optional[str] = None,
    line: Optional[str] = None,
) -> contextvars.Token:
    """在**当前任务上下文**里写入归因（返回 token，调用方可 `reset` 复原）。

    agentserver 应在**每请求入口**调用一次；同任务内的所有 LLM 调用随之带上归因头。
    """
    values = {
        "session": str(session_id).strip() if session_id else "",
        "line": str(line).strip() if line else "",
    }
    if not values["line"]:
        values["line"] = derive_line(values["session"])
    return _current.set(values)


def reset(token: contextvars.Token) -> None:
    """复原 `set_current` 的设置（异常安全，供 finally 使用）。"""
    try:
        _current.reset(token)
    except Exception:  # noqa: BLE001 - 跨任务 reset 会报错；忽略即可（不致命）
        pass


def current() -> dict:
    """当前生效的归因（请求上下文优先，其次进程 env；都缺则空值）。"""
    ctx = _current.get() or {}
    line = ctx.get("line") or next(
        (os.environ.get(k, "") for k in LINE_ENVS if os.environ.get(k)), ""
    )
    session = ctx.get("session") or next(
        (os.environ.get(k, "") for k in SESSION_ENVS if os.environ.get(k)), ""
    )
    if not line and session:
        line = derive_line(session)
    return {"line": str(line).strip(), "session": str(session).strip()}


def headers() -> dict[str, str]:
    """要注入到 LLM 请求的归因头（空值不发，避免无意义头部）。"""
    cur = current()
    out: dict[str, str] = {}
    if cur["line"]:
        out[HEADER_LINE] = cur["line"]
    if cur["session"]:
        out[HEADER_SESSION] = cur["session"]
    return out


def merge_into_kwargs(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """把归因头并入一次 LLM 调用的 kwargs（**纯函数**，便于单测）。

    - 无归因头 ⇒ 原样返回入参（默认行为零变化）；
    - 调用方**显式**传入的 `custom_headers` 优先（`setdefault` 语义），不被覆盖；
    - 显式传入的非法值（非 Mapping）按"无"处理并保留原值不动。
    """
    attrs = headers()
    if not attrs:
        return dict(kwargs)
    merged = {**kwargs}
    existing = kwargs.get("custom_headers")
    if existing is None:
        merged["custom_headers"] = dict(attrs)
    elif isinstance(existing, Mapping):
        combined = {str(k): v for k, v in existing.items()}
        for key, value in attrs.items():
            combined.setdefault(key, value)
        merged["custom_headers"] = combined
    return merged


def drop_injected_session(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """摘掉**框架注入**的 `session` kwarg（否则它会一路漏进 OpenAI SDK）。

    `callback/framework.py:183 _inject_session_if_needed` 对"签名里有 `**kwargs`"的回调
    **就地**塞入 `session=get_current_session()`。transform 回调把 kwargs 原样返回后，
    这个 `session` 会经 `_bind_args_no_duplicate` → `client.invoke(**kwargs)` →
    `_build_request_params(**kwargs)` 直达 SDK。实测报错：
    `TypeError: AsyncCompletions.create() got an unexpected keyword argument 'session'`
    （2026-09-23，本模块首次注册 transform 回调时暴露）。

    为什么可以摘：**所有** model client 都不消费 `session` 这个 kwarg——全量 grep
    `openjiuwen/core/foundation/llm/` 内 `pop("session")`/`get("session")` 命中 **0 处**
    （KV 缓存走的是 `session_id`，见 `openai_model_client.build_kv_cache_invoke_kwargs`）。
    只摘"值等于当前 session（=框架注入的那份）"的键；调用方自己传的原样保留。
    """
    try:
        if "session" not in kwargs:
            return dict(kwargs)
        from openjiuwen.core.session import get_current_session

        if kwargs["session"] is not get_current_session():
            return dict(kwargs)
        cleaned = {k: v for k, v in kwargs.items() if k != "session"}
        return cleaned
    except Exception:  # noqa: BLE001 - 摘不干净也不能更糟：原样返回
        return dict(kwargs)


async def _transform_llm_input(*args: Any, **kwargs: Any) -> tuple:
    """`LLM_INVOKE_INPUT`/`LLM_STREAM_INPUT` 的 transform 回调。

    框架契约（`callback/decorator.py:_input_from_events`）：回调收到 `(*args, **kwargs)`，
    必须返回 `(new_args, new_kwargs)`；框架用 `_bind_args_no_duplicate` 把它们绑回原函数。
    **绝不抛异常**：归因是观测，不能让观测打断被观测的调用。
    """
    call_kwargs = drop_injected_session(kwargs)
    try:
        return args, merge_into_kwargs(call_kwargs)
    except Exception:  # noqa: BLE001 - 归因失败绝不影响主流程
        logger.debug("[llm-attribution] 注入 custom_headers 失败", exc_info=True)
        return args, call_kwargs


#: 已安装的框架（按 `id()` 去重）——`install_llm_attribution_hook` 幂等。
_INSTALLED_FRAMEWORKS: set[int] = set()


def install_llm_attribution_hook(framework: Any = None) -> bool:
    """向框架回调注册表安装归因 transform 回调（幂等）。

    使用**框架公开 API**（`Runner.callback_framework.on_transform`），只注册
    `callback_type="transform"` 的回调——`trigger()`（before/after 观测链）会显式跳过
    该类型，故对既有观测 rail 零影响。

    Args:
        framework: 目标回调框架；None 时取 `Runner.callback_framework` 单例。

    Returns:
        True = 本次真的安装了；False = 已装过或框架不可用。
    """
    try:
        if framework is None:
            from openjiuwen.core.runner import Runner

            framework = Runner.callback_framework
    except Exception:  # noqa: BLE001 - 无框架（纯脚本/测试）时静默跳过
        logger.debug("[llm-attribution] 取 Runner.callback_framework 失败", exc_info=True)
        return False

    if framework is None:
        return False

    key = id(framework)
    if key in _INSTALLED_FRAMEWORKS:
        return False

    try:
        from openjiuwen.core.runner.callback.events import LLMCallEvents

        for event in (LLMCallEvents.LLM_INVOKE_INPUT, LLMCallEvents.LLM_STREAM_INPUT):
            framework.on_transform(event)(_transform_llm_input)
    except Exception:  # noqa: BLE001 - 安装失败不阻断启动（只是少归因）
        logger.warning("[llm-attribution] 安装归因回调失败", exc_info=True)
        return False

    _INSTALLED_FRAMEWORKS.add(key)
    logger.info(
        "[llm-attribution] 已安装原生归因回调（llm_invoke_input / llm_stream_input）"
    )
    return True
