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

# session.model / ops.session.model 特性设计

## 1. 范围、状态与依据

本分支已实现。最近修订 2026-10-31，维护者刘瑜；本次基于已合入 Server `c6a07f909` / Core `bdde48a8`，不依赖未合入的生命周期 PR。公共机制以 [S_16](../specs/S_16_session-query-events.md) 为准，专项字段主责为 [S_04_session-model-ops.md](../specs/S_04_session-model-ops.md)；本文字段表提供填写与消费解释，不另立契约。

本次按用户确认的方向，以 Claude Code Mods 2.1.277 的返回形态优先；固定参考提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`。此前直接读实现已改为查询事件链；旧验证不能代替本次事件化验证。统一实施与最终证据见 [F_27](F_27_session-query-events.md)。

## 2. 能力说明与 Claude 对照

`await ops.session.model()`：读取当前绑定主模型的公开模型名，适合显示或记录模型标签。优先取已应用运行快照；没有运行快照时只读已保存的可得名称，不创建客户端、不激活或切换模型。

`session.model` 在每次该 Ops 调用时触发，Handler 接收查询参数，可继续、加工独立返回视图、短路或拒绝；默认 CORE 完成读取，结果由原 Ops 调用者消费。它不是状态变化通知，也不会自动渲染或写入记录。

| 能力 | 默认 CORE 数据来源 | 公开返回 | 与 Claude 的差异 |
| --- | --- | --- | --- |
| session.model | 绑定 session_model_info 经公开字段校验后取 model_name；否则 get_session_model_info(session_id)。 | str 或 None | 返回主体对齐 Claude 字符串；本地缺失事实保留 None。保存名称不保证此刻已经激活，运行快照仍优先于保存信息。 |

## 3. 事件契约

### 3.1 触发位置与完整流程

```text
示例入口：原生回合完成，平台收集可信会话资料
  → notify_context_usage(...) 安排 context.usage.updated
  → Worker EventBus 给实际 USER on_usage 绑定本次 Ops
  → USER 调用 ops.session.model()
  → 查询代理：验证参数与会话，固定原调用包，建立循环保护
  → 同一 Worker EventBus.trigger(同名查询 Event, 原可信 SessionContext)
  → 查询 USER：前置操作 → next_handler(event)
  → 查询 BUILTIN：计时 → next_handler(event)
  → 查询 CORE：直接读取平台事实或服务，不调用同名公共 Ops
  ← {'value': 查询结果} / {'deny': 原因} 逐层返回
  ← 代理验证、复制、解包；拒绝转 SessionQueryDenied
  ← 外层 USER 使用值；按原事件契约 next，继续原通知 CORE
```

这个入口只是现成示例，不是查询前置依赖。任何已由平台绑定相应会话的 Handler 都可调用；用户不用修改 Adapter、创建 EventBus 或先调用 notify 取得上下文。Front/Supervisor 不新增 EventBus。

### 3.2 事件输入、上下文和调用者

| 对象 | 类型 | 谁提供 | 含义 | 如何使用 |
| --- | --- | --- | --- | --- |
| ops.session_context | Core 只读属性 | 平台在业务边界提供，Core 规范化冻结 | 当前 Handler 的可信会话入口；不是 await 方法，不负责采集或组装数据 | 直接读取属性；不要把用户 payload 转成可信上下文 |
| SessionContext.session_id | str | 平台业务入口 | 当前会话身份；query 参数中没有这个字段 | session-a；用户不能通过修改 Event 输入换目标 |
| SessionContext.cwd / metadata | 只读快照 | 实际业务/运行时绑定 | 目录、项目根及可用主模型/用量快照；不要求装载全部历史或 Git 数据 | repo/messages/turns 可凭身份调用底层服务 |
| current_query_origin().package | str 或 None | 查询代理从原 Ops 的包身份固定 | 发起查询的包；不是当前查询 USER Handler 所属包。直接由宿主触发且无 Ops 调用域时 helper 返回 None | query.reader；不包含一个猜测的 tier |

### session.model：输入

| 字段 | 类型 / 默认 | 谁填写 | 含义与边界 | 怎样填 / 例子 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.<方法>() |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

### session.model：返回值字段

| 字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式 / 例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | str 或 None | 由平台 CORE 或查询 Handler 返回 | 模型名称字符串；只有选择 ID 而没有实际名称时返回 None，不把路由 ID 当模型名 | example-model；不是模型详情字典 |
| 不再返回的旧键 | selection/model_name/provider/source/activated | 调用者删除旧访问代码 | 这些是平台内部模型事实或保存选择结构，不再是公开 model() 的返回字段 | 把 info['model_name'] 改成 model_name = await ops.session.model() |

### 3.3 事件输出、拒绝与合法改写

| 事件结果键 | 类型 | 出现规则 | 含义 | 使用方式 |
| --- | --- | --- | --- | --- |
| value | 按方法定义 | 成功时唯一顶层键 | 下游或 CORE 给出的查询值；合法 None 仍须包装成 {'value': None} | 普通 Ops 调用已解包，不读取 result['value'] |
| deny | str，去空白后非空，最多 512 字符 | 拒绝时唯一顶层键 | 明确拒绝原因；不能与 value 同时出现，也不靠 raise 来代替 | return {'deny': 'WORKSPACE_QUERY_DISABLED'} |

普通 Ops 返回的是 value 本身，查询 Handler 的 next 返回的是封装结果。字段改写只改变本次查询视图，不会更换可信目标、切目录、切模型或改历史。CORE 和原始服务不使用可改写的 cwd/root/model 查询结果来重定向其他查询。

普通 USER/BUILTIN 异常及无效输出依 Core 隔离，默认读取可能继续；这不等于明确拒绝。需要拒绝就返回 deny，调用方捕获 SessionQueryDenied。取消继续传播，调用域 finally 清理；CORE/框架故障不冒充空值或成功。多个 Handler 按 USER→BUILTIN→CORE 及同层注册顺序进入，结果反向返回。多次 next 可能重复读取，不承诺查询默认缓存。

### 3.4 用户 Handler 注册示例

```python
class Extension:
    def register(self, registrar):
        registrar.register_event_handler("session.model", self.on_query, {}, "user")

    async def on_query(self, event, ops, next_handler):
        # 这里可记录审计、检查自己的规则；默认查询用 next，不能再次调同名 Ops。
        result = await next_handler(event)
        if "deny" in result:
            return result
        return {"value": result["value"]}  # 可按本方法契约加工独立返回副本
```

短路查询可直接 `return {"deny": "QUERY_DISABLED"}` 或 `return {"value": 合法值}`；不会执行后续默认查询。无参数查询使用空匹配模式；usage 可按 `breakdown` 等参数匹配。Matcher 在 trigger 时固定，next 改写参数不会重新匹配。

## 4. 方法契约

### 4.1 签名、调用场合与填写方式

```python
model_name = await ops.session.model()
logger.info("primary model name: %s", model_name)
```

### 4.2 返回值与错误

### 6.2 当前边界

只有已接线入口保证提供运行摘要；其他已绑定会话的事件可读保存值。保存信息不能证明运行激活，运行摘要不能证明未来模型调用、网络或实际路由成员。模型切换、权限变更和 UI 不属于查询副作用。

## 7. 参考资料

- [S_04](../specs/S_04_session-model-ops.md)、[Ops](../../handler_ops/session_ops.py)、[白名单与保存读取](../../../server/runtime/session/model_selection_store.py)。
- [稳定选择类型](../../../common/model_selection.py)、[通知入口](../../event_types/context_usage.py)、[可安装示例](../../examples/session_model_info/README.md)。
- [Claude 固定版本声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)、[内置事件接入说明](../designs/builtin-event-development-guide.md)。

各方法字段和单位已在第 3.2 节逐项列出。返回副本可供用户加工，不写回平台，也不自动保存到 SessionContext。结果未知、空数组、真实零值和读取失败按对应方法区分；不得统一回填零或空字符串。

无 SessionContext 时 RuntimeError；会话身份非法由平台检查拒绝；非法方法参数 TypeError。显式拒绝为 SessionQueryDenied（code=DENIED、reason 是用户收到的公开原因）。嵌套同名或间接查询循环为 SessionQueryError(code=QUERY_REENTRANT)，深度超过 16 为 QUERY_DEPTH_EXCEEDED。调用参数不能覆盖 bus/event_type。平台具体读取错误继续保留其错误类型和稳定 code。

## 5. 接入与兼容方案

### 5.1 平台装配和业务调用链

bootstrap 注册本次同名查询类型、Matcher、Validator 和唯一默认 CORE；再注册真实查询 BUILTIN（计时与独立副本）以及 ops.build BUILTIN。ops.build CORE 只创建共享 Builder，BUILTIN 登记查询代理；每个实际 Handler 获得的是共享目录全部能力与当次可信上下文，不是只装载当前事件的一个方法。

实现分工：`event_types/session_queries.py` 定义、校验和注册；`handler_ops/session_queries.py` 创建封闭代理；`query_dispatch.py` 负责 trigger、调用者归属、重入和解包；`event_handlers/session_queries.py` 提供查询 BUILTIN/CORE；真实读取位于 `server/runtime/session/query_*`。新增查询定义模块不包含 trigger，本次不迁移既有 notify 入口的位置。

内部旧直接读取辅助函数/分页服务仍可供平台和存储回归使用，但不再登记为这七个公开 Ops。查询不创建 Agent、不发模型请求；用户包卸载移除其 USER Hook，不移除平台查询事件或默认 CORE。没有全局结果缓存；并发和嵌套会话分别绑定。

### 5.2 迁移清单

| 方法 | 本次需要调整什么 |
| --- | --- |
| model | 旧 info['model_name'] / info.get('provider') 等访问改为直接消费字符串；需要模型配置详情的业务应使用自己已有的配置能力，不从名称反推供应商、凭据或激活状态。 |

### 5.3 可运行案例与效果

分支示例为 [examples/session_model_info](../../examples/session_model_info/)，统一七项示例见 [session_query_audit](../../examples/session_query_audit/)。验证复用 Token Weather 订阅的既有 context.usage.updated 通知作为入口；collect_session_parameters 返回包含七项真实查询结果的参数字典，交给用户自己的 on_parameters 业务函数。测试逐字段核对字典，并将 ui.render 设置为一旦调用就失败；不介入原 Token Weather 渲染代码。

具体例子：平台在一次真实回合后绑定 session-a 的 cwd、项目根、模型/用量观测并触发 context.usage.updated；用户 on_usage 调用 ops.session.model，代理进入同名查询链，查询 USER 可审计，BUILTIN 计时，CORE 读取原服务；代理解包，用户消费返回参数，再 next 原用量通知。用户只安装自己的包和 Handler，不编写 Adapter 收集资料。

## 6. 验收与未决问题

| 本次实际验证 | 结果 | 范围 |
| --- | --- | --- |
| 扩展模块回归 | 632 项通过 | 生产装配、七项查询、包加载/卸载、原历史与既有扩展行为；源码使用已合入 Core bdde48a8 |
| 相关平台回归 | 140 项通过 | 原生用量入口、历史路径/写入/游标、模型选择迁移及运行配置 |
| 独立进程参数验收 | 通过，7 项返回值逐字段一致 | 真实 ZIP 包→平台 notify→context.usage.updated→七个 Ops/同名查询 Event→返回参数→卸载/释放 |
| 不进入渲染链 | ui.render 调用 0 次 | 验收中 render 一旦被调用即失败；原 Token Weather 渲染文件保持原样 |

专项用例覆盖默认结果、类型/缺失语义、拦截/短路/拒绝、故障隔离、绑定身份、并发/递归/取消、真实包加载与卸载；关联平台用例仍覆盖真实 Git/JSONL 和原存储规则。模型查询验证绑定快照与保存信息，不据此宣称执行了新的模型 API 请求。未执行的浏览器/跨进程用户操作不列为通过。

## 7. 参考资料

- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude OpEventOf 与拦截规则](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [F_27 统一实施记录](F_27_session-query-events.md)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
