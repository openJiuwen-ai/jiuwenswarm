# ops.session.model() / session.model 特性设计

## 1. 范围、状态与依据

最近修订 2026-10-31。本文仅维护 `ops.session.model()` 与同名查询事件 `session.model` 的使用说明、接入和验证边界；一个方法与一个查询事件组成一项特性。开发状态、负责人和交付统一见[开发计划 EXTV2-22](../designs/extensions-v2-development-plan.md#extv2-22-七项只读查询事件化)，交付 [PR !100](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/100)。

公共协议主责为 [S_16](../specs/S_16_session-query-events.md)，专项字段主责为 [S_04_session-model-ops.md](../specs/S_04_session-model-ops.md)；下文表格解释怎样填写、怎样消费，不另立契约。共享实现和既有验收记录见[查询公共机制与七项特性索引](../designs/session-query-events.md)。

2026-10-31 按单个 Ops / Event 拆分特性文档；原合篇只作为文档组织历史，不代表仍需一起调用。此次拆分没有改变运行时契约或实现。

## 2. 能力说明与 Claude 对照

`await ops.session.model()`：读取当前绑定主模型的公开模型名，适合显示或记录模型标签。优先取已应用运行快照；没有运行快照时只读已保存的可得名称，不创建客户端、不激活或切换模型。

`session.model` 是这一次查询调用的拦截事件。方法代理验证参数和已绑定会话后触发它；查询 Handler 接收下述参数，可继续默认处理、增加前后操作、返回合法替代视图或明确拒绝。默认 CORE 读取平台事实，结果返回原方法调用者；它不表示状态发生变化，也不自动写历史或显示到界面。

| 项 | 当前默认行为 |
| --- | --- |
| 默认数据来源 | 绑定 session_model_info 经公开字段校验后取 model_name；否则 get_session_model_info(session_id)。 |
| 方法公开返回 | str 或 None |
| Claude 对齐与差异 | 返回主体对齐 Claude 字符串；本地缺失事实保留 None。保存名称不保证此刻已经激活，运行快照仍优先于保存信息。 |

对照依据是 Claude Code Mods 2.1.277 的固定公开类型声明，提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81`；不是运行 Claude 私有宿主的结果。

## 3. 事件契约

### 3.1 触发位置、粒度与完整链路

方法代理通过会话、参数及重入检查后，每次调用触发一次 `session.model`；检查未通过时不会进入查询链。下图展示 Handler 继续默认读取的路径；合法短路或明确拒绝可以在 USER 返回，不进入 CORE。返回箭头对应同一次 await 的结果，不是额外业务通知。

```mermaid
sequenceDiagram
    participant U as 外层用户 Handler
    participant O as Ops 查询代理
    participant B as Worker EventBus
    participant H as 查询 USER / BUILTIN
    participant C as 查询 CORE
    U->>O: await ops.session.model()
    O->>O: 取已绑定 SessionContext 并校验参数
    O->>B: trigger(session.model, 参数, context)
    B->>H: 匹配并执行查询 Handler
    H->>C: next_handler(event)
    C-->>H: value 结果
    H-->>B: value 或 deny 封装
    B-->>O: 查询事件结果
    O-->>U: 解包后的值或查询异常
```

平台业务方法先收集可信会话资料并随外层事件传入 Bus，Core 为实际 Handler 创建 Ops。现成示例是 notify_context_usage 发布 context.usage.updated；其他已由平台接好的事件也能使用本方法，前提是本次 Handler 已绑定下表所需资料。用户不需要修改 Adapter、自己创建 Bus，或先调用 notify 来取得上下文。

### 3.2 事件输入、会话上下文与字段说明

| 事件输入字段 | 类型 / 默认 | 谁填写 | 准确含义与改写边界 | 填写示例 |
| --- | --- | --- | --- | --- |
| Event.event_input | 空映射 {} | Ops 代理自动构造 | 查询 Handler 收到空参数对象；不是 SessionContext，也不携带可改写目标身份 | 注册时使用 match_pattern={} |

会话上下文通过 `bus.trigger(Event(...), context)` 的第二个参数传递，与 Event.event_input 分开。下面字段由平台和 Core 绑定，不能通过增加查询参数改换会话或读取目标。

| 对象 / 字段 | 类型与是否必需 | 谁提供 / 表达什么 | 填写或读取示例 |
| --- | --- | --- | --- |
| ops.session_context | Core 只读属性 | 平台随外层事件传入，Core 在执行 Handler 前绑定 | 直接读属性；不是 await 方法，不收集、不组装上下文 |
| SessionContext.session_id | 有效非空 str，必需 | 平台业务边界提供的当前会话身份 | session-a；不从 query 输入或 Hook 返回值改换目标 |
| SessionContext.metadata["session_model_info"] | 只读映射或 None，可缺省 | 平台捕获并校验的已应用运行模型资料；优先读取 model_name | 运行快照提供 demo-model；无可用快照时按 session_id 只读已存模型选择 |
| current_query_origin().package | str 或 None | Server 查询代理从原调用 Ops 的包身份记录 | 是原发起包，不是当前 Hook 所属包；无 Ops 查询域时 helper 返回 None，须先判空；不是 Core next.origin |

`ops.session_context` 是已有只读属性。业务方法负责采集和组装，查询代理只取出它并继续传给查询事件；Bus 会规范化为只读副本，保留同一会话身份和资料，不承诺前后是同一个 Python 对象。

### 3.3 事件输出、允许的改写、失败与取消

| 事件结果字段 | 类型 | 出现规则 | 准确含义 | 如何消费 |
| --- | --- | --- | --- | --- |
| value | str 或 None | 成功时唯一顶层键 | 本次查询值；允许 None 的查询仍须包装为 {"value": None} | Ops 会取出 value；调用者不需要再访问 result["value"] |
| deny | 去空白后非空 str，原字符串最多 512 字符 | 明确拒绝时唯一顶层键 | 公开拒绝原因；不能和 value 同时出现 | return {"deny": "QUERY_DISABLED"}；Ops 转为 SessionQueryDenied |

下面的返回字段位于事件结果的 `value` 内。方法成功返回时已经去掉外层 value 包装，因此消费示例中的对象就是方法返回值。

| 返回字段 | 类型 / 出现条件 | 谁提供 | 准确含义 | 消费方式与例子 |
| --- | --- | --- | --- | --- |
| 方法返回 / value | str 或 None | 由平台 CORE 或查询 Handler 返回 | 模型名称字符串；只有选择 ID 而没有实际名称时返回 None，不把路由 ID 当模型名 | example-model；不是模型详情字典 |
| 不再返回的旧键 | selection/model_name/provider/source/activated | 调用者删除旧访问代码 | 这些是平台内部模型事实或保存选择结构，不再是公开 model() 的返回字段 | 把 info['model_name'] 改成 model_name = await ops.session.model() |

继续默认查询使用 `await next_handler(event)`。需要短路时，可直接返回 `{"value": 'demo-model'}`；需要明确拒绝时返回 `{"deny": "QUERY_DISABLED"}`，下游可以不执行。加工结果必须符合本表的值类型及专项 spec；只改变本次返回视图，不改写平台真实状态、上下文或授权范围。

普通 USER / BUILTIN 异常或无效输出按 Core 隔离规则处理，可能继续默认 CORE；抛异常不能代替明确 deny。CORE / 框架读取失败向调用者传播，不用成功空值掩盖；取消继续传播并在 finally 清理查询调用域。匹配在 trigger 初始固定，next 改写合法参数不会重新选择 Handler；多次 next 可能重复读取，不承诺结果缓存。

### 3.4 可安装用户包中的注册与调用示例

将下面代码作为扩展包的入口模块，并按[现有示例 manifest](../../examples/session_model_info/manifest.yaml) 的形式指定入口。示例同时展示外层业务 Handler 如何调用本 Ops，以及本查询的 USER Handler 如何继续默认链。

```python
import logging

logger = logging.getLogger(__name__)


class Extension:
    def register(self, registrar):
        # Example outer event: the platform already binds SessionContext.
        registrar.register_event_handler(
            "context.usage.updated", self.on_usage, {}, "user"
        )
        # Intercept this query only.
        registrar.register_event_handler(
            "session.model", self.on_query, {}, "user"
        )

    async def on_query(self, event, ops, next_handler):
        logger.debug("before query: %s", event.event_type)
        # Continue this query chain; do not call the same public Ops here.
        result = await next_handler(event)
        logger.debug("after query: denied=%s", "deny" in result)
        return result

    async def on_usage(self, event, ops, next_handler):
        value = await ops.session.model()
        # Use value in your own business code; no ui.render is required.
        logger.info("session.model result type: %s", type(value).__name__)
        # Continue the original event, not the nested query event.
        return await next_handler(event)


def create_extension():
    return Extension()
```

查询 Hook 想取得默认值应调用 next；在自己的 `session.model` Hook 中再次调用同名 Ops 会被循环保护拒绝。USER → BUILTIN → CORE 在各层继续时执行，结果反向返回；共享 BUILTIN 负责计时与独立副本，默认 CORE 负责实际读取。

## 4. 方法契约

### 4.1 签名、参数与调用场合

下列是调用侧签名；Core 自动注入的内部 ops 参数不由用户填写。

```python
# Public call signature; no session_id or path argument.
async def model() -> str | None: ...
```

| 方法参数 | 类型 / 默认 | 谁填写 | 准确含义 | 应该怎样填 |
| --- | --- | --- | --- | --- |
| 方法参数 | 无 | 调用者不填写 | 查询已绑定会话，不接受 session_id、路径、模型名或包 ID | 直接调用 await ops.session.model() |

典型调用如下；它可写在任何满足会话前提的已注册业务 Handler 中。

```python
model_name = await ops.session.model()
logger.info("primary model name: %s", model_name)
```

### 4.2 方法与事件转换、返回值及错误

```text
方法参数 → 查询代理规范化 → Event.event_input
当前 ops.session_context → trigger 的独立 context 参数
  → 同名查询事件 session.model
事件 {value: 结果} → 参数/结果校验、独立复制、解包 → 方法返回值
事件 {deny: 原因} → SessionQueryDenied(code=DENIED, reason=原因)
```

成功返回 `str | None`，字段及缺失语义见第 3.3 节。无 SessionContext 报 RuntimeError；非法会话 ID 由平台会话校验拒绝；方法参数非法报 TypeError。活跃同名或间接循环报 QUERY_REENTRANT，深度超过 16 报 QUERY_DEPTH_EXCEEDED；不允许调用方覆盖 bus / event_type。平台读取故障保持其原有异常类型和稳定 code。Handler 未捕获 Ops 错误时，还受外层事件自身的故障隔离规则约束，不能把外层继续误认为查询成功。

## 5. 接入与兼容方案

### 5.1 启动登记、上下文绑定与 CORE 分工

平台 bootstrap 向 Bus 登记 `session.model` 的 Matcher、Validator 和默认 CORE，并登记查询 BUILTIN。Registry.init 触发一次 ops.build；其 CORE 创建 Builder，BUILTIN 将 session.model 对应的封闭查询代理加入方法目录，Bus 保存 Builder。执行每个实际 Handler 前，Core 用共享目录及本次包身份 / SessionContext 创建新的 Ops。

业务时，用户调用本方法，Core 方法访问器自动把当前 Ops 传给登记的代理；代理读取已绑定上下文并显式 trigger `session.model`。查询链各实际 Handler 也获得各自的 Ops，查询 CORE 走 `read_session_query` → `_model(context)` → 运行快照优先，必要时按会话 ID 读已存选择，不再调用公共同名 Ops。Builder 保存方法定义，不保存会话查询结果；ops.build 与运行时 builder.build(...) 是两种操作。

统一实现职责见[公共设计第 5 节](../designs/session-query-events.md#5-接入与兼容方案)。默认查询不写业务数据、不发模型请求、不自动渲染；没有全局查询结果缓存。用户包卸载只移除自己的 Hook，不移除平台查询类型或 CORE。

### 5.2 迁移与生效边界

旧 info['model_name'] / info.get('provider') 等访问改为直接消费字符串；需要模型配置详情的业务应使用自己已有的配置能力，不从名称反推供应商、凭据或激活状态。

本次文档拆分不再改变上述兼容边界。执行目录、模型、Git、历史和用量事实仍由平台服务维护；对返回视图的改写不成为另一项平台操作的可信输入。

### 5.3 现成案例和可观察效果

对应用户示例见 [examples/session_model_info](../../examples/session_model_info/)，七项联合参数返回示例见 [session_query_audit](../../examples/session_query_audit/)。联合案例中的 collect_session_parameters 是普通用户侧辅助函数，本方法可单独调用；用户业务函数收到返回参数即可验证，不依赖 ui.render。

## 6. 验收与未决问题

### 6.1 本特性的独立验收场景

| 场景 ID | 输入与前置 | 操作 | 预期结果 |
| --- | --- | --- | --- |
| F_14-V-01 | 运行快照与保存选择使用不同模型名 | 调用 model() | 优先返回运行快照中的字符串名称 |
| F_14-V-02 | 无运行快照；保存信息有名称或完全缺失 | 调用 model() | 分别返回已存名称或 None，不创建模型客户端 |
| F_14-V-03 | 安装仅拦截 session.model 的用户包 | 分别 next、短路 value、显式 deny | 默认值 / 合法替代值 / SessionQueryDenied 分别可观测 |
| F_14-V-04 | 不同会话绑定不同上下文；或在 Hook 中同名递归 | 分别调用本方法 | 会话隔离；活跃同名或间接循环报 QUERY_REENTRANT，不能无限递归 |
| F_14-V-05 | 读取失败、Hook 异常或查询取消 | 分别构造这些分支 | 按第 3.3 / 4.2 节区分隔离、明确拒绝、平台错误与取消，不把失败当成功空值 |

对应事件化用例位于 [test_session_query_events.py](../../../../tests/unit_tests/extensions_v2/test_session_query_events.py)：包含七项默认调用、各项短路 / 拒绝、非法参数、查询顺序、隔离、循环 / 取消及实际用户包装载卸载。每个场景只验证本方法承诺，不把其他六项也作为调用前置。

### 6.2 已有实施证据与本次文档调整

实现验证基于本 PR 实现提交 `f313f513ee9b`、已合入 Server 基线 `c6a07f909` 与 Core `bdde48a8`，Python 3.11。扩展 632 项、相关平台 140 项及独立进程 ZIP / notify / 七项参数返回验证的实际范围、命令和限制统一保留在[公共设计第 6 节](../designs/session-query-events.md#6-验收与未决问题)。这属于查询事件化实现的既有证据；此次仅拆分文档，不声称重新执行了这些业务测试。

返回主体对齐 Claude 字符串；本地缺失事实保留 None。保存名称不保证此刻已经激活，运行快照仍优先于保存信息。 该限制属于已知支持边界；此轮未增加新的运行时待决项。未执行 Claude 私有宿主、新模型 API、浏览器或 Front/Supervisor 跨进程验收，不能由文档完整推断这些验收已完成。

## 7. 参考资料

- [专项字段规约 S_04_session-model-ops.md](../specs/S_04_session-model-ops.md)
- [S_16 查询公共协议](../specs/S_16_session-query-events.md)
- [七项独立特性索引与共享实施记录](../designs/session-query-events.md)
- [Claude 会话方法](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L2342-L2447)
- [Claude 同名 OpEventOf 查询事件](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts#L5529-L5624)
- [内置事件开发指南](../designs/builtin-event-development-guide.md)
