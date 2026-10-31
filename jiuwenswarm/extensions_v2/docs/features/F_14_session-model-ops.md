# ops.session.model 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_14 / session-model-ops |
| 归档日期 | 2026-10-26 |
| 后续修订 | 2026-10-29，完整字段、示例、来源/激活语义；仅文档 |
| 关联功能 PR | PR02 [!79](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/79)，已合入 |
| 主责契约 | [S_04](../specs/S_04_session-model-ops.md) |

## 1. 范围、状态与依据

本方法已实现并合入。本轮基于最新扩展目标 7795851916dd5ce1d34b70f5a20d6d30eafc25e3 统一说明，未修改业务代码。目的是读取绑定主会话的模型事实，不是查询模型目录、配置模型或调用模型。

## 2. 能力说明与 Claude 对照

`await ops.session.model()` 优先读取本次可信运行摘要；没有非 None 摘要时，在线程中读取该会话保存的稳定选择或旧版模型名。返回固定五字段 dict 或 None，不补全局默认模型，不创建/切换/激活模型，不执行网络调用。

Claude 固定版本的 `$.session.model()` 返回模型字符串；本地返回结构化事实，明确区分实际运行绑定和保存的选择。字段名称相似不表示可以原样套用 Claude 消费代码。

## 3. 复用事件与上下文契约

本特性没有新增查询事件或查询 CORE。示例订阅既有 `context.usage.updated`；这只是一个已接线的业务触发入口，不是调用查询的前置操作。其他已有事件只要平台提供所需 SessionContext，其 USER Handler 同样可以调用。

| 对象 | 字段 / 来源 | 用户应如何使用 |
| --- | --- | --- |
| 原通知输入 | ContextUsageUpdate：session_id、request_id、input_tokens、output_tokens、total_tokens、cache_tokens、cache_hit_rate、context_window_tokens、usage_percent、model | 由平台汇总填写；不包含本特性查询的权威目录/模型/仓库返回值。仅用于观察和筛选，不能靠改 payload 重定向 Ops |
| 原通知输出 | ContextUsageOutput：accepted 为 bool；request_id 为 str | 默认 CORE 填 accepted=True 并回显请求 ID，仅确认输入类型已接受，不表示目录/模型/Git/UI 验收成功。用户通常原样 next 返回 |
| Ops 绑定 | Core 已有只读属性 ops.session_context | 平台随 Event 单独提供，Core 规范化/冻结后绑定到本次 Ops；不是本次新增的异步方法，用户不调用它来收集资料 |

原通知在已接线的原生流式请求末尾、有有效用量时由平台调度，Worker 内实际 trigger；不按每个 Token、工具调用或目录/模型变化触发。默认通知 CORE 只校验输入类型，没有因本次查询新增完整数值 Validator。用户保持输入并 `return await next_handler(event)` 继续原链；短路不撤销已完成回合，USER 异常依 Core 隔离规则处理。

完整通知定义以 [context_usage.py](../../event_types/context_usage.py) 为准。查询本身不会再 trigger、不会再次 ops.build，也不会把自己的返回值自动塞入原通知回执。


## 4. 方法输入输出与填写规则

### 4.1 输入：无参数，平台决定查询目标

```python
info = await ops.session.model()
```

用户不填模型名、model_id、session_id、provider、路径或凭据。平台使用可信 SessionContext.session_id；运行摘要来自 metadata.session_model_info。该键缺失或为 None 时回读保存信息；**非 None 的非法摘要报错，不静默回读旧值**。

这与某些其他能力“键存在且为 None 就禁止回读”的策略不同，以本方法实际 `.get(...)` 规则为准。仅有会话 ID 的事件也可读取保存选择；不必先调用 notify。

### 4.2 非 None 返回的五个字段

以下由平台返回，用户只读，不需要填回 Event 或查询参数。所有五个键均存在；None 是字段未知，不用空字符串或猜测值替代。

| 字段 | 类型 / 缺省语义 | 准确含义、来源与合法值 | 示例与消费方式 |
| --- | --- | --- | --- |
| selection | dict 或 None | 保存的稳定选择；运行摘要必须为 None，不能混入旧保存选择。仅有旧模型名时也为 None | {'type':'model_group','id':'coding-group','route_id':'fast'}；子字段见下一表 |
| model_name | str 或 None | 当前运行模型的配置名称，或旧版保存名称；非空文本去首尾空白。保存了稳定选择时不解析名称，因此可为 None | "demo-model"；不是 API Key，也不保证是模型组某次实际物理路由成员 |
| provider | str 或 None | 可确认的运行提供方；只读保存选择/旧名称时为 None，不查模型目录补齐 | "OpenAI"；显示标签，不是客户端对象 |
| source | str，runtime / session | runtime 表示本次调用边界捕获的运行摘要；session 表示保存信息。不是默认模型配置来源，也不是读取文件路径 | 先判断该字段再解释 activated |
| activated | True 或 None | runtime 必须 True，表示边界已确认绑定；session 必须 None，表示激活状态未知。当前不会用 False 表示“尚未启动” | 不据此推断查询时 Worker 仍存活或模型网络可达 |

#### selection：有值时的全部子字段

| 字段 | 类型 / 是否必有 | 准确含义和合法值 | 应该填写什么 / 示例 |
| --- | --- | --- | --- |
| type | str，必有 | 稳定选择种类，只能 model 或 model_group | 由平台保存的选择提供；用户查询时不填写。例 model_group |
| id | str，必有 | 对应模型配置或模型组的稳定 ID；去空白后不能为空，不是 model_name | 例 coding-group；用户只读取 |
| route_id | str，可缺省 | 仅 model_group 可携带的逻辑 route ID；去空白后不能为空。None 在序列化时被省略，不能假设该键总在 | 例 fast；type=model 时不允许非 None route_id |

模型组 ID 或 route_id 是保存的逻辑选择，不证明已选定本次物理模型；运行摘要也不会把旧选择掺进来。不能根据这些字段反查或暴露凭据、端点、Header、请求默认值、客户端或内部模型对象。

### 4.3 三种典型返回和缺失

运行快照：

```json
{"selection":null,"model_name":"demo-model","provider":"OpenAI","source":"runtime","activated":true}
```

保存的模型组选择：

```json
{"selection":{"type":"model_group","id":"coding-group","route_id":"fast"},"model_name":null,"provider":null,"source":"session","activated":null}
```

旧版仅保存模型名：

```json
{"selection":null,"model_name":"legacy-model","provider":null,"source":"session","activated":null}
```

没有运行摘要、会话 metadata 不存在或没有保存的选择/旧模型名时，整个方法返回 None。它不表示该会话永远不能运行模型。不能把 None 与“保存选择的 activated=None”混为一谈：后者仍有合法事实对象。

| 情况 | 方法行为与消费方式 |
| --- | --- |
| 没有 SessionContext | RuntimeError；选择平台已有会话绑定事件 |
| 会话 ID 非法/路径越界 | ValueError；不尝试其他会话文件 |
| 宿主摘要字段类型非法 | TypeError，或选择模型校验 ValueError；不视为真实缺失 |
| source/activated 组合非法、运行摘要缺名称或混入 selection | ValueError；当前合法组合只有 runtime+True、session+None |
| 保存 metadata 不可读、非对象或选择不合法 | 既有 ModelSelectionError；保留已有错误，不用 None 冒充正常 |

返回为全新白名单字典，修改它不改变宿主摘要、保存记录或另一个 Handler。运行摘要描述的是事件边界，不是查询时重新探测模型。

## 5. 平台接入与用户使用

```text
启动：Worker 装配 → ops.build → CORE 返回 Builder
  → session_ops_build BUILTIN 在 next 返回后登记共享 session 方法
  → Core 保存能力目录；Registry 发布用户 Handler

业务：已接线的平台入口收集真实 SessionContext
  → Worker EventBus.trigger(原事件, context)
  → Core 为实际 Handler 创建当次 Ops，绑定会话和包身份
  → 用户 Handler await 本特性只读 Ops
  → Core 方法代理自动把当前 Ops 传给对应平台函数
  → 平台读取可信快照或本特性明确允许的数据源 → 直接返回值
  → 用户自行记录/展示，再 next_handler(原事件)
  → 剩余原事件 Handler → 原 CORE → 原通知任务收到回执
```

启动时准备的是共享方法目录，不执行查询。业务时创建绑定本次会话/包身份的 Ops 视图，不按事件名只创建少数方法；用户实际 await 哪个方法才执行哪个查询。这里的“Core 方法代理”是框架适配，不是新建的业务 CORE。

USER 负责自己的日志或展示；BUILTIN 负责启动时登记 Ops；原事件 CORE 处理原事件。三个查询特性均不新增专用查询 CORE、Rail、Front/Supervisor 协议。EventBus 仍只由 Worker 持有。

用户提供 manifest.yaml / extension.py，通过既有管理入口安装包并进行正常业务操作。平台负责业务触发和可信资料，用户不修改 Adapter，不创建 EventBus/Builder，不调用 notify 来“取回上下文”。安装成功只说明 Handler 已发布；尚无对应业务事件时不会自动执行查询。

返回值交给调用方，不自动缓存查询结果、写第二份状态文件或渲染 UI。示例的 logger 是用户显式写日志；其他使用方式需用户接好自己的消费逻辑。卸载移除 USER Handler，不删除平台能力或既有会话事实。


### 5.1 用户包与结果消费

现成包见 [session_model_info README](../../examples/session_model_info/README.md)，包根 manifest.yaml 与 extension.py 通过现有 /extension 管理入口安装。仓库 USER 示例完整代码：

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Log safe primary-model facts when a native turn reports usage."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_model_info")


class SessionModelInfo:
    """User-owned behavior consuming a host-provided read-only Ops method."""

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        info = await ops.session.model()
        logger.info("session model: session=%s info=%s", ops.session_context.session_id, info)
        return await next_handler(event)


def create_extension():
    return SessionModelInfo()
```

用户可先判断 `info is None`，再按 source 解释 activated；不要直接把 None 展示成“模型未激活”。需要前端展示时由用户另行消费结果，本能力不会自动渲染。

## 6. 验收与未决问题

2026-10-29 本轮仅修改文档。校核五个公开字段、selection 三个子字段、无参签名、示例和异常；说明用 JSON 按当前白名单转换核对，未调用真实模型或重跑历史业务验收。检查证据在 `D:\work_space\.codex-temp\server-session-docs-20261029\documentation-verification.json`。

<a id="5-验证设计与执行记录"></a>

### 6.1 原功能验证（2026-10-26，保留原结果）

验证覆盖真实 Registry 用户包调用、两会话隔离、请求覆盖、缺失事实、持久化选择与旧模型名称、无会话、非法 ID、损坏 metadata、白名单与返回值隔离、异步调度前快照，以及原生 Adapter 发布接线。

执行结果（2026-10-26）：

- 新增能力与接线定向验证：26 项通过，覆盖 15 个模型 Ops 用例、5 个用量事件用例和 6 个原生流结束用例。
- 扩展机制与原生用量回归：140 项通过；模型选择迁移、资源修复、目录及 MOA 兼容回归：116 项通过。两组共 256 项通过；上述 26 项包含在内，不重复累计。仅出现外部 Authlib 弃用提示。
- 独立 Python 进程使用仓库内示例生成 ZIP，通过真实 PackageManagementAdapter 安装，并经真实 Registry、EventBus、原生 Adapter 用量入口和 USER Handler 调用 Ops。两会话保持各自运行模型快照，另一个会话返回保存选择；实际 `agent_server.log` 输出了三条示例记录。
- 查询本身创建模型次数为 0；验证器中的两次模型解析来自正常请求。卸载后不再执行示例 Handler，最终独立进程正常退出并清理自身工作目录。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/agentserver/test_deep_adapter_stream_end_final.py tests/unit_tests/agentserver/test_context_usage.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/test_model_selection_migration.py tests/unit_tests/test_model_resource_fixes.py tests/unit_tests/runtime/test_model_catalog.py tests/unit_tests/agentserver/test_moa_runtime.py --asyncio-mode=auto -o log_cli=false
```

测试从当前 JiuwenSwarm 工作树与固定的 agent-core `ead700d7d18a247968d64de54918cf5d201a207a` 导入。独立进程的 Agent 输出为测试替身；未调用真实模型，未验证浏览器或网络链路，UI 展示不属于本次实现。

### 6.2 当前边界

只有已接线入口保证提供运行摘要；其他已绑定会话的事件可读保存值。保存信息不能证明运行激活，运行摘要不能证明未来模型调用、网络或实际路由成员。模型切换、权限变更和 UI 不属于查询副作用。

## 7. 参考资料

- [S_04](../specs/S_04_session-model-ops.md)、[Ops](../../handler_ops/session_ops.py)、[白名单与保存读取](../../../server/runtime/session/model_selection_store.py)。
- [稳定选择类型](../../../common/model_selection.py)、[通知入口](../../event_types/context_usage.py)、[可安装示例](../../examples/session_model_info/README.md)。
- [Claude 固定版本声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)、[内置事件接入说明](../designs/builtin-event-development-guide.md)。
